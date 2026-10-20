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
const PURCHASE_CANCELED = stream("purchase-canceled")
const PURCHASE_REPEATED = stream("purchase-repeated")
const PURCHASE_BLOCKED = stream("purchase-blocked")

// Trazas de src/storybook/trace-fixtures (las mismas de Storybook y del contrato Pydantic), con el
// `run_id` del stream: la UI descarta una traza de otra compra.
const TRACES = new URL("../src/storybook/trace-fixtures/", import.meta.url)
type Trace = { run_id: string; policy_mode: string; status: string; steps: { kind: string }[] }
const trace = (name: string, runId: string): Trace => ({
  ...JSON.parse(readFileSync(new URL(`${name}.json`, TRACES), "utf8")),
  run_id: runId,
})

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

/** `run_id` de una compra que la política detuvo: solo viaja en los `args` de `prepare_checkout`. */
function blockedRun(sse: string) {
  const match = /"toolName": "prepare_checkout", "args": \{[^}]*"run_id": "(run_[0-9a-f]+)"/.exec(sse)
  if (!match?.[1]) throw Error("El stream no trae el run_id de prepare_checkout")
  return match[1]
}

const BLOCKED_RUN = blockedRun(PURCHASE_BLOCKED)
const NOTHING_CHARGED = "No se creó ningún checkout ni se hizo ningún cobro."

// Términos que ya no valen (S04): el backend rechaza la aprobación o la compra restaurada llega con
// `terms_status`. El `message` es el texto interno del backend, que la UI no debe mostrar.
const STALE_TERMS = {
  changed: {
    status: 409,
    body: { code: "checkout_terms_changed", message: "Live offer no longer matches the checkout terms" },
    title: "Las condiciones del comercio cambiaron",
  },
  unavailable: {
    status: 503,
    body: { code: "offer_freshness_unavailable", message: "Live offer could not be read" },
    title: "No pude confirmar las condiciones con el comercio",
  },
} as const

/** Compra pendiente restaurada sin términos, como la devuelve `GET /me/pending-purchase`. */
const stalePending = (termsStatus: keyof typeof STALE_TERMS) => ({
  purchase: {
    run_id: APPROVAL.run_id,
    stage: "awaiting_confirmation",
    checkout_id: APPROVAL.terms.checkout_id,
    order_id: null,
  },
  approval_terms: null,
  terms_status: termsStatus,
})

/** Índice de la primera llamada que cumple la condición, o -1. */
const callIndex = (calls: AgentCall[], method: string, suffix: string) =>
  calls.findIndex((call) => call.method === method && call.path.endsWith(suffix))

type Phase = "awaiting" | "completed"

/**
 * La compra cambia como en el backend: espera la aprobación hasta que se completa por REST. El
 * stream simulado llega de una vez, así que no hay un tramo «en curso» que observar aquí; ese estado
 * lo cubren las historias de `PurchaseActivity`.
 */
function purchaseTrace(phaseTrace: (phase: Phase) => Trace): TraceRoute {
  return (calls) => ({
    body: phaseTrace(callIndex(calls, "POST", "/complete") >= 0 ? "completed" : "awaiting"),
  })
}

const shadowTrace = (phase: Phase) =>
  trace(phase === "completed" ? "completed-shadow" : "awaiting-approval", APPROVAL.run_id)

/** Política desactivada: la espera es la misma traza sin el paso de política. */
function policyOffTrace(phase: Phase): Trace {
  if (phase === "completed") return trace("policy-off", APPROVAL.run_id)
  const awaiting = shadowTrace("awaiting")
  return {
    ...awaiting,
    policy_mode: "off",
    steps: awaiting.steps.filter((step) => step.kind !== "policy_decision"),
  }
}

type AssistantBody = {
  turn_id: string
  commands: { type: string; runId?: string; message?: { parts: { text: string }[] } }[]
  details: {
    buyer: Record<string, string>
    fulfillment: { address: Record<string, string> }
  }
  state: { messages: unknown[]; purchase: { run_id: string } | null }
}

type AgentCall = { method: string; path: string; authorization: string | null; body: unknown }

/** Respuesta de `GET /purchases/{run_id}/trace` según las llamadas hechas hasta ahora (incluida esta). */
type TraceRoute = (calls: AgentCall[]) => { status?: number; body: unknown }

type AgentOptions = {
  /** Stream de cada mensaje del comprador, en orden: por defecto, la compra preparada. */
  turns?: string[]
  /** Sin ella, la traza responde 404, como una compra que el agente no conoce. */
  trace?: TraceRoute
  /** `GET /me/pending-purchase` al cargar: por defecto, ninguna compra pendiente. */
  pending?: unknown
  /** `POST /purchases/{run_id}/approval`: por defecto, la aprobación registrada. */
  approval?: { status: number; body: unknown }
  /** Respuesta a `purchase-updated` después de cancelar: por defecto, `purchase-canceled.sse`. */
  canceled?: (body: AssistantBody) => string
}

const RUN_SEGMENT = /\/run_[A-Za-z0-9_-]+(?=\/|$)/

/** Agente simulado: registra cada llamada para revisar cuerpos y cabeceras. */
async function routeAgent(
  page: Page,
  { turns = [PURCHASE], trace, pending = null, approval, canceled = () => PURCHASE_CANCELED }: AgentOptions = {},
) {
  const calls: AgentCall[] = []
  let turn = 0
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
    switch (`${call.method} ${call.path.replace(RUN_SEGMENT, "/:runId")}`) {
      case "GET /health":
        return json({ status: "ok" })
      case "GET /me/pending-purchase":
        return route.fulfill({ headers: cors, contentType: "application/json", body: JSON.stringify(pending) })
      case "POST /assistant": {
        // Cada stream continúa el estado del anterior, así que encajan en el orden del fixture.
        const body = call.body as AssistantBody
        const followUp = body.commands.at(-1)?.type === "purchase-updated"
        const wasCanceled = callIndex(calls, "POST", "/cancel") >= 0
        return route.fulfill({
          headers: cors,
          contentType: "text/event-stream",
          body: followUp ? (wasCanceled ? canceled(body) : PURCHASE_COMPLETED) : turns[turn++],
        })
      }
      case "GET /purchases/:runId/trace": {
        if (!trace) break
        const reply = trace(calls)
        return route.fulfill({ status: reply.status ?? 200, headers: cors, json: reply.body })
      }
      case "GET /purchases/:runId/approval":
        return json(APPROVAL.terms)
      case "POST /purchases/:runId/approval":
        if (approval) return route.fulfill({ status: approval.status, headers: cors, json: approval.body })
        return json(approvalRecord(APPROVAL.run_id))
      case "POST /purchases/:runId/cancel": {
        const runId = /run_[A-Za-z0-9_-]+/.exec(call.path)?.[0]
        return json({ run_id: runId, stage: "canceled", checkout_id: APPROVAL.terms.checkout_id, order_id: null })
      }
      case "POST /purchases/:runId/complete":
        return json(COMPLETED)
    }
    return route.fulfill({ status: 404, headers: cors, json: { code: "not_found", message: "not found" } })
  })
  return calls
}

/**
 * `purchase-updated` sobre un estado que no sale de los fixtures (una compra restaurada): el mismo
 * estado con la etapa cancelada, como lo devolvería el backend antes de su mensaje de seguimiento.
 */
function canceledEcho({ state }: AssistantBody) {
  const value = { ...state, purchase: { ...state.purchase, stage: "canceled" } }
  const frame = { type: "update-state", operations: [{ type: "set", path: [], value }] }
  return `data: ${JSON.stringify(frame)}\n\ndata: [DONE]\n\n`
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

test.describe("actividad de la compra", () => {
  test("modo observando: la fila pasa de esperar la aprobación a completada y muestra la política permitida con sus dos reglas", async ({
    page,
  }) => {
    await routeSession(page)
    const calls = await routeAgent(page, {
      trace: purchaseTrace(shadowTrace),
    })
    await page.goto("/")
    const card = await preparePurchase(page)
    const activity = page.getByTestId("purchase-activity")
    await expect(activity).toHaveAttribute("data-status", "awaiting_approval")
    await expect(activity.getByRole("button", { name: /Esperando tu aprobación/ })).toBeVisible()
    expect(callIndex(calls, "GET", `/purchases/${APPROVAL.run_id}/trace`)).toBeGreaterThanOrEqual(0)

    await card.getByTestId("approve-purchase").click()
    await expect(card).toHaveAttribute("data-status", "completed")
    await expect(activity).toHaveAttribute("data-status", "completed")
    const trigger = activity.getByRole("button", { name: /Compra completada en \d+ s/ })
    await expect(trigger).toBeVisible()
    // Se relee después de completar, y el recibo sale de esa traza.
    const completed = callIndex(calls, "POST", "/complete")
    expect(calls.slice(completed).some((call) => call.path.endsWith("/trace"))).toBe(true)
    await expect(card.getByTestId("approval-receipt")).toHaveText(/^Aprobado por ti · \d{2}:\d{2}/)

    await trigger.click()
    await expect(activity.getByText("Política de compra: permitida")).toBeVisible()
    await expect(activity.getByText("Solo observando: registra, no detiene")).toBeVisible()
    const rules = activity.getByRole("list", { name: "Reglas de la política de compra" })
    await expect(rules.getByRole("listitem")).toHaveCount(2)
    await expect(rules.getByText("Producto dentro de lo que pediste")).toBeVisible()
    await expect(rules.getByText("Comercio permitido")).toBeVisible()
    await expect(rules.getByText("Cumple")).toHaveCount(2)
  })

  test("modo activo: el bloqueo por el comercio muestra su tarjeta y ningún error inesperado", async ({
    page,
  }) => {
    await routeSession(page)
    const calls = await routeAgent(page, {
      turns: [PURCHASE_BLOCKED],
      trace: () => ({ body: trace("blocked-merchant", BLOCKED_RUN) }),
    })
    await page.goto("/")
    const composer = page.getByLabel("Mensaje de compra")
    await composer.fill(PURCHASE_REQUEST)
    await composer.press("Enter")

    const card = page.getByTestId("policy-blocked-card")
    await expect(card).toBeVisible()
    await expect(card.getByRole("heading", { name: "La política de compra detuvo esta compra" })).toBeVisible()
    await expect(card.getByText("Comercio permitido")).toBeVisible()
    await expect(card.getByText("https://tienda-aliada.example.test")).toBeVisible()
    await expect(card.getByText(NOTHING_CHARGED)).toBeVisible()
    await expect(page.getByTestId("purchase-activity")).toHaveAttribute("data-status", "blocked")
    await expect(page.getByText(/error inesperado/i)).toHaveCount(0)
    // Un bloqueo no es un error: ni aviso rojo ni tarjeta de aprobación.
    await expect(page.getByRole("alert")).toHaveCount(0)
    await expect(page.getByTestId("purchase-approval-card")).toHaveCount(0)
    expect(calls.some((call) => call.path === `/purchases/${BLOCKED_RUN}/trace`)).toBe(true)
  })

  test("política desactivada: la compra se completa y el control figura como desactivado", async ({
    page,
  }) => {
    await routeSession(page)
    await routeAgent(page, { trace: purchaseTrace(policyOffTrace) })
    await page.goto("/")
    const card = await preparePurchase(page)
    await card.getByTestId("approve-purchase").click()
    await expect(card).toHaveAttribute("data-status", "completed")
    await expect(page.getByText(/La compra quedó completada/)).toBeVisible()

    const activity = page.getByTestId("purchase-activity")
    await activity.getByRole("button", { name: /Compra completada/ }).click()
    const policy = activity.getByRole("listitem").filter({ hasText: "Política desactivada" })
    await expect(policy).toBeVisible()
    await expect(policy).toContainText("No se revisó esta compra")
    await expect(activity.getByText("Orden confirmada")).toBeVisible()
  })

  for (const status of [404, 503]) {
    test(`una traza que responde ${status} no rompe la compra`, async ({ page }) => {
      await routeSession(page)
      await routeAgent(page, { trace: () => ({ status, body: { detail: "unavailable" } }) })
      await page.goto("/")
      const card = await preparePurchase(page)
      await card.getByTestId("approve-purchase").click()
      await expect(card).toHaveAttribute("data-status", "completed")
      await expect(page.getByText(/La compra quedó completada/)).toBeVisible()
      await expect(page.getByTestId("purchase-activity")).toHaveCount(0)
      await expect(page.getByRole("alert")).toHaveCount(0)
    })
  }
})

test.describe("términos que ya no valen", () => {
  for (const termsStatus of ["changed", "unavailable"] as const) {
    const stale = STALE_TERMS[termsStatus]
    test(`aprobar responde ${stale.status} ${stale.body.code}: sin cobro, se cancela y se pide de nuevo`, async ({
      page,
    }) => {
      await routeSession(page)
      const calls = await routeAgent(page, {
        turns: [PURCHASE, PURCHASE_REPEATED],
        approval: { status: stale.status, body: stale.body },
      })
      await page.goto("/")
      const card = await preparePurchase(page)
      await card.getByTestId("approve-purchase").click()

      // La tarjeta de aprobación cede su lugar: sin cobro y con una sola salida.
      const staleCard = page.getByTestId("stale-terms-card")
      await expect(staleCard).toHaveAttribute("data-status", "stale")
      await expect(staleCard.getByRole("heading", { name: stale.title })).toBeVisible()
      await expect(staleCard.getByText("No se hizo ningún cobro.", { exact: true })).toBeVisible()
      await expect(staleCard.getByRole("button")).toHaveText(["Cancelar compra"])
      await expect(page.getByTestId("purchase-approval-card")).toHaveCount(0)
      await expect(page.getByText(/recarga la página|no longer|could not/i)).toHaveCount(0)
      await expect(page.getByRole("alert")).toHaveCount(0)
      // Ni el plan ni la actividad siguen esperando una aprobación que ya no es posible.
      await expect(page.getByText("Esperando tu aprobación")).toHaveCount(0)

      await staleCard.getByRole("button", { name: "Cancelar compra" }).click()
      await expect(staleCard).toHaveAttribute("data-status", "canceled")
      await expect(page.getByText(/Cancelé la compra pendiente/)).toBeVisible()
      expect(callIndex(calls, "POST", `/purchases/${APPROVAL.run_id}/cancel`)).toBeGreaterThanOrEqual(0)
      expect(callIndex(calls, "POST", "/complete")).toBe(-1)

      // Pedirla de nuevo abre otra compra, con su propio checkout y términos por aprobar.
      const composer = page.getByLabel("Mensaje de compra")
      await composer.fill(PURCHASE_REQUEST)
      await composer.press("Enter")
      const again = page.getByTestId("purchase-approval-card")
      await expect(again).toHaveAttribute("data-status", "awaiting_approval")
      await expect(again.getByTestId("approve-purchase")).toBeEnabled()
      await expect(staleCard).toHaveAttribute("data-status", "canceled")
      expect(assistantBodies(calls).map((body) => body.commands.at(-1)?.type)).toEqual([
        "add-message",
        "purchase-updated",
        "add-message",
      ])
    })

    if (termsStatus === "changed") {
      test("aprobar responde 409: la actividad muestra que la frescura detuvo la aprobación y qué cambió", async ({
        page,
      }) => {
        await routeSession(page)
        // La traza se relee tras el 409: la frescura de la oferta, no la política, detuvo la compra.
        await routeAgent(page, {
          approval: { status: stale.status, body: stale.body },
          trace: (calls) => ({
            body: trace(
              callIndex(calls, "POST", "/approval") >= 0 ? "stale-terms" : "awaiting-approval",
              APPROVAL.run_id,
            ),
          }),
        })
        await page.goto("/")
        const card = await preparePurchase(page)
        await card.getByTestId("approve-purchase").click()

        const activity = page.getByTestId("purchase-activity")
        await expect(activity).toHaveAttribute("data-status", "stale_terms")
        await expect(page.getByTestId("stale-terms-card").getByRole("button")).toHaveText(["Cancelar compra"])
        await activity.getByRole("button", { name: /Detenida: cambiaron las condiciones/ }).click()
        const blocked = activity.getByRole("listitem").filter({ hasText: "Las condiciones del comercio cambiaron" })
        await expect(blocked).toHaveAttribute("data-state", "blocked")
        await expect(blocked).toContainText("Tu aprobación no se registró y no se firmó ninguna autorización.")
        const total = blocked.getByRole("list", { name: "Qué cambió" }).getByRole("listitem").filter({ hasText: "Total" })
        await expect(total).toContainText(/En el checkout\$\s?1\.924\.000(,00)? COP/)
        await expect(total).toContainText(/Oferta actual\$\s?2\.024\.000(,00)? COP/)
        await expect(activity.getByText("Condiciones confirmadas con el comercio")).toBeVisible()
        await expect(activity.getByText("La política detuvo la compra")).toHaveCount(0)
        await expect(page.getByText("Esperando tu aprobación")).toHaveCount(0)
        await expect(page.getByRole("alert")).toHaveCount(0)
      })
    }

    test(`una compra pendiente con terms_status "${termsStatus}" se puede cancelar tras recargar`, async ({
      page,
    }) => {
      await routeSession(page)
      const calls = await routeAgent(page, { pending: stalePending(termsStatus), canceled: canceledEcho })
      await page.goto("/")
      await expect(page.getByText("Recuperé tu compra pendiente. Revisa su estado antes de continuar.")).toBeVisible()
      const staleCard = page.getByTestId("stale-terms-card")
      await expect(staleCard.getByRole("heading", { name: stale.title })).toBeVisible()
      await expect(staleCard.getByText("No se hizo ningún cobro.", { exact: true })).toBeVisible()
      await expect(staleCard.getByRole("button")).toHaveText(["Cancelar compra"])
      await expect(page.getByRole("alert")).toHaveCount(0)

      await staleCard.getByRole("button", { name: "Cancelar compra" }).click()
      await expect(staleCard).toHaveAttribute("data-status", "canceled")
      await expect.poll(() => assistantBodies(calls).length).toBe(1)
      expect(assistantBodies(calls)[0].commands).toEqual([{ type: "purchase-updated", runId: APPROVAL.run_id }])
      expect(calls.filter((call) => call.method === "POST" && call.path.endsWith("/cancel"))).toHaveLength(1)
    })
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

  test("la tarjeta de bloqueo cabe en 375 px", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await routeSession(page)
    await routeAgent(page, {
      turns: [PURCHASE_BLOCKED],
      trace: () => ({ body: trace("blocked-merchant", BLOCKED_RUN) }),
    })
    await page.goto("/")
    const composer = page.getByLabel("Mensaje de compra")
    await composer.fill(PURCHASE_REQUEST)
    await composer.press("Enter")
    await expect(page.getByTestId("policy-blocked-card")).toBeVisible()
    expect(await fitsViewport(page)).toBe(true)
  })

  test("la tarjeta de términos que cambiaron cabe en 375 px", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await routeSession(page)
    await routeAgent(page, { pending: stalePending("unavailable") })
    await page.goto("/")
    await expect(page.getByTestId("stale-terms-card")).toBeVisible()
    expect(await fitsViewport(page)).toBe(true)
  })

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
