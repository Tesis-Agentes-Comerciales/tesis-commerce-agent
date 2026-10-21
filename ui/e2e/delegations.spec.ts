import { readFileSync } from "node:fs"
import { expect, test, type Page } from "@playwright/test"

import { PURCHASE_REQUEST } from "../src/storybook/fixtures.ts"
import { AGENT_API_URL, fitsViewport, routeSession, UI_ORIGIN } from "./support.ts"

// «Delegaciones» (HNP-10) en el bundle de producción. Cada ruta de la API de delegaciones responde
// con una secuencia real del backend (src/storybook/fixtures/delegation-api/, generadas por
// scripts/export_delegation_replays.py), en el orden en que el backend la respondió. `POST
// /assistant` reproduce el stream real de una compra: de ahí salen las opciones del formulario.
// Ningún cuerpo se escribe aquí, salvo la lista con una delegación escalada (la fixture del
// contrato, delegation-escalated.json).

type Exchange = { method: string; path: string; request: unknown; status: number; body: unknown }

const REPLAYS = new URL("../src/storybook/fixtures/delegation-api/", import.meta.url)
const replay = (name: string): Exchange[] =>
  JSON.parse(readFileSync(new URL(`${name}.json`, REPLAYS), "utf8"))
const fixture = (name: string) =>
  JSON.parse(readFileSync(new URL(`../src/storybook/fixtures/${name}.json`, import.meta.url), "utf8"))
const PURCHASE_SSE = readFileSync(
  new URL("../src/storybook/fixtures/assistant-transport/purchase.sse", import.meta.url),
  "utf8",
)

const CONSENT = replay("consent")
const FLAG_OFF = replay("flag-off")

/** La respuesta número `nth` a `METHOD /ruta` de una secuencia. */
function answer(exchanges: Exchange[], route: string, nth = 0): Exchange {
  const found = exchanges.filter((item) => `${item.method} ${item.path}` === route)[nth]
  if (!found) throw new Error(`La secuencia no responde ${route} (#${nth})`)
  return found
}

const HEALTH_ON = answer(CONSENT, "GET /health")
const NO_DELEGATIONS = answer(CONSENT, "GET /me/delegations", 0)

const isDelegationRoute = (path: string) => /^\/(me\/)?delegations(\/|$)/.test(path)

type Call = { method: string; path: string; authorization: string | null; body: unknown }

/**
 * Agente simulado. `/health`, la compra pendiente y `/assistant` responden siempre lo mismo; cada
 * ruta de delegaciones consume, en orden, las respuestas de `exchanges` para su método y ruta. Una
 * llamada de delegaciones sin respuesta queda en `unexpected`, y cada prueba comprueba que no hubo
 * ninguna.
 */
async function routeAgent(page: Page, exchanges: Exchange[], health = HEALTH_ON) {
  const calls: Call[] = []
  const unexpected: string[] = []
  const pending = [...exchanges]
  const cors = {
    "Access-Control-Allow-Origin": UI_ORIGIN,
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  }
  await page.route(`${AGENT_API_URL}/**`, async (route) => {
    const request = route.request()
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors })
    const call: Call = {
      method: request.method(),
      path: new URL(request.url()).pathname,
      authorization: await request.headerValue("authorization"),
      body: request.postDataJSON(),
    }
    calls.push(call)
    const reply = (status: number, body: unknown) =>
      route.fulfill({ status, headers: cors, contentType: "application/json", body: JSON.stringify(body) })
    if (call.path === "/health") return reply(health.status, health.body)
    if (call.path === "/me/pending-purchase") return reply(200, null)
    if (call.path === "/assistant") {
      return route.fulfill({ headers: cors, contentType: "text/event-stream", body: PURCHASE_SSE })
    }
    // La traza de la compra no es parte de esto: responde como una compra que el agente no conoce.
    if (!isDelegationRoute(call.path)) return reply(404, { detail: "Not Found" })
    const index = pending.findIndex((item) => item.method === call.method && item.path === call.path)
    if (index < 0) {
      unexpected.push(`${call.method} ${call.path}`)
      return reply(404, { detail: "Not Found" })
    }
    const [next] = pending.splice(index, 1)
    return reply(next!.status, next!.body)
  })
  return { calls, unexpected }
}

const delegationCalls = (calls: Call[]) => calls.filter((call) => isDelegationRoute(call.path))

/** Pide la compra en el chat: el agente busca y muestra las opciones que luego se pueden delegar. */
async function searchInChat(page: Page) {
  const composer = page.getByLabel("Mensaje de compra")
  await composer.fill(PURCHASE_REQUEST)
  await composer.press("Enter")
  await expect(page.getByTestId("purchase-approval-card")).toBeVisible()
}

async function openDelegations(page: Page) {
  await page.getByRole("button", { name: "Delegaciones" }).click()
  const dialog = page.getByTestId("delegations")
  await expect(dialog.getByRole("heading", { name: "Mis delegaciones" })).toBeVisible()
  return dialog
}

/** Llena el formulario como en la secuencia `consent`: dos opciones, $2.000.000 y una hora. */
async function fillForm(page: Page, { city }: { city?: string } = {}) {
  const dialog = page.getByTestId("delegations")
  await dialog.getByRole("button", { name: "Nueva delegación" }).click()
  const form = dialog.getByTestId("delegation-form")
  await form.getByRole("checkbox", { name: /Bose QuietComfort Ultra negro/ }).check()
  await form.getByRole("checkbox", { name: /Sony WH-1000XM6 negro/ }).check()
  await form.getByLabel("Total máximo con envío (pesos)").fill("2.000.000")
  if (city !== undefined) await form.getByLabel("Ciudad").fill(city)
  await form
    .getByLabel("Tu indicación (opcional)")
    .fill("Si los dos están disponibles, prefiero los Sony.")
  await form.getByRole("button", { name: "Preparar la autorización" }).click()
  return form
}

const consentCard = (page: Page) => page.getByTestId("delegation-consent-card")

/**
 * Ancho del diálogo, cuánto se desplaza de lado y cuántos elementos se salen de él. Es texto para
 * el navegador: la configuración del E2E no incluye los tipos del DOM.
 */
const DIALOG_FITS = `(() => {
  const dialog = document.querySelector("[data-testid='delegations']")
  const box = dialog.getBoundingClientRect()
  const outside = [...dialog.querySelectorAll("*")].filter((child) => {
    const inner = child.getBoundingClientRect()
    return inner.width > 0 && (inner.left < box.left - 0.5 || inner.right > box.right + 0.5)
  })
  return { width: box.width, scroll: dialog.scrollWidth - dialog.clientWidth, outside: outside.length }
})()`

test.beforeEach(async ({ page }) => {
  // El feed sintético apunta a un dominio de ejemplo; ninguna prueba sale a la red.
  await page.route("https://merchant.example.test/**", (route) => route.abort())
  await routeSession(page)
})

test("propone y autoriza una delegación; el destino viaja tal como se escribió", async ({ page }) => {
  const { calls, unexpected } = await routeAgent(page, CONSENT.slice(1))
  await page.goto("/")
  await searchInChat(page)
  const dialog = await openDelegations(page)
  await expect(dialog.getByText("Todavía no tienes delegaciones.")).toBeVisible()

  await fillForm(page, { city: "Bogotá" })
  const card = consentCard(page)
  await expect(card).toBeVisible()
  // La vigencia efectiva: la propuesta vence antes que la delegación que crearía.
  const proposal = answer(CONSENT, "POST /delegations/proposals").body as { expires_at: string }
  await expect(card.locator("time", { hasText: /^Puedes autorizarla hasta el/ })).toHaveAttribute(
    "datetime",
    proposal.expires_at,
  )
  await expect(card).toContainText("Bogota, DC")
  await card.getByRole("button", { name: "Autorizar" }).click()

  await expect(consentCard(page)).toHaveCount(0)
  const status = dialog.getByTestId("delegation-status-card")
  await expect(status).toHaveCount(1)
  // Activa y sin tarea en curso: «Programada».
  await expect(status).toHaveAttribute("data-status", "scheduled")
  await expect(status.getByRole("button", { name: "Revocar", exact: true })).toBeVisible()

  // El cuerpo es el de la secuencia, con la ciudad tal como se escribió: «Bogotá».
  const sent = delegationCalls(calls)
  const proposed = sent.find((call) => call.path === "/delegations/proposals")
  expect(proposed?.body).toEqual(answer(CONSENT, "POST /delegations/proposals").request)
  expect(sent.find((call) => call.path === "/delegations")?.body).toEqual(
    answer(CONSENT, "POST /delegations").request,
  )
  expect(sent.map((call) => `${call.method} ${call.path}`)).toEqual([
    "GET /me/delegations",
    "POST /delegations/proposals",
    "POST /delegations",
    "GET /me/delegations",
  ])
  expect(sent.every((call) => call.authorization?.startsWith("Bearer "))).toBe(true)
  expect(unexpected).toEqual([])
})

for (const [name, code] of [
  ["scope-mismatch", "delegation_scope_mismatch"],
  ["proposal-expired", "delegation_proposal_expired"],
] as const) {
  test(`autorizar responde 409 ${code}: muestra el mensaje y pide otra propuesta`, async ({
    page,
  }) => {
    const exchanges = replay(name)
    const { calls, unexpected } = await routeAgent(page, [NO_DELEGATIONS, ...exchanges])
    await page.goto("/")
    await searchInChat(page)
    await openDelegations(page)
    await fillForm(page)

    const card = consentCard(page)
    const proposal = answer(exchanges, "POST /delegations/proposals").body as {
      expires_at: string
      scope: { expires_at: string }
    }
    const deadline =
      Date.parse(proposal.scope.expires_at) < Date.parse(proposal.expires_at)
        ? proposal.scope.expires_at
        : proposal.expires_at
    await expect(card.locator("time", { hasText: /^Puedes autorizarla hasta el/ })).toHaveAttribute(
      "datetime",
      deadline,
    )
    await card.getByRole("button", { name: "Autorizar" }).click()

    const failed = answer(exchanges, "POST /delegations").body as { message: string }
    await expect(card.getByRole("alert")).toHaveText(failed.message)
    // Reintentar no la arregla: «Autorizar» ya no actúa y «Cancelar» vuelve al formulario.
    const authorize = card.getByRole("button", { name: "Autorizar" })
    await expect(authorize).toHaveAttribute("aria-disabled", "true")
    await authorize.focus()
    await page.keyboard.press("Enter")
    await card.getByRole("button", { name: "Cancelar" }).click()
    await expect(page.getByTestId("delegation-form")).toBeVisible()
    await expect(page.getByLabel("Total máximo con envío (pesos)")).toHaveValue("2.000.000")

    expect(delegationCalls(calls).filter((call) => call.path === "/delegations")).toHaveLength(1)
    expect(unexpected).toEqual([])
  })
}

test("el comercio no entrega en ese destino (422): lo explica y conserva el formulario", async ({
  page,
}) => {
  const exchanges = replay("destination-unsupported")
  const { calls, unexpected } = await routeAgent(page, exchanges)
  await page.goto("/")
  await searchInChat(page)
  await openDelegations(page)
  const form = await fillForm(page, { city: "Medellín" })

  await expect(form.getByRole("alert")).toHaveText(
    "Por ahora el comercio solo entrega en Bogotá D.C. (departamento DC, código postal 110111). Revisa la dirección de entrega.",
  )
  await expect(form.getByLabel("Ciudad")).toHaveValue("Medellín")
  await expect(consentCard(page)).toHaveCount(0)
  const proposed = delegationCalls(calls).find((call) => call.path === "/delegations/proposals")
  expect(proposed?.body).toEqual(answer(exchanges, "POST /delegations/proposals").request)
  expect(unexpected).toEqual([])
})

test("revoca cada delegación y muestra qué pasó con el pago: sin efecto, liberada y en curso", async ({
  page,
}) => {
  const exchanges = replay("revoke")
  const { calls, unexpected } = await routeAgent(page, exchanges)
  await page.goto("/")
  const dialog = await openDelegations(page)
  const listed = (answer(exchanges, "GET /me/delegations").body as { delegations: { grant_id: string }[] })
    .delegations
  const cards = dialog.getByTestId("delegation-status-card")
  await expect(cards).toHaveCount(listed.length)

  const revoked = exchanges.filter((item) => item.path.endsWith("/revoke"))
  for (const exchange of revoked) {
    const grantId = exchange.path.split("/")[2]!
    const card = cards.nth(listed.findIndex((item) => item.grant_id === grantId))
    await card.getByRole("button", { name: "Revocar", exact: true }).click()
    const receipt = exchange.body as { effect: string; message: string }
    await expect(card.getByTestId("delegation-revoke-receipt")).toHaveAttribute("data-effect", receipt.effect)
    await expect(card.getByTestId("delegation-revoke-receipt")).toContainText(receipt.message)
    await expect(card).toHaveAttribute("data-status", "revoked")
    await expect(card.getByRole("button", { name: "Revocar", exact: true })).toHaveCount(0)
  }

  expect(revoked.map((item) => (item.body as { effect: string }).effect)).toEqual([
    "no_effect",
    "released",
    "in_flight",
  ])
  // Cada revocación relee su delegación; nada más.
  expect(delegationCalls(calls).map((call) => `${call.method} ${call.path}`)).toEqual(
    exchanges.map((item) => `${item.method} ${item.path}`),
  )
  expect(unexpected).toEqual([])
})

test("una delegación que ya terminó (409) se relee y deja de ofrecer «Revocar»", async ({ page }) => {
  const exchanges = replay("not-revocable")
  const { unexpected } = await routeAgent(page, exchanges)
  await page.goto("/")
  const dialog = await openDelegations(page)
  const card = dialog.getByTestId("delegation-status-card")
  await expect(card).toHaveAttribute("data-status", "scheduled")
  await card.getByRole("button", { name: "Revocar", exact: true }).click()

  const refused = exchanges.find((item) => item.status === 409)!.body as { message: string }
  await expect(card.getByRole("alert")).toHaveText(refused.message)
  await expect(card).toHaveAttribute("data-status", "expired")
  await expect(card.getByRole("button", { name: "Revocar", exact: true })).toHaveCount(0)
  expect(unexpected).toEqual([])
})

test("«Revocar todo» deja un recibo en cada delegación activa y relee la lista", async ({ page }) => {
  const exchanges = replay("revoke-all")
  const { calls, unexpected } = await routeAgent(page, exchanges)
  await page.goto("/")
  const dialog = await openDelegations(page)
  await expect(dialog.getByRole("button", { name: "Revocar", exact: true })).toHaveCount(3)
  await dialog.getByRole("button", { name: "Revocar todo" }).click()

  await expect(dialog.getByTestId("delegation-revoke-receipt")).toHaveCount(3)
  await expect(dialog.getByRole("button", { name: "Revocar todo" })).toHaveCount(0)
  await expect(dialog.getByRole("button", { name: "Revocar", exact: true })).toHaveCount(0)
  const cards = dialog.getByTestId("delegation-status-card")
  await expect(cards).toHaveCount(4)
  for (const card of await cards.all()) await expect(card).toHaveAttribute("data-status", "revoked")
  expect(delegationCalls(calls).map((call) => `${call.method} ${call.path}`)).toEqual([
    "GET /me/delegations",
    "POST /me/delegations/revoke-all",
    "GET /me/delegations",
  ])
  expect(unexpected).toEqual([])
})

test("una delegación escalada lleva su compra al compositor del chat", async ({ page }) => {
  const escalated = fixture("delegation-escalated")
  const { unexpected } = await routeAgent(page, [
    { method: "GET", path: "/me/delegations", request: null, status: 200, body: { delegations: [escalated] } },
  ])
  await page.goto("/")
  const dialog = await openDelegations(page)
  await dialog.getByRole("button", { name: "Comprar desde el chat" }).click()

  await expect(dialog).toBeHidden()
  const composer = page.getByLabel("Mensaje de compra")
  await expect(composer).toHaveValue(escalated.escalation.prefill_request)
  await expect(composer).toBeFocused()
  expect(unexpected).toEqual([])
})

test("con HNP apagado no hay «Delegaciones» ni llamadas a sus rutas", async ({ page }) => {
  const { calls, unexpected } = await routeAgent(page, [], answer(FLAG_OFF, "GET /health"))
  await page.goto("/")
  await searchInChat(page)
  await expect(page.getByRole("button", { name: "Delegaciones" })).toHaveCount(0)
  expect(calls.some((call) => call.path === "/health")).toBe(true)
  expect(delegationCalls(calls)).toEqual([])
  expect(unexpected).toEqual([])
})

test.describe("móvil", () => {
  test.use({ viewport: { width: 320, height: 640 } })

  test("a 320 px el diálogo, el formulario, la propuesta y la lista caben sin desbordar", async ({
    page,
  }) => {
    const revokeAll = replay("revoke-all")
    const { unexpected } = await routeAgent(page, [
      answer(revokeAll, "GET /me/delegations"),
      ...CONSENT.filter((item) => item.method === "POST"),
      answer(revokeAll, "GET /me/delegations", 1),
    ])
    await page.goto("/")
    await searchInChat(page)
    await openDelegations(page)
    const fits = () => page.evaluate(DIALOG_FITS)

    expect(await fits()).toEqual({ width: 288, scroll: 0, outside: 0 })
    await fillForm(page, { city: "Bogotá" })
    await expect(consentCard(page)).toBeVisible()
    expect(await fits()).toEqual({ width: 288, scroll: 0, outside: 0 })
    expect(await fitsViewport(page)).toBe(true)
    expect(unexpected).toEqual([])
  })
})
