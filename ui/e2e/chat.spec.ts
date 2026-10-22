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
// Distancia de la fila de actividad al borde de su mensaje, medida en el mismo cuadro: no cambia si
// el chat se desplaza.
const ROW_OFFSET = `(() => {
  const row = document.querySelector("[data-testid='purchase-activity']")
  return row.getBoundingClientRect().top - row.closest("[data-message-id]").getBoundingClientRect().top
})()`

// Términos que ya no valen (S04): el backend rechaza la aprobación o la compra restaurada llega con
// `terms_status`. El `message` es el texto interno del backend, que la UI no debe mostrar.
/** Lo que no ocurrió cuando la frescura detuvo la compra, en cualquiera de sus dos etapas. */
const STOPPED = "No se registró ninguna aprobación ni se firmó ninguna autorización."

/** La traza después de «Cancelar compra»: los mismos pasos y la cancelación al final. */
function canceledTrace(stale: Trace): Trace {
  const at = "2026-10-20T16:11:30.000Z"
  return {
    ...stale,
    status: "canceled",
    updated_at: at,
    steps: [...stale.steps, { id: "canceled", at, duration_ms: null, actor: "user", state: "done", kind: "canceled", by: "user" }],
  } as Trace
}

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
  // El checkout venció; al recargar, `GET /me/pending-purchase` lo informa como `expired`.
  expired: {
    status: 409,
    body: { code: "checkout_expired", message: "Checkout has expired" },
    title: "Las condiciones que revisaste vencieron",
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

/**
 * Respuesta de `GET /purchases/{run_id}/trace` según las llamadas hechas hasta ahora (incluida esta).
 * `until` la retiene hasta que la prueba la suelta, como una traza lenta.
 */
type TraceRoute = (calls: AgentCall[]) => { status?: number; body: unknown; until?: Promise<void> }

type AgentOptions = {
  /** Stream de cada mensaje del comprador, en orden: por defecto, la compra preparada. */
  turns?: string[]
  /** Sin ella, la traza responde 404, como una compra que el agente no conoce. */
  trace?: TraceRoute
  /** `GET /me/pending-purchase` al cargar: por defecto, ninguna compra pendiente. */
  pending?: unknown
  /** `POST /purchases/{run_id}/approval`: por defecto, la aprobación registrada. */
  approval?: { status: number; body: unknown }
  /** `GET /purchases/{run_id}/approval`, que la UI pide antes del POST: por defecto, los términos. */
  terms?: { status: number; body: unknown }
  /** Respuesta a `purchase-updated` después de cancelar: por defecto, `purchase-canceled.sse`. */
  canceled?: (body: AssistantBody) => string
}

const RUN_SEGMENT = /\/run_[A-Za-z0-9_-]+(?=\/|$)/

/** Agente simulado: registra cada llamada para revisar cuerpos y cabeceras. */
async function routeAgent(
  page: Page,
  { turns = [PURCHASE], trace, pending = null, approval, terms, canceled = () => PURCHASE_CANCELED }: AgentOptions = {},
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
        await reply.until
        return route.fulfill({ status: reply.status ?? 200, headers: cors, json: reply.body })
      }
      case "GET /purchases/:runId/approval":
        if (terms) return route.fulfill({ status: terms.status, headers: cors, json: terms.body })
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
  // Con el mouse el foco vuelve al compositor: lo que se escribe enseguida no se pierde.
  const composer = page.getByLabel("Mensaje de compra")
  await expect(composer).toBeFocused()
  await page.keyboard.type("gracias")
  await expect(composer).toHaveValue("gracias")
  const completedLive = page.getByRole("status").filter({ hasText: "Compra completada. El comercio registró la orden." })
  await expect(completedLive).toHaveCount(1)

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
    // La fila anuncia que hay que aprobar, y nadie más lo repite.
    await expect(page.getByRole("status").filter({ hasText: "Esperando tu aprobación" })).toHaveCount(1)
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
    // El comercio observado va por su nombre; debajo, el origen que evaluó la política.
    await expect(card.getByText("Synthetic Commerce Merchant")).toBeVisible()
    await expect(card.getByText("http://127.0.0.1:4120")).toBeVisible()
    await expect(card.getByText(NOTHING_CHARGED)).toBeVisible()
    const activity = page.getByTestId("purchase-activity")
    await expect(activity).toHaveAttribute("data-status", "blocked")
    await expect(
      activity.getByRole("button", { name: /Bloqueada por la política de compra/ }),
    ).toContainText("· Synthetic Commerce Merchant")
    await expect(page.getByText(/error inesperado/i)).toHaveCount(0)
    // Un bloqueo no es un error: ni aviso rojo ni tarjeta de aprobación.
    await expect(page.getByRole("alert")).toHaveCount(0)
    await expect(page.getByTestId("purchase-approval-card")).toHaveCount(0)
    expect(calls.some((call) => call.path === `/purchases/${BLOCKED_RUN}/trace`)).toBe(true)
  })

  test("modo activo: el bloqueo por presupuesto muestra el precio del catálogo y el presupuesto", async ({
    page,
  }) => {
    await routeSession(page)
    const calls = await routeAgent(page, {
      turns: [PURCHASE_BLOCKED],
      trace: () => ({ body: trace("blocked-budget", BLOCKED_RUN) }),
    })
    await page.goto("/")
    const composer = page.getByLabel("Mensaje de compra")
    await composer.fill(PURCHASE_REQUEST)
    await composer.press("Enter")

    const card = page.getByTestId("policy-blocked-card")
    await expect(card).toBeVisible()
    await expect(card.getByRole("heading", { name: "La política de compra detuvo esta compra" })).toBeVisible()
    await expect(card.getByText("Precio dentro de tu presupuesto")).toBeVisible()
    await expect(card.getByText("Precio en el catálogo")).toBeVisible()
    await expect(card.getByText(/^\$\s?1\.540\.000(,00)? COP$/)).toBeVisible()
    await expect(card.getByText("Tu presupuesto total")).toBeVisible()
    await expect(card.getByText(/^\$\s?1\.000\.000(,00)? COP$/)).toBeVisible()
    await expect(card.getByText(NOTHING_CHARGED)).toBeVisible()
    // Solo la regla que falló; las que cumplen quedan en la traza.
    await expect(card.getByText("Comercio permitido")).toHaveCount(0)
    await expect(page.getByRole("alert")).toHaveCount(0)
    await expect(page.getByTestId("purchase-approval-card")).toHaveCount(0)
    expect(calls.some((call) => call.path === `/purchases/${BLOCKED_RUN}/trace`)).toBe(true)
  })

  test("modo activo: la tarjeta de bloqueo sale del stream antes de la traza, sin aviso rojo ni tarjeta del producto", async ({
    page,
  }) => {
    await routeSession(page)
    let release = () => {}
    const slow = new Promise<void>((resolve) => {
      release = resolve
    })
    await routeAgent(page, {
      turns: [PURCHASE_BLOCKED],
      trace: () => ({ body: trace("blocked-budget", BLOCKED_RUN), until: slow }),
    })
    await page.goto("/")
    const composer = page.getByLabel("Mensaje de compra")
    await composer.fill(PURCHASE_REQUEST)
    await composer.press("Enter")

    // Sin traza todavía: el código del stream ya explica el bloqueo, con el producto dentro.
    const card = page.getByTestId("policy-blocked-card")
    await expect(card).toHaveAttribute("aria-busy", "true")
    await expect(card.getByRole("heading", { name: "La política de compra detuvo esta compra" })).toBeVisible()
    await expect(card.getByTestId("policy-blocked-product")).toContainText("Sony WH-1000XM6")
    await expect(card.getByText(NOTHING_CHARGED)).toBeVisible()
    await expect(page.getByRole("alert")).toHaveCount(0)
    await expect(page.getByText("Se detuvo por un error")).toHaveCount(0)
    await expect(page.getByTestId("product-card-hero")).toHaveCount(0)
    // La fila de actividad ya está en su lugar, con lo que dice el stream, y el plan se detiene en
    // el checkout sin irse.
    const row = page.getByTestId("purchase-activity")
    await expect(row).toHaveAttribute("data-source", "stream")
    await expect(row).toHaveAttribute("data-status", "blocked")
    await expect(page.getByTestId("agent-plan")).toContainText("La política la detuvo")
    // Respecto del mensaje: el chat puede desplazarse cuando la tarjeta crece.
    const offset = () => page.evaluate(ROW_OFFSET)
    const rowTop = await offset()

    release()
    await expect(card).not.toHaveAttribute("aria-busy")
    await expect(card.getByText("Precio dentro de tu presupuesto")).toBeVisible()
    await expect(row).toHaveAttribute("data-source", "trace")
    await expect(row).toHaveAttribute("data-status", "blocked")
    // La traza completa la fila donde estaba: no entra después empujando la tarjeta.
    expect(await offset()).toBe(rowTop)
    await expect(page.getByRole("alert")).toHaveCount(0)
    await expect(page.getByTestId("product-card-hero")).toHaveCount(0)
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
  for (const termsStatus of ["changed", "unavailable", "expired"] as const) {
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
      // Con el teclado: el foco sigue a la tarjeta y nunca cae al <body>.
      await card.getByTestId("approve-purchase").press("Enter")

      // La tarjeta de aprobación cede su lugar: sin cobro y con una sola salida.
      const staleCard = page.getByTestId("stale-terms-card")
      await expect(staleCard).toHaveAttribute("data-status", "stale")
      await expect(staleCard.getByRole("heading", { name: stale.title })).toBeVisible()
      await expect(staleCard.getByRole("heading", { name: stale.title })).toBeFocused()
      await expect(staleCard.getByText("No se hizo ningún cobro.", { exact: true })).toBeVisible()
      await expect(staleCard.getByRole("button")).toHaveText(["Cancelar compra"])
      await expect(page.getByTestId("purchase-approval-card")).toHaveCount(0)
      await expect(page.getByText(/recarga la página|no longer|could not|has expired/i)).toHaveCount(0)
      await expect(page.getByRole("alert")).toHaveCount(0)
      // Ni el plan ni la actividad siguen esperando una aprobación que ya no es posible.
      await expect(page.getByText("Esperando tu aprobación")).toHaveCount(0)

      await page.keyboard.press("Tab")
      await expect(staleCard.getByRole("button", { name: "Cancelar compra" })).toBeFocused()
      await page.keyboard.press("Enter")
      await expect(staleCard).toHaveAttribute("data-status", "canceled")
      await expect(page.getByText(/Cancelé la compra pendiente/)).toBeVisible()
      // El turno de `purchase-updated` no le pasa el foco al compositor: sigue en la tarjeta.
      await expect(staleCard.getByRole("heading", { name: "Compra cancelada" })).toBeFocused()
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
        // Es la etapa `approve` (la del piloto): la oferta cambió entre releer los términos y el POST.
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
        await expect(blocked).toContainText(STOPPED)
        const changes = blocked.getByRole("list", { name: "Qué cambió" }).getByRole("listitem")
        const total = changes.filter({ hasText: "Total" })
        await expect(total).toContainText(/En el checkout\$\s?1\.924\.000(,00)? COP/)
        await expect(total).toContainText(/Oferta actual\$\s?2\.024\.000(,00)? COP/)
        // La vigencia renovada cae en el mismo minuto que la anterior: los segundos la distinguen.
        const expiry = changes.filter({ hasText: "Vigencia de la oferta" }).getByRole("definition")
        await expect(expiry).toHaveText([/\d{1,2}:\d{2}:12\b/, /\d{1,2}:\d{2}:48\b/])
        const [presented, current] = await expiry.allTextContents()
        expect(presented).not.toBe(current)
        await expect(activity.getByText("Condiciones confirmadas con el comercio")).toBeVisible()
        await expect(activity.getByText("La política detuvo la compra")).toHaveCount(0)
        await expect(page.getByText("Esperando tu aprobación")).toHaveCount(0)
        await expect(page.getByRole("alert")).toHaveCount(0)
      })
    }

    if (termsStatus === "changed") {
      // El orden real del chat: «Aprobar» relee los términos (GET) y, si cambiaron, el POST no sale.
      test("releer los términos al aprobar responde 409: sin POST, se cancela y la actividad lo muestra", async ({
        page,
      }) => {
        await routeSession(page)
        const reread = trace("stale-terms-reread", APPROVAL.run_id)
        const calls = await routeAgent(page, {
          terms: { status: stale.status, body: stale.body },
          trace: (calls) => {
            if (callIndex(calls, "POST", "/cancel") >= 0) return { body: canceledTrace(reread) }
            const refused = callIndex(calls, "GET", `/purchases/${APPROVAL.run_id}/approval`) >= 0
            return { body: refused ? reread : trace("awaiting-approval", APPROVAL.run_id) }
          },
        })
        await page.goto("/")
        const card = await preparePurchase(page)
        await card.getByTestId("approve-purchase").click()

        const staleCard = page.getByTestId("stale-terms-card")
        await expect(staleCard).toHaveAttribute("data-status", "stale")
        await expect(staleCard.getByRole("heading", { name: stale.title })).toBeVisible()
        // Con el mouse el foco no salta al título; la tarjeta se anuncia por su región de estado.
        await expect(staleCard.getByRole("heading", { name: stale.title })).not.toBeFocused()
        const staleLive = page.getByRole("status").filter({ hasText: `${stale.title}. No se hizo ningún cobro.` })
        await expect(staleLive).toHaveCount(1)
        await expect(staleCard.getByText("No se hizo ningún cobro.", { exact: true })).toBeVisible()
        await expect(staleCard.getByRole("button")).toHaveText(["Cancelar compra"])
        await expect(page.getByTestId("purchase-approval-card")).toHaveCount(0)
        await expect(page.getByRole("alert")).toHaveCount(0)
        expect(callIndex(calls, "POST", `/purchases/${APPROVAL.run_id}/approval`)).toBe(-1)

        const activity = page.getByTestId("purchase-activity")
        await expect(activity).toHaveAttribute("data-status", "stale_terms")
        await activity.getByRole("button", { name: /Detenida: cambiaron las condiciones/ }).click()
        const steps = activity.getByRole("listitem")
        await expect(steps.filter({ hasText: "Condiciones confirmadas con el comercio" })).toContainText(
          "Antes de mostrarte los términos",
        )
        await expect(steps.filter({ hasText: "Tu aprobación" })).toContainText("No se ejecutó")
        const blocked = steps.filter({ hasText: "Las condiciones del comercio cambiaron" })
        await expect(blocked).toHaveAttribute("data-state", "blocked")
        await expect(blocked).toContainText(STOPPED)
        await expect(activity.getByText(/No se mostraron/)).toHaveCount(0)

        await staleCard.getByRole("button", { name: "Cancelar compra" }).click()
        await expect(staleCard).toHaveAttribute("data-status", "canceled")
        await expect(page.getByText(/Cancelé la compra pendiente/)).toBeVisible()
        await expect(activity).toHaveAttribute("data-status", "canceled")
        expect(callIndex(calls, "POST", `/purchases/${APPROVAL.run_id}/cancel`)).toBeGreaterThanOrEqual(0)
        expect(callIndex(calls, "POST", `/purchases/${APPROVAL.run_id}/approval`)).toBe(-1)
        expect(callIndex(calls, "POST", "/complete")).toBe(-1)
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
      // Con el mouse, el turno de `purchase-updated` devuelve el foco al compositor.
      await expect(page.getByLabel("Mensaje de compra")).toBeFocused()
      expect(assistantBodies(calls)[0].commands).toEqual([{ type: "purchase-updated", runId: APPROVAL.run_id }])
      expect(calls.filter((call) => call.method === "POST" && call.path.endsWith("/cancel"))).toHaveLength(1)
    })
  }
})

test.describe("autorización al aprobar (D2)", () => {
  const NOTHING_SIGNED = "No se firmó ninguna autorización ni se hizo ningún cobro."
  const DENIED_TITLE = "La política de compra no autorizó esta compra"
  const D2_DENIED = {
    status: 409,
    body: {
      code: "d2_denied",
      message:
        "La política de compra no autorizó esta compra al aprobarla. No se firmó ninguna autorización ni se hizo ningún cobro.",
    },
  }

  test("aprobar responde 409 d2_denied: sin firma ni cobro, la regla que falló y solo cancelar", async ({
    page,
  }) => {
    await routeSession(page)
    const denied = trace("authorization-denied", APPROVAL.run_id)
    const calls = await routeAgent(page, {
      approval: D2_DENIED,
      trace: (calls) => {
        if (callIndex(calls, "POST", "/cancel") >= 0) return { body: canceledTrace(denied) }
        const refused = callIndex(calls, "POST", `/purchases/${APPROVAL.run_id}/approval`) >= 0
        return { body: refused ? denied : trace("awaiting-approval", APPROVAL.run_id) }
      },
    })
    await page.goto("/")
    const card = await preparePurchase(page)
    await card.getByTestId("approve-purchase").click()

    // La tarjeta de aprobación cede su lugar: qué regla no se cumplió y una sola salida.
    const deniedCard = page.getByTestId("stale-terms-card")
    await expect(deniedCard.getByRole("heading", { name: DENIED_TITLE })).toBeVisible()
    await expect(deniedCard.getByText(NOTHING_SIGNED)).toBeVisible()
    await expect(deniedCard.getByText("La política no cambió desde que eligió el producto")).toBeVisible()
    await expect(deniedCard.getByText("b2-hp-v2 · revisión 2")).toBeVisible()
    await expect(deniedCard.getByRole("button")).toHaveText(["Cancelar compra"])
    await expect(page.getByTestId("purchase-approval-card")).toHaveCount(0)
    await expect(page.getByTestId("policy-blocked-card")).toHaveCount(0)
    await expect(page.getByRole("alert")).toHaveCount(0)

    // La actividad dice que la política la detuvo al aprobar, después de confirmar las condiciones.
    const activity = page.getByTestId("purchase-activity")
    await expect(activity).toHaveAttribute("data-status", "blocked")
    await activity.getByRole("button", { name: /Bloqueada por la política de compra/ }).click()
    const steps = activity.getByRole("listitem")
    const refused = steps.filter({ hasText: "La política no autorizó la compra" })
    await expect(refused).toHaveAttribute("data-state", "blocked")
    await expect(refused).toContainText(NOTHING_SIGNED)
    // «Antes de registrar tu aprobación» también contiene el texto: se busca el título exacto.
    for (const control of ["Tu aprobación", "Firmar la autorización"]) {
      const row = steps.filter({ has: page.getByText(control, { exact: true }) })
      await expect(row).toHaveAttribute("data-state", "skipped")
    }

    await deniedCard.getByRole("button", { name: "Cancelar compra" }).click()
    await expect(deniedCard).toHaveAttribute("data-status", "canceled")
    await expect(page.getByText(/Cancelé la compra pendiente/)).toBeVisible()
    expect(callIndex(calls, "POST", `/purchases/${APPROVAL.run_id}/cancel`)).toBeGreaterThanOrEqual(0)
    expect(callIndex(calls, "POST", "/complete")).toBe(-1)
  })

  test("una compra pendiente con terms_status authorization_denied se puede cancelar tras recargar", async ({
    page,
  }) => {
    await routeSession(page)
    const pending = {
      purchase: {
        run_id: APPROVAL.run_id,
        stage: "awaiting_confirmation",
        checkout_id: APPROVAL.terms.checkout_id,
        order_id: null,
      },
      approval_terms: null,
      terms_status: "authorization_denied",
    }
    const calls = await routeAgent(page, { pending, canceled: canceledEcho })
    await page.goto("/")
    await expect(page.getByText("Recuperé tu compra pendiente. Revisa su estado antes de continuar.")).toBeVisible()
    const deniedCard = page.getByTestId("stale-terms-card")
    await expect(deniedCard.getByRole("heading", { name: DENIED_TITLE })).toBeVisible()
    await expect(deniedCard.getByText(NOTHING_SIGNED)).toBeVisible()
    await expect(page.getByTestId("purchase-approval-card")).toHaveCount(0)
    await expect(page.getByRole("alert")).toHaveCount(0)

    await deniedCard.getByRole("button", { name: "Cancelar compra" }).click()
    await expect(deniedCard).toHaveAttribute("data-status", "canceled")
    expect(calls.filter((call) => call.method === "POST" && call.path.endsWith("/approval"))).toHaveLength(0)
  })
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

  test("la tarjeta de la autorización negada, con su regla, cabe en 375 px", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await routeSession(page)
    await routeAgent(page, {
      approval: { status: 409, body: { code: "d2_denied", message: "denied" } },
      trace: (calls) => ({
        body: trace(
          callIndex(calls, "POST", "/approval") >= 0 ? "authorization-denied" : "awaiting-approval",
          APPROVAL.run_id,
        ),
      }),
    })
    await page.goto("/")
    const card = await preparePurchase(page)
    await card.getByTestId("approve-purchase").click()
    const denied = page.getByTestId("stale-terms-card")
    await expect(denied.getByText("b2-hp-v2 · revisión 2")).toBeVisible()
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
