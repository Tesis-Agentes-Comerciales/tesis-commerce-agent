import { readFileSync } from "node:fs"
import { expect, test, type Page } from "@playwright/test"

import { PURCHASE_REQUEST } from "../src/storybook/fixtures.ts"
import { AGENT_API_URL, fitsViewport, routeSession, UI_ORIGIN } from "./support.ts"

// La compra delegada (HNP) en el bundle de producción: en «Delegar» el chat busca y la delegación
// se prepara, se autoriza y se sigue en el hilo (docs/design/hnp-chat-delegate.md); «Mis
// delegaciones» guarda la lista y la revocación. Cada ruta de la API de delegaciones responde con
// una secuencia real del backend (src/storybook/fixtures/delegation-api/, generadas por
// scripts/export_delegation_replays.py), en el orden en que el backend la respondió. `POST
// /assistant` reproduce el stream real del backend: `delegate.sse` en «Delegar» (de ahí sale el
// borrador del formulario) y `purchase.sse` en «Comprar conmigo». Ningún cuerpo se escribe aquí,
// salvo la lista con una delegación escalada (la fixture del contrato, delegation-escalated.json) y,
// para ver terminar una compra delegada, la delegación consentida con la tarea de las fixtures del
// contrato (delegation-running.json y delegation-purchased.json) y su traza HNP
// (trace-fixtures/hnp/enforce-completed.json).

type Exchange = { method: string; path: string; request: unknown; status: number; body: unknown }
type View = { grant_id: string; state: string; task: Record<string, unknown> }

const REPLAYS = new URL("../src/storybook/fixtures/delegation-api/", import.meta.url)
const replay = (name: string): Exchange[] =>
  JSON.parse(readFileSync(new URL(`${name}.json`, REPLAYS), "utf8"))
const fixture = (name: string) =>
  JSON.parse(readFileSync(new URL(`../src/storybook/${name}.json`, import.meta.url), "utf8"))
const sse = (name: string) =>
  readFileSync(new URL(`../src/storybook/fixtures/assistant-transport/${name}.sse`, import.meta.url), "utf8")
const PURCHASE_SSE = sse("purchase")
const DELEGATE_SSE = sse("delegate")

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
const LIST = "GET /me/delegations"
const isRead = (item: Exchange) => item.method === "GET"

type Call = { method: string; path: string; authorization: string | null; body: unknown }

/**
 * Lo que el backend respondería ahora a una lectura. Las escrituras (POST) de la secuencia se
 * consumen en orden; una lectura ve el estado que dejaron las escrituras ya hechas: la última lista
 * de la secuencia hasta la próxima escritura pendiente, con las delegaciones releídas después. Así
 * el sondeo de «Mis delegaciones» (cada 2 s) puede releer cuantas veces quiera sin adelantarse ni
 * volver atrás.
 */
function currentRead(log: Exchange[], served: ReadonlySet<number>, route: string) {
  const pending = log.findIndex((item, index) => !isRead(item) && !served.has(index))
  const reached = pending < 0 ? log : log.slice(0, pending)
  let found: Exchange | null = null
  let delegations: View[] | null = null
  for (const item of reached) {
    const key = `${item.method} ${item.path}`
    if (key === LIST && item.status === 200) {
      delegations = [...(item.body as { delegations: View[] }).delegations]
    } else if (isRead(item) && item.status === 200 && delegations) {
      const view = item.body as View
      delegations = delegations.map((known) => (known.grant_id === view.grant_id ? view : known))
    }
    if (key === route) found = item
  }
  // La tarjeta del hilo relee su delegación: la vista de ese momento en la lista, si no hay otra.
  const grant = /^GET \/delegations\/([^/]+)$/.exec(route)?.[1]
  const listed = grant ? delegations?.find((view) => view.grant_id === grant) : undefined
  if (!found) return listed ? { status: 200, body: listed } : null
  if (route !== LIST || found.status !== 200) return { status: found.status, body: found.body }
  return { status: 200, body: { delegations } }
}

/**
 * Agente simulado. `/health`, la compra pendiente y `/assistant` responden siempre lo mismo; cada
 * escritura de delegaciones consume, en orden, la respuesta de `exchanges` para su método y ruta, y
 * cada lectura responde el estado de ese momento (`currentRead`). `publish` agrega lecturas como si
 * la tarea hubiera avanzado en el backend, y `traces` responde la traza de cada compra (sin ella,
 * 404). Una llamada de delegaciones sin respuesta queda en `unexpected`, y cada prueba comprueba
 * que no hubo ninguna.
 */
async function routeAgent(page: Page, exchanges: Exchange[], health = HEALTH_ON) {
  const calls: Call[] = []
  const unexpected: string[] = []
  const log = [...exchanges]
  const served = new Set<number>()
  const traces = new Map<string, unknown>()
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
      const delegate = (call.body as { mode?: string } | null)?.mode === "delegate"
      const body = delegate ? DELEGATE_SSE : PURCHASE_SSE
      return route.fulfill({ headers: cors, contentType: "text/event-stream", body })
    }
    const traced = /^\/purchases\/([^/]+)\/trace$/.exec(call.path)
    if (traced && traces.has(traced[1]!)) return reply(200, traces.get(traced[1]!))
    // Ninguna otra compra es parte de esto: responde como una que el agente no conoce.
    if (!isDelegationRoute(call.path)) return reply(404, { detail: "Not Found" })
    const key = `${call.method} ${call.path}`
    if (call.method === "GET") {
      const read = currentRead(log, served, key)
      if (read) return reply(read.status, read.body)
    } else {
      const index = log.findIndex(
        (item, position) => !served.has(position) && `${item.method} ${item.path}` === key,
      )
      if (index >= 0) {
        served.add(index)
        return reply(log[index]!.status, log[index]!.body)
      }
    }
    unexpected.push(key)
    return reply(404, { detail: "Not Found" })
  })
  /** El backend avanzó: estas lecturas responden desde ahora. */
  const publish = (...reads: Exchange[]) => log.push(...reads)
  return { calls, unexpected, traces, publish }
}

/**
 * Las llamadas de delegaciones sin las relecturas del sondeo: una lista que se lee seguida varias
 * veces cuenta una, como la que la app pide después de cada acción.
 */
const withoutPolls = (calls: string[]) =>
  calls.filter((call, index) => !(call === LIST && calls[index - 1] === LIST))

const delegationCalls = (calls: Call[]) => calls.filter((call) => isDelegationRoute(call.path))

/** El modo de compra de la cabecera: el radio se elige con un clic en su segmento. */
const modeSwitch = (page: Page) => page.getByRole("group", { name: "Modo de compra" })

/** La compra delegada del hilo, al pie de los mensajes. */
const inChat = (page: Page) => page.getByTestId("delegation-in-chat")

/** El plan de la compra con el comprador (HP): Entender … Aprobación y Completar. */
const purchasePlan = (page: Page) => page.getByRole("region", { name: "Plan de compra" })

/**
 * En «Delegar», pide la compra en el chat: el agente busca, no crea checkout y deja debajo el
 * formulario prellenado con las opciones, el presupuesto del mensaje y el mensaje como indicación.
 */
async function delegateInChat(page: Page) {
  await modeSwitch(page).getByText("Delegar", { exact: true }).click()
  await expect(page.getByRole("radio", { name: "Delegar" })).toBeChecked()
  await expect(page.getByRole("dialog")).toHaveCount(0)
  const composer = page.getByLabel("Mensaje de compra")
  await composer.fill(PURCHASE_REQUEST)
  await composer.press("Enter")
  const form = inChat(page).getByTestId("delegation-form")
  await expect(form).toBeVisible()
  await expect(page.getByTestId("purchase-approval-card")).toHaveCount(0)
  return form
}

/** «Mis delegaciones»: el botón de la cabecera abre la lista. */
async function openDelegations(page: Page) {
  await page.getByRole("button", { name: "Mis delegaciones" }).click()
  const dialog = page.getByRole("dialog", { name: "Mis delegaciones" })
  await expect(dialog).toBeVisible()
  return dialog
}

/**
 * Del formulario prellenado a la secuencia `consent`: las tres opciones marcadas y el total con el
 * presupuesto del mensaje (2.000.000); queda Bose y Sony, una hora y la indicación de la secuencia.
 */
async function fillForm(page: Page, { city, submit = true }: { city?: string; submit?: boolean } = {}) {
  const form = await delegateInChat(page)
  for (const box of await form.getByRole("checkbox").all()) await expect(box).toBeChecked()
  await expect(form.getByLabel("Total máximo con envío (pesos)")).toHaveValue("2.000.000")
  await expect(form.getByLabel("Tu indicación (opcional)")).toHaveValue(PURCHASE_REQUEST)
  await form.getByRole("checkbox", { name: /Sennheiser/ }).uncheck()
  if (city !== undefined) await form.getByLabel("Ciudad").fill(city)
  await form
    .getByLabel("Tu indicación (opcional)")
    .fill("Si los dos están disponibles, prefiero los Sony.")
  if (submit) await form.getByRole("button", { name: "Preparar la autorización" }).click()
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

/** Lo mismo para la tarjeta del hilo: nada se sale de la ventana ni obliga a desplazar de lado. */
const THREAD_FITS = `(() => {
  const section = document.querySelector("[data-testid='delegation-in-chat']")
  const outside = [...section.querySelectorAll("*")].filter((child) => {
    const inner = child.getBoundingClientRect()
    return inner.width > 0 && (inner.left < -0.5 || inner.right > window.innerWidth + 0.5)
  })
  return { scroll: section.scrollWidth - section.clientWidth, outside: outside.length }
})()`

test.beforeEach(async ({ page }) => {
  // El feed sintético apunta a un dominio de ejemplo; ninguna prueba sale a la red.
  await page.route("https://merchant.example.test/**", (route) => route.abort())
  await routeSession(page)
})

test("en «Delegar» el chat busca y la delegación se autoriza en el hilo; el destino viaja tal cual", async ({
  page,
}) => {
  const { calls, unexpected } = await routeAgent(page, CONSENT.slice(1))
  await page.goto("/")
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
  const status = inChat(page).getByTestId("delegation-status-card")
  await expect(status).toHaveCount(1)
  // Activa y sin tarea en curso: «Programada».
  await expect(status).toHaveAttribute("data-status", "scheduled")
  // La revocación sigue en «Mis delegaciones».
  await expect(status.getByRole("button", { name: "Revocar", exact: true })).toHaveCount(0)

  // El turno fue «Delegar»: el chat no creó ningún checkout.
  const turns = calls.filter((call) => call.path === "/assistant")
  expect(turns.map((call) => (call.body as { mode?: string }).mode)).toEqual(["delegate"])
  expect(calls.some((call) => call.path.startsWith("/purchases/"))).toBe(false)

  // El cuerpo es el de la secuencia, con la ciudad tal como se escribió: «Bogotá».
  const sent = delegationCalls(calls)
  const proposed = sent.find((call) => call.path === "/delegations/proposals")
  expect(proposed?.body).toEqual(answer(CONSENT, "POST /delegations/proposals").request)
  expect(sent.find((call) => call.path === "/delegations")?.body).toEqual(
    answer(CONSENT, "POST /delegations").request,
  )
  // Activa, la tarjeta relee su delegación cada 2 s hasta que la tarea termine: eso no cuenta aquí.
  const grant = `/delegations/${GRANTED.grant_id}`
  expect(withoutPolls(sent.map((call) => `${call.method} ${call.path}`)).filter((call) => call !== `GET ${grant}`)).toEqual([
    "POST /delegations/proposals",
    "POST /delegations",
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
    await expect(inChat(page).getByTestId("delegation-form")).toBeVisible()
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
  // Cada revocación relee su delegación; nada más. Con compras en curso, la lista se relee aparte.
  const sent = delegationCalls(calls).map((call) => `${call.method} ${call.path}`)
  const actions = (routes: string[]) => routes.filter((route) => route !== LIST)
  expect(sent[0]).toBe(LIST)
  expect(actions(sent)).toEqual(actions(exchanges.map((item) => `${item.method} ${item.path}`)))
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
  expect(withoutPolls(delegationCalls(calls).map((call) => `${call.method} ${call.path}`))).toEqual([
    "GET /me/delegations",
    "POST /me/delegations/revoke-all",
    "GET /me/delegations",
  ])
  expect(unexpected).toEqual([])
})

test("una delegación escalada lleva su compra al compositor del chat", async ({ page }) => {
  const escalated = fixture("fixtures/delegation-escalated")
  const { unexpected } = await routeAgent(page, [
    { method: "GET", path: "/me/delegations", request: null, status: 200, body: { delegations: [escalated] } },
  ])
  await page.goto("/")
  await modeSwitch(page).getByText("Delegar", { exact: true }).click()
  const dialog = await openDelegations(page)
  await dialog.getByRole("button", { name: "Comprar desde el chat" }).click()

  await expect(dialog).toBeHidden()
  const composer = page.getByLabel("Mensaje de compra")
  await expect(composer).toHaveValue(escalated.escalation.prefill_request)
  await expect(composer).toBeFocused()
  // La decisión vuelve al comprador: la compra sigue en «Comprar conmigo».
  await expect(page.getByRole("radio", { name: "Comprar conmigo" })).toBeChecked()
  expect(unexpected).toEqual([])
})

test("con HNP apagado la cabecera es la de siempre: sin modo de compra ni llamadas a sus rutas", async ({
  page,
}) => {
  const { calls, unexpected } = await routeAgent(page, [], answer(FLAG_OFF, "GET /health"))
  await page.goto("/")
  const composer = page.getByLabel("Mensaje de compra")
  await composer.fill(PURCHASE_REQUEST)
  await composer.press("Enter")
  await expect(page.getByTestId("purchase-approval-card")).toBeVisible()
  await expect(modeSwitch(page)).toHaveCount(0)
  await expect(page.getByRole("radio")).toHaveCount(0)
  await expect(page.getByRole("button", { name: "Mis delegaciones" })).toHaveCount(0)
  // Sin HNP el cuerpo de `/assistant` es el de siempre: sin `mode`.
  const turn = calls.find((call) => call.path === "/assistant")?.body as Record<string, unknown>
  expect(turn).not.toHaveProperty("mode")
  // La misma cabecera que antes de HNP: sus clases, «Salir» y los datos de entrega.
  const header = page.locator("header")
  await expect(header).toHaveClass("flex h-14 shrink-0 items-center justify-end gap-2 px-3 sm:px-5")
  await expect(header.getByRole("button")).toHaveCount(2)
  await expect(header.getByRole("button", { name: "Salir" })).toBeVisible()
  await expect(header.getByRole("button", { name: "Datos de entrega" })).toBeVisible()
  expect(calls.some((call) => call.path === "/health")).toBe(true)
  expect(delegationCalls(calls)).toEqual([])
  expect(unexpected).toEqual([])
})

// Una compra delegada de punta a punta: «Delegar», autorizar, el disparo y la gobernanza en la tarjeta.

const GRANTED = answer(CONSENT, "POST /delegations").body as View
const RUNNING_TASK = fixture("fixtures/delegation-running").task as Record<string, unknown>
const PURCHASED = fixture("fixtures/delegation-purchased") as View
const COMPLETED_TRACE = fixture("trace-fixtures/hnp/enforce-completed") as {
  steps: { at: string }[]
}
const RUN_ID = RUNNING_TASK.run_id as string

/** La delegación consentida con su tarea en otro momento, como la relee el backend. */
function consentedWith(state: string, task: Record<string, unknown>): Exchange {
  const view = { ...GRANTED, state, task: { ...task, task_id: GRANTED.task.task_id }, last_activity_at: task.updated_at }
  return { method: "GET", path: "/me/delegations", request: null, status: 200, body: { delegations: [view] } }
}

/** La traza a mitad de camino: D2 ya verificó; faltan la reserva, el cierre, D3 y la orden. */
const RUNNING_TRACE = {
  ...COMPLETED_TRACE,
  run_id: RUN_ID,
  status: "in_progress",
  updated_at: COMPLETED_TRACE.steps[5]!.at,
  steps: COMPLETED_TRACE.steps.slice(0, 6),
}

/** Los controles que la tarjeta muestra de la compra completada, en orden. */
const GOVERNED_STEPS = [
  "Delegación autorizada",
  "Compra delegada en marcha",
  "Política de compra: permitida",
  "Checkout creado en el comercio",
  "Oferta vigente al cerrar",
  "Autorización delegada verificada",
  "Uso único reservado",
  "Autorización cerrada por el agente",
  "Despacho autorizado",
  "Pago autorizado",
  "Orden confirmada",
]

/**
 * Elige «Delegar», pide la compra, autoriza y, sin salir del hilo, el backend dispara la tarea y la
 * compra termina: la tarjeta pasa sola de activa a en curso y a completada, con su gobernanza.
 * Terminada, nada se sigue releyendo.
 */
async function delegateAndWatchTheGovernance(page: Page, check: (step: string) => Promise<void>) {
  const backend = await routeAgent(page, CONSENT.slice(1))
  await page.goto("/")
  const form = await fillForm(page, { city: "Bogotá", submit: false })
  // «Preparar la autorización» entero dentro del formulario (#134, brecha 4).
  const submit = form.getByRole("button", { name: "Preparar la autorización" })
  const [formBox, submitBox] = [await form.boundingBox(), await submit.boundingBox()]
  expect(submitBox!.x + submitBox!.width).toBeLessThanOrEqual(formBox!.x + formBox!.width)
  await check("formulario")
  await submit.click()
  await check("consentimiento")
  await consentCard(page).getByRole("button", { name: "Autorizar" }).click()
  const status = inChat(page).getByTestId("delegation-status-card")
  await expect(status).toHaveAttribute("data-status", "scheduled")
  // Sin compra todavía, la tarjeta no muestra gobernanza.
  await expect(status.getByRole("group", { name: "Gobernanza de la compra" })).toHaveCount(0)

  // El disparo: la tarea compra; la traza ya trae D1, la frescura y D2.
  backend.traces.set(RUN_ID, RUNNING_TRACE)
  backend.publish(consentedWith("active", RUNNING_TASK))
  await expect(status).toHaveAttribute("data-status", "running")
  const governance = status.getByRole("group", { name: "Gobernanza de la compra" })
  await expect(governance.getByRole("button", { name: /^Comprando…/ })).toBeVisible()

  // Terminó: el pago, la orden, el uso único consumido y las tres decisiones de la política.
  backend.traces.set(RUN_ID, { ...COMPLETED_TRACE, run_id: RUN_ID })
  backend.publish(consentedWith(PURCHASED.state, PURCHASED.task))
  await expect(status).toHaveAttribute("data-status", "purchased")
  // Nadie aprueba al final de una compra delegada: el plan HP no queda en el hilo.
  await expect(purchasePlan(page)).toHaveCount(0)
  // En el hilo la gobernanza sale abierta: la compra se ve avanzar sin buscarla.
  const row = governance.getByRole("button", { name: /^Compra completada en / })
  await expect(row).toHaveAttribute("aria-expanded", "true")
  const steps = governance.getByRole("list", { name: "Controles de la compra" }).locator(":scope > li > div > div > p")
  await expect(steps).toHaveText([...GOVERNED_STEPS, "Uso único consumido"])
  await expect(governance.getByText("Activa: detiene la compra si no cumple")).toBeVisible()
  await expect(governance.getByText("Detalle técnico")).toBeVisible()
  await check("gobernanza")

  // Terminada, la tarjeta deja de releer: ni la delegación ni la traza.
  await expect(page.getByRole("radio", { name: "Delegar" })).toBeChecked()
  const before = backend.calls.length
  await page.waitForTimeout(4_500)
  expect(backend.calls.slice(before).map((call) => call.path)).toEqual([])
  expect(backend.calls.some((call) => call.path === `/purchases/${RUN_ID}/trace`)).toBe(true)
  expect(backend.unexpected).toEqual([])
}

test("elige «Delegar», autoriza y ve la gobernanza cuando la compra delegada termina", async ({
  page,
}) => {
  await delegateAndWatchTheGovernance(page, async () => {})
})

test("el selector cambia el modo del chat sin abrir nada; «Comprar conmigo» vuelve a la compra HP", async ({
  page,
}) => {
  const { calls, unexpected } = await routeAgent(page, [NO_DELEGATIONS])
  await page.goto("/")
  await delegateInChat(page)
  // Terminado el turno «Delegar», su seguimiento es la tarjeta del hilo: sin el plan HP.
  await expect(page.getByRole("button", { name: "Enviar mensaje" })).toBeVisible()
  await expect(purchasePlan(page)).toHaveCount(0)
  await modeSwitch(page).getByText("Comprar conmigo", { exact: true }).click()
  await expect(page.getByRole("radio", { name: "Comprar conmigo" })).toBeChecked()
  const composer = page.getByLabel("Mensaje de compra")
  await composer.fill(PURCHASE_REQUEST)
  await composer.press("Enter")
  await expect(page.getByTestId("purchase-approval-card")).toBeVisible()
  // La compra HP lleva su plan, como siempre.
  await expect(purchasePlan(page)).toBeVisible()
  // El turno HP reemplaza el borrador: la tarjeta de delegación ya no está en el hilo.
  await expect(inChat(page)).toHaveCount(0)
  const modes = calls.filter((call) => call.path === "/assistant").map((call) => (call.body as { mode?: string }).mode)
  expect(modes).toEqual(["delegate", undefined])
  expect(delegationCalls(calls)).toEqual([])
  expect(unexpected).toEqual([])
})

test.describe("teléfono de 390 × 844", () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test("el modo de compra va en su fila y la compra delegada cabe de punta a punta", async ({
    page,
  }) => {
    const fits = async (step: string) => {
      expect(await page.evaluate(THREAD_FITS), step).toEqual({ scroll: 0, outside: 0 })
      expect(await fitsViewport(page), step).toBe(true)
    }
    await delegateAndWatchTheGovernance(page, fits)
    // El selector ocupa su fila, a todo el ancho, debajo de «Salir».
    const [header, switchBox, signOut] = await Promise.all([
      page.locator("header").boundingBox(),
      modeSwitch(page).boundingBox(),
      page.getByRole("button", { name: "Salir" }).boundingBox(),
    ])
    expect(switchBox!.y).toBeGreaterThanOrEqual(signOut!.y + signOut!.height)
    expect(switchBox!.width).toBeGreaterThan(header!.width - 32)
    expect(await fitsViewport(page)).toBe(true)
  })
})

test.describe("móvil", () => {
  test.use({ viewport: { width: 320, height: 640 } })

  test("a 320 px el formulario y la propuesta del hilo, el diálogo y la lista caben sin desbordar", async ({
    page,
  }) => {
    const revokeAll = replay("revoke-all")
    // La lista va primero: la autorización nunca se envía, así que una lectura que dependiera de
    // ella quedaría sin respuesta.
    const { unexpected } = await routeAgent(page, [
      answer(revokeAll, "GET /me/delegations"),
      ...CONSENT.filter((item) => item.method === "POST"),
    ])
    await page.goto("/")
    await fillForm(page, { city: "Bogotá", submit: false })
    expect(await page.evaluate(THREAD_FITS)).toEqual({ scroll: 0, outside: 0 })
    await inChat(page).getByRole("button", { name: "Preparar la autorización" }).click()
    await expect(consentCard(page)).toBeVisible()
    expect(await page.evaluate(THREAD_FITS)).toEqual({ scroll: 0, outside: 0 })
    expect(await fitsViewport(page)).toBe(true)

    const dialog = await openDelegations(page)
    await expect(dialog.getByTestId("delegation-status-card").first()).toBeVisible()
    expect(await page.evaluate(DIALOG_FITS)).toEqual({ width: 288, scroll: 0, outside: 0 })
    expect(unexpected).toEqual([])
  })
})
