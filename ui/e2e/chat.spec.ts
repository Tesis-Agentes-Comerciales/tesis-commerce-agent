import { readFileSync } from "node:fs"
import { expect, test, type Page } from "@playwright/test"

import { DEMO_PROFILE, type ApprovalTerms } from "../src/lib/commerce-types.ts"
import { approvalRecord, BUYER_TOKEN, COMPLETED, PURCHASE_REQUEST } from "../src/storybook/fixtures.ts"
import { AGENT_API_URL, agentRequests, fitsViewport, routeSession, UI_ORIGIN } from "./support.ts"

// Bundle de producción contra un agente simulado en la red. `POST /assistant` reproduce los streams
// reales del backend (src/storybook/fixtures/assistant-transport, los mismos de Storybook) y las rutas
// REST responden con los datos de src/storybook/fixtures.ts.

const STREAMS = new URL("../src/storybook/fixtures/assistant-transport/", import.meta.url)
const stream = (name: string) => readFileSync(new URL(`${name}.sse`, STREAMS), "utf8")
const PURCHASE = stream("purchase")
const PURCHASE_COMPLETED = stream("purchase-completed")

type Approval = { run_id: string; terms: ApprovalTerms }

function findApproval(value: unknown): Approval | null {
  if (typeof value !== "object" || value === null) return null
  if ("run_id" in value && "terms" in value) return value as Approval
  for (const child of Object.values(value)) {
    const found = findApproval(child)
    if (found) return found
  }
  return null
}

/** Resultado de `request_approval` en el stream; el backend real repite esos términos por REST. */
function streamedApproval(sse: string): Approval {
  for (const line of sse.split(/\r?\n/)) {
    const found = line.startsWith("data:") ? findApproval(JSON.parse(line.slice(5))) : null
    if (found) return found
  }
  throw Error("El stream no trae el resultado de request_approval")
}

const APPROVAL = streamedApproval(PURCHASE)

type AssistantBody = {
  turn_id: string
  commands: { type: string; runId?: string; message?: { parts: { text: string }[] } }[]
  details: {
    buyer: Record<string, string>
    fulfillment: { address: Record<string, string> }
  }
}

type AgentCall = { method: string; path: string; authorization: string | null; body: unknown }

/** Agente simulado: registra cada llamada para revisar cuerpos y cabeceras. */
async function routeAgent(page: Page) {
  const calls: AgentCall[] = []
  const cors = {
    "Access-Control-Allow-Origin": UI_ORIGIN,
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  }
  await page.route(`${AGENT_API_URL}/**`, async (route) => {
    const request = route.request()
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors })
    const call: AgentCall = {
      method: request.method(),
      path: new URL(request.url()).pathname,
      authorization: await request.headerValue("authorization"),
      body: request.postDataJSON(),
    }
    calls.push(call)
    const json = (body: unknown) => route.fulfill({ headers: cors, json: body })
    switch (`${call.method} ${call.path.replace(APPROVAL.run_id, ":runId")}`) {
      case "GET /health":
        return json({ status: "ok" })
      case "GET /me/pending-purchase":
        return route.fulfill({ headers: cors, contentType: "application/json", body: "null" })
      case "POST /assistant": {
        // El segundo turno continúa el estado del primero, así que los streams encajan.
        const followUp = (call.body as AssistantBody).commands.at(-1)?.type === "purchase-updated"
        return route.fulfill({
          headers: cors,
          contentType: "text/event-stream",
          body: followUp ? PURCHASE_COMPLETED : PURCHASE,
        })
      }
      case "GET /purchases/:runId/approval":
        return json(APPROVAL.terms)
      case "POST /purchases/:runId/approval":
        return json(approvalRecord(APPROVAL.run_id))
      case "POST /purchases/:runId/complete":
        return json(COMPLETED)
    }
    return route.fulfill({ status: 404, headers: cors, json: { code: "not_found", message: "not found" } })
  })
  return calls
}

const assistantBodies = (calls: AgentCall[]) =>
  calls.filter((call) => call.path === "/assistant").map((call) => call.body as AssistantBody)

async function preparePurchase(page: Page) {
  const composer = page.getByLabel("Mensaje de compra")
  await composer.fill(PURCHASE_REQUEST)
  await composer.press("Enter")
  const card = page.getByTestId("purchase-approval-card")
  await expect(card).toHaveAttribute("data-status", "awaiting_approval")
  return card
}

test.beforeEach(async ({ page }) => {
  // El feed sintético apunta a un dominio de ejemplo; ninguna prueba sale a la red.
  await page.route("https://merchant.example.test/**", (route) => route.abort())
})

test("prepara, aprueba y completa una compra; la aprobación solo viaja por REST", async ({ page }) => {
  await routeSession(page)
  const calls = await routeAgent(page)
  await page.goto("/")
  await expect(page).toHaveTitle("Comprador ACP")
  await expect(page.getByText("¿Qué quieres comprar?")).toBeVisible()
  // Con el agente disponible la cabecera no anuncia nada; solo avisa cuando falla.
  await expect(page.getByText("Agente sin conexión")).toHaveCount(0)

  const card = await preparePurchase(page)
  await expect(page.getByTestId("product-card-hero")).toBeVisible()
  const [turn] = assistantBodies(calls)
  expect(turn.commands).toHaveLength(1)
  expect(turn.commands[0]).toMatchObject({
    type: "add-message",
    message: { parts: [{ text: PURCHASE_REQUEST }] },
  })
  expect(turn.turn_id).toMatch(/^[A-Za-z0-9_-]{1,128}$/)
  expect(turn.details.buyer.email).toBe(DEMO_PROFILE.email)
  expect(turn.details.buyer).not.toHaveProperty("phone_number")
  expect(turn.details.fulfillment.address.city).toBe(DEMO_PROFILE.city)
  // Ningún comando transporta evidencia de aprobación ni totales autoritativos.
  expect(JSON.stringify(turn.commands)).not.toContain("fingerprint")

  await card.getByTestId("approve-purchase").click()
  await expect(card).toHaveAttribute("data-status", "completed")
  await expect(page.getByText(/La compra quedó completada/)).toBeVisible()
  await expect(card.getByRole("link", { name: /Ver orden/ })).toBeVisible()

  // La aprobación va por REST con el fingerprint de los términos releídos.
  const approval = calls.find((call) => call.method === "POST" && call.path.endsWith("/approval"))
  expect(approval?.body).toEqual({ expected_fingerprint: APPROVAL.terms.fingerprint, checkout_jwt: null })
  // El seguimiento es el comando propio purchase-updated, sin datos de aprobación.
  expect(assistantBodies(calls)[1].commands).toEqual([{ type: "purchase-updated", runId: APPROVAL.run_id }])
  // Todo salvo /health lleva el token de la sesión de Better Auth.
  for (const call of calls.filter((call) => call.path !== "/health")) {
    expect(call.authorization, `${call.method} ${call.path}`).toBe(`Bearer ${BUYER_TOKEN}`)
  }
})

test("sin sesión muestra el acceso y no llama al agente", async ({ page }) => {
  await routeSession(page, { signedIn: false })
  const requests = agentRequests(page)
  await page.goto("/")
  await expect(page.getByRole("button", { name: "Entrar" })).toBeVisible()
  await expect(page.getByLabel("Mensaje de compra")).toHaveCount(0)
  expect(requests).toEqual([])
})

test.describe("móvil", () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test("la bienvenida y la tarjeta de aprobación caben sin scroll horizontal", async ({ page }) => {
    await routeSession(page)
    await routeAgent(page)
    await page.goto("/")
    await expect(page.getByText("¿Qué quieres comprar?")).toBeVisible()
    expect(await fitsViewport(page)).toBe(true)
    await preparePurchase(page)
    expect(await fitsViewport(page)).toBe(true)
  })
})
