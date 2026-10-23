import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, fn, waitFor, within } from "storybook/test"

import App from "@/App"
import type { PendingPurchase as PendingPurchaseBody, PendingTermsStatus } from "@/lib/commerce-api"
import type { AgentApiRequest } from "@/storybook/agent-api-mock"
import {
  ASSISTANT_SSE,
  FIXTURE_REQUESTS,
  fixtureState,
  scriptedAssistant,
} from "@/storybook/assistant-transport"
import { mouseClick, sendComposerMessage } from "@/storybook/interactions"
import { DELEGATION_REPLAYS, replayed } from "@/storybook/fixtures/delegation-replays"
import { TRACES, withRunId } from "@/storybook/trace-fixtures"

const meta = {
  title: "Pages/App",
  component: App,
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof App>

export default meta
type Story = StoryObj<typeof meta>

type PlayContext = Parameters<NonNullable<Story["play"]>>[0]

// Los streams de /assistant son los fixtures reales del backend; ver storybook/assistant-transport.
const STREAM_TIMEOUT = { timeout: 30000 }

// Las historias con `play` reproducen los frames sin pausa apreciable; la de uso libre mantiene el
// ritmo por defecto para poder mirar los pasos.
const FAST_STREAM = {
  agentApi: { "POST /assistant": (request: AgentApiRequest) => scriptedAssistant(request, 10) },
}

async function approvalCard(context: PlayContext) {
  await sendComposerMessage(context, FIXTURE_REQUESTS.purchase)
  const card = await context.canvas.findByTestId("purchase-approval-card", {}, STREAM_TIMEOUT)
  await waitFor(() => expect(card).toHaveAttribute("data-status", "awaiting_approval"), STREAM_TIMEOUT)
  return card
}

/**
 * Interactivo: "hola" saluda, un pedido de audífonos prepara la compra, "50 mil" no encuentra
 * candidatos y cualquier otro texto pide detalles. Cada respuesta reproduce un fixture real.
 */
export const Online: Story = {}

export const Offline: Story = {
  parameters: {
    agentApi: {
      "GET /health": { networkError: true },
      "POST /assistant": { networkError: true },
    },
  },
  play: async (context) => {
    await expect(await context.canvas.findByText("Agente sin conexión")).toBeVisible()
    await sendComposerMessage(context, FIXTURE_REQUESTS.greeting)
    await expect(
      await context.canvas.findByText(/No pude comunicarme con el agente/, {}, STREAM_TIMEOUT),
    ).toBeVisible()
    // El mensaje del usuario no se pierde aunque la petición falle.
    await expect(context.canvas.getByText(FIXTURE_REQUESTS.greeting)).toBeVisible()
  },
}

/** Región de estado de la fila de actividad del mensaje, o `null` si no hay fila. */
function activityStatus({ canvasElement }: PlayContext) {
  return canvasElement.querySelector("[data-testid='purchase-activity'] [role='status']")
}

/** Región de estado de la tarjeta: va justo antes de la sección, fuera de su `aria-busy`. */
const cardStatus = (card: HTMLElement) => card.previousElementSibling

/**
 * Con el mouse. La fila anuncia que la compra espera la aprobación y la tarjeta no lo repite.
 * Completada, el foco vuelve al compositor, como antes de la tarjeta: quien presenta escribe
 * enseguida. El resultado lo anuncia la tarjeta, y la fila calla.
 */
export const PurchaseFlow: Story = {
  parameters: FAST_STREAM,
  play: async (context) => {
    const card = await approvalCard(context)
    await waitFor(
      () => expect(activityStatus(context)).toHaveTextContent("Esperando tu aprobación"),
      STREAM_TIMEOUT,
    )
    await expect(cardStatus(card)).toBeEmptyDOMElement()

    mouseClick(within(card).getByTestId("approve-purchase"))
    await waitFor(() => expect(card).toHaveAttribute("data-status", "completed"), STREAM_TIMEOUT)
    // purchase-updated reproduce purchase-completed.sse.
    await expect(
      await context.canvas.findByText(/La compra quedó completada/, {}, STREAM_TIMEOUT),
    ).toBeVisible()
    const composer = context.canvas.getByRole("textbox", { name: "Mensaje de compra" })
    await waitFor(() => expect(composer).toHaveFocus())
    await context.userEvent.keyboard("gracias")
    await expect(composer).toHaveValue("gracias")
    await expect(cardStatus(card)).toHaveTextContent(
      "Compra completada. El comercio registró la orden.",
    )
    await context.canvas.findByRole("button", { name: /Compra completada/ }, STREAM_TIMEOUT)
    await expect(activityStatus(context)).toBeEmptyDOMElement()
    await expect(within(card).getByRole("link", { name: /Ver orden/ })).toBeVisible()
  },
}

/**
 * Con el teclado: «Cancelar» desaparece y el foco pasa al título, que el lector lee al llegar. El
 * turno de `purchase-updated` no se lo quita.
 */
export const CancelFlow: Story = {
  parameters: FAST_STREAM,
  play: async (context) => {
    const card = await approvalCard(context)
    within(card).getByRole("button", { name: "Cancelar" }).focus()
    await context.userEvent.keyboard("{Enter}")
    await waitFor(() => expect(card).toHaveAttribute("data-status", "canceled"), STREAM_TIMEOUT)
    const title = within(card).getByRole("heading", { name: "Compra cancelada" })
    await expect(title).toHaveFocus()
    await expect(cardStatus(card)).toBeEmptyDOMElement()
    await expect(
      await context.canvas.findByText(/Cancelé la compra pendiente/, {}, STREAM_TIMEOUT),
    ).toBeVisible()
    await expect(title).toHaveFocus()
  },
}

export const CompletionFails: Story = {
  parameters: {
    ...FAST_STREAM,
    agentApi: {
      ...FAST_STREAM.agentApi,
      "POST /purchases/:runId/complete": {
        delayMs: 700,
        status: 502,
        body: { code: "http_error", message: "El ecommerce no pudo procesar la solicitud." },
      },
    },
  },
  play: async (context) => {
    const card = await approvalCard(context)
    await context.userEvent.click(within(card).getByTestId("approve-purchase"))
    // La aprobación quedó registrada: solo se puede reintentar completar, no cancelar.
    await expect(
      await within(card).findByRole("button", { name: "Reintentar compra" }, STREAM_TIMEOUT),
    ).toBeVisible()
    await expect(within(card).queryByRole("button", { name: "Cancelar" })).toBeNull()
  },
}

export const PaymentDeclined: Story = {
  parameters: {
    agentApi: {
      ...FAST_STREAM.agentApi,
      "POST /purchases/:runId/complete": {
        status: 422,
        body: {
          code: "payment_declined",
          message: "El pago sandbox fue rechazado.",
          merchant_request_id: "merchant-req-123",
        },
      },
    },
  },
  play: async (context) => {
    const card = await approvalCard(context)
    await context.userEvent.click(within(card).getByTestId("approve-purchase"))
    await expect(
      await within(card).findByRole("alert", {}, STREAM_TIMEOUT),
    ).toHaveTextContent(/pago sandbox fue rechazado.*merchant-req-123/)
    await expect(within(card).getByRole("button", { name: "Nueva compra" })).toBeVisible()
    await expect(within(card).queryByRole("button", { name: "Reintentar compra" })).toBeNull()
  },
}

export const NoCandidate: Story = {
  parameters: FAST_STREAM,
  play: async (context) => {
    await sendComposerMessage(context, FIXTURE_REQUESTS.noCandidate)
    await expect(
      await context.canvas.findByText(/No encontré audífonos por menos de \$50\.000/, {}, STREAM_TIMEOUT),
    ).toBeVisible()
    await expect(context.canvas.queryByTestId("purchase-approval-card")).toBeNull()
  },
}

export const PendingPurchase: Story = {
  parameters: FAST_STREAM,
  play: async (context) => {
    await approvalCard(context)
    await sendComposerMessage(context, FIXTURE_REQUESTS.pendingPurchase)
    await expect(
      await context.canvas.findByText(/Ya hay una compra pendiente/, {}, STREAM_TIMEOUT),
    ).toBeVisible()
  },
}

export const BackendError: Story = {
  parameters: {
    agentApi: { "POST /assistant": { sse: ASSISTANT_SSE.error, frameDelayMs: 10 } },
  },
  play: async (context) => {
    await sendComposerMessage(context, FIXTURE_REQUESTS.purchase)
    await expect(
      await context.canvas.findByRole("alert", {}, STREAM_TIMEOUT),
    ).toHaveTextContent("No pude comunicarme con el ecommerce ACP.")
    await expect(context.canvas.queryByTestId("purchase-approval-card")).toBeNull()
  },
}

/** El backend rechaza el cuerpo antes de abrir el stream (HTTP 422 saneado). */
export const RequestRejected: Story = {
  parameters: {
    agentApi: {
      "POST /assistant": {
        delayMs: 300,
        status: 422,
        body: { code: "invalid_request", message: "Revisa tus datos de entrega." },
      },
    },
  },
  play: async (context) => {
    await sendComposerMessage(context, FIXTURE_REQUESTS.purchase)
    await expect(
      await context.canvas.findByText("Revisa tus datos de entrega.", {}, STREAM_TIMEOUT),
    ).toBeVisible()
  },
}

// Términos que ya no valen (S04): la oferta cambió, venció o no se pudo leer, o el checkout venció.
// El `message` es el texto interno del backend; la tarjeta no lo muestra.
const STALE_APPROVAL = {
  changed: {
    status: 409,
    body: { code: "checkout_terms_changed", message: "Live offer no longer matches the checkout terms" },
  },
  unavailable: {
    status: 503,
    body: { code: "offer_freshness_unavailable", message: "Live offer could not be read" },
  },
  expired: {
    status: 409,
    body: { code: "offer_expired", message: "La oferta del comercio venció, así que no seguí con la compra." },
  },
  // Checkout vencido, por `expires_at` o porque el comercio ya lo marcó `status: "expired"`: las
  // dos causas responden `checkout_expired`. El `message` de REST es el interno, en inglés.
  checkoutExpired: {
    status: 409,
    body: { code: "checkout_expired", message: "Checkout has expired" },
  },
} as const

/** Título de la tarjeta por motivo; es el mismo en la sesión que recibió el error y al recargar. */
const STALE_TITLE: Record<PendingTermsStatus, string> = {
  changed: "Las condiciones del comercio cambiaron",
  expired: "Las condiciones que revisaste vencieron",
  unavailable: "No pude confirmar las condiciones con el comercio",
  authorization_denied: "La política de compra no autorizó esta compra",
  authorization_unverified: "No pude verificar la autorización de la compra",
}

/**
 * Aprobar responde 409: la tarjeta deja solo «Cancelar compra» y después se pide de nuevo. Todo con
 * el teclado: el foco sigue a la tarjeta (aprobación → términos cambiados → cancelada) y nunca cae
 * al <body>.
 */
export const TermsChangedOnApprove: Story = {
  parameters: {
    agentApi: { ...FAST_STREAM.agentApi, "POST /purchases/:runId/approval": STALE_APPROVAL.changed },
  },
  play: async (context) => {
    const card = await approvalCard(context)
    within(card).getByTestId("approve-purchase").focus()
    await context.userEvent.keyboard("{Enter}")
    const stale = await context.canvas.findByTestId("stale-terms-card", {}, STREAM_TIMEOUT)
    const title = within(stale).getByRole("heading", { name: STALE_TITLE.changed })
    await expect(title).toBeVisible()
    await expect(title).toHaveFocus()
    // Ni la fila ni la región de la tarjeta repiten lo que el lector lee en el título.
    await expect(activityStatus(context)?.textContent ?? "").toBe("")
    await expect(cardStatus(stale)).toBeEmptyDOMElement()
    await expect(within(stale).getByText("No se hizo ningún cobro.")).toBeVisible()
    await expect(within(stale).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Cancelar compra",
    ])
    await expect(context.canvas.queryByText(/no longer|recarga la página/i)).toBeNull()
    await expect(context.canvas.queryByText("Esperando tu aprobación")).toBeNull()

    await context.userEvent.tab()
    await expect(within(stale).getByRole("button", { name: "Cancelar compra" })).toHaveFocus()
    await context.userEvent.keyboard("{Enter}")
    await waitFor(() => expect(stale).toHaveAttribute("data-status", "canceled"), STREAM_TIMEOUT)
    await expect(within(stale).getByRole("heading", { name: "Compra cancelada" })).toHaveFocus()
    await expect(
      await context.canvas.findByText(/Cancelé la compra pendiente/, {}, STREAM_TIMEOUT),
    ).toBeVisible()

    // Pedirla de nuevo prepara otra compra (otro `run_id`) con términos por aprobar.
    await sendComposerMessage(context, FIXTURE_REQUESTS.purchase)
    const again = await context.canvas.findByTestId("purchase-approval-card", {}, STREAM_TIMEOUT)
    await waitFor(() => expect(again).toHaveAttribute("data-status", "awaiting_approval"), STREAM_TIMEOUT)
    await expect(stale).toHaveAttribute("data-status", "canceled")
  },
}

/** Aprobar responde 503: no se pudo leer la oferta, así que tampoco se cobra. */
export const TermsUnavailableOnApprove: Story = {
  parameters: {
    agentApi: { ...FAST_STREAM.agentApi, "POST /purchases/:runId/approval": STALE_APPROVAL.unavailable },
  },
  play: async (context) => {
    const card = await approvalCard(context)
    await context.userEvent.click(within(card).getByTestId("approve-purchase"))
    const stale = await context.canvas.findByTestId("stale-terms-card", {}, STREAM_TIMEOUT)
    await expect(within(stale).getByRole("heading", { name: STALE_TITLE.unavailable })).toBeVisible()
    await expect(within(stale).getByRole("button", { name: "Cancelar compra" })).toBeEnabled()
  },
}

/**
 * La compra ya no espera aprobación (se aprobó o canceló en otra pestaña): el backend responde 409
 * `invalid_stage` con su texto interno en inglés. La tarjeta lo dice en español y no promete nada.
 */
export const ApproveInvalidStage: Story = {
  parameters: {
    agentApi: {
      ...FAST_STREAM.agentApi,
      "POST /purchases/:runId/approval": {
        status: 409,
        body: { code: "invalid_stage", message: "Purchase is not awaiting approval" },
      },
    },
  },
  play: async (context) => {
    const card = await approvalCard(context)
    await context.userEvent.click(within(card).getByTestId("approve-purchase"))
    await expect(await within(card).findByRole("alert", {}, STREAM_TIMEOUT)).toHaveTextContent(
      "Esta compra cambió de estado y ya no admite esta acción.",
    )
    await expect(context.canvas.queryByText(/awaiting|Purchase is/)).toBeNull()
  },
}

/**
 * Cancelar: el comercio devolvió otro checkout, o el mismo sin quedar cancelado; el backend responde
 * 409 `checkout_mismatch` en los dos casos. El texto es cierto para ambos.
 */
export const CancelCheckoutMismatch: Story = {
  parameters: {
    agentApi: {
      ...FAST_STREAM.agentApi,
      "POST /purchases/:runId/cancel": {
        status: 409,
        body: { code: "checkout_mismatch", message: "Merchant did not cancel the expected checkout" },
      },
    },
  },
  play: async (context) => {
    const card = await approvalCard(context)
    await context.userEvent.click(within(card).getByRole("button", { name: "Cancelar" }))
    await expect(await within(card).findByRole("alert", {}, STREAM_TIMEOUT)).toHaveTextContent(
      "La respuesta del comercio no coincide con esta compra, así que me detuve.",
    )
    // El mensaje en inglés del backend no llega a la UI; el nombre del comercio sí puede.
    await expect(context.canvas.queryByText(/Merchant did not|expected checkout/)).toBeNull()
  },
}

function expiredOnApprove(response: (typeof STALE_APPROVAL)["expired" | "checkoutExpired"]): Story {
  return {
    parameters: {
      agentApi: { ...FAST_STREAM.agentApi, "POST /purchases/:runId/approval": response },
    },
    play: async (context) => {
      const card = await approvalCard(context)
      await context.userEvent.click(within(card).getByTestId("approve-purchase"))
      const stale = await context.canvas.findByTestId("stale-terms-card", {}, STREAM_TIMEOUT)
      await expect(within(stale).getByRole("heading", { name: STALE_TITLE.expired })).toBeVisible()
      await expect(within(stale).getByRole("button", { name: "Cancelar compra" })).toBeEnabled()
      await expect(context.canvas.queryByText(/Esperando tu aprobación|has expired/)).toBeNull()
    },
  }
}

/** Aprobar responde 409 `offer_expired`: la tarjeta dice que vencieron y solo deja cancelar. */
export const OfferExpiredOnApprove: Story = expiredOnApprove(STALE_APPROVAL.expired)

/** Aprobar responde 409 `checkout_expired`: el comercio también cancela un checkout vencido. */
export const CheckoutExpiredOnApprove: Story = expiredOnApprove(STALE_APPROVAL.checkoutExpired)

/** Aprobar responde 409 `checkout_not_ready`: el aviso va en español, sin el texto interno. */
export const ApproveCheckoutNotReady: Story = {
  parameters: {
    agentApi: {
      ...FAST_STREAM.agentApi,
      "POST /purchases/:runId/approval": {
        status: 409,
        body: { code: "checkout_not_ready", message: "Checkout is not ready for payment" },
      },
    },
  },
  play: async (context) => {
    const card = await approvalCard(context)
    await context.userEvent.click(within(card).getByTestId("approve-purchase"))
    await expect(await within(card).findByRole("alert", {}, STREAM_TIMEOUT)).toHaveTextContent(
      "El checkout del comercio ya no está listo para pagar, así que no seguí con la compra.",
    )
    await expect(context.canvas.queryByText(/not ready|Checkout is/)).toBeNull()
  },
}

/** Compra pendiente de `purchase.sse`, restaurada al recargar con `terms_status` y sin términos. */
function stalePending(termsStatus: PendingTermsStatus): PendingPurchaseBody {
  const purchase = fixtureState("purchase").purchase
  if (!purchase) throw Error("purchase.sse no deja una compra pendiente")
  return {
    purchase: { ...purchase, checkout_id: "cs_storybook_1" },
    approval_terms: null,
    terms_status: termsStatus,
  }
}

function restoredStaleStory(termsStatus: PendingTermsStatus): Story {
  return {
    parameters: {
      agentApi: { ...FAST_STREAM.agentApi, "GET /me/pending-purchase": { body: stalePending(termsStatus) } },
    },
    play: async (context) => {
      await expect(
        await context.canvas.findByText(/Recuperé tu compra pendiente/, {}, STREAM_TIMEOUT),
      ).toBeVisible()
      const stale = context.canvas.getByTestId("stale-terms-card")
      await expect(within(stale).getByRole("heading", { name: STALE_TITLE[termsStatus] })).toBeVisible()
      await expect(context.canvas.queryByTestId("purchase-approval-card")).toBeNull()
      await expect(context.canvas.queryByRole("alert")).toBeNull()

      await context.userEvent.click(within(stale).getByRole("button", { name: "Cancelar compra" }))
      await waitFor(() => expect(stale).toHaveAttribute("data-status", "canceled"), STREAM_TIMEOUT)
      await expect(
        await context.canvas.findByText(/Cancelé la compra pendiente/, {}, STREAM_TIMEOUT),
      ).toBeVisible()
    },
  }
}

/** Tras recargar, `GET /me/pending-purchase` trae `terms_status: "changed"`: se puede cancelar. */
export const RestoredTermsChanged: Story = restoredStaleStory("changed")

/** Tras recargar, `terms_status: "unavailable"`: la oferta no se pudo leer. */
export const RestoredTermsUnavailable: Story = restoredStaleStory("unavailable")

/**
 * Tras recargar, `terms_status: "expired"`: venció la oferta o el checkout (antes, `GET
 * /me/pending-purchase` respondía 409 con el checkout vencido). Se puede cancelar.
 */
export const RestoredTermsExpired: Story = restoredStaleStory("expired")

/**
 * Tras recargar, `terms_status: "authorization_denied"`: al aprobar, la política no autorizó la
 * compra (D2). La tarjeta lo dice y solo deja cancelar; aprobar otra vez se rechazaría igual.
 */
export const RestoredAuthorizationDenied: Story = restoredStaleStory("authorization_denied")

// Runs cuya aprobación la política no autorizó en esta historia: su traza pasa a la del deny.
const deniedRuns = new Set<string>()

/**
 * Aprobar responde 409 `d2_denied` (POL-06): la tarjeta reemplaza a la de aprobación, dice que no se
 * firmó nada y, cuando llega la traza, muestra la regla que falló. La fila de actividad dice que la
 * política la bloqueó, sin aviso rojo.
 */
export const AuthorizationDeniedOnApprove: Story = {
  parameters: {
    agentApi: {
      ...FAST_STREAM.agentApi,
      "POST /purchases/:runId/approval": ({ params }: AgentApiRequest) => {
        deniedRuns.add(params.runId ?? "")
        return {
          status: 409,
          body: { code: "d2_denied", message: "La política de compra no autorizó esta compra al aprobarla." },
        }
      },
      "GET /purchases/:runId/trace": ({ params }: AgentApiRequest) => {
        const runId = params.runId ?? ""
        const trace = deniedRuns.has(runId) ? TRACES.authorizationDenied : TRACES.awaitingApproval
        return { body: withRunId(trace, runId) }
      },
    },
  },
  play: async (context) => {
    const card = await approvalCard(context)
    await context.userEvent.click(within(card).getByTestId("approve-purchase"))
    const stale = await context.canvas.findByTestId("stale-terms-card", {}, STREAM_TIMEOUT)
    await expect(
      within(stale).getByRole("heading", { name: STALE_TITLE.authorization_denied }),
    ).toBeVisible()
    await expect(
      within(stale).getByText("No se firmó ninguna autorización ni se hizo ningún cobro."),
    ).toBeVisible()
    await expect(
      await within(stale).findByText("La política no cambió desde que eligió el producto", {}, STREAM_TIMEOUT),
    ).toBeVisible()
    await expect(within(stale).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Cancelar compra",
    ])
    await expect(context.canvas.queryByRole("alert")).toBeNull()
    await expect(context.canvas.queryByTestId("policy-blocked-card")).toBeNull()
  },
}

// Modo de compra (HP/HNP): la cabecera lo muestra solo si `/health` trae `hnp_enabled: true`.

/** `/health` con HNP encendido, tal como lo respondió el backend (secuencia `consent`). */
const HNP_HEALTH = replayed(DELEGATION_REPLAYS.consent, "GET /health").body

const CONSENT = DELEGATION_REPLAYS.consent
const GRANTED = replayed(CONSENT, "POST /delegations").body

/** Con HNP, las rutas de la secuencia `consent` y `/assistant` con los fixtures reales. */
const HNP_API = {
  agentApi: {
    ...FAST_STREAM.agentApi,
    "GET /health": { body: HNP_HEALTH },
    "POST /delegations/proposals": {
      status: 201,
      body: replayed(CONSENT, "POST /delegations/proposals").body,
    },
    "POST /delegations": { status: 201, body: GRANTED },
    "GET /delegations/:grantId": { body: GRANTED },
    "GET /me/delegations": { body: { delegations: [] } },
  },
}

/**
 * Con HNP, «Delegar» cambia el modo del chat y no abre nada. El pedido busca sin crear checkout
 * (`delegate.sse`) y deja debajo el formulario prellenado: las opciones marcadas, el total con el
 * presupuesto del mensaje y el mensaje como indicación. «Preparar» lleva al consentimiento y
 * «Autorizar» al estado de la delegación, todo en el mismo hilo.
 */
export const HnpDelegateInChat: Story = {
  parameters: HNP_API,
  play: async (context) => {
    const { canvas, userEvent } = context
    const present = await canvas.findByRole("radio", { name: "Comprar conmigo" })
    await expect(present).toBeChecked()
    await userEvent.click(canvas.getByText("Delegar", { exact: true }))
    await expect(canvas.getByRole("radio", { name: "Delegar" })).toBeChecked()
    await expect(within(document.body).queryByRole("dialog")).toBeNull()

    await sendComposerMessage(context, FIXTURE_REQUESTS.delegate)
    const thread = within(await canvas.findByTestId("delegation-in-chat", {}, STREAM_TIMEOUT))
    const form = within(thread.getByTestId("delegation-form"))
    for (const box of form.getAllByRole("checkbox")) await expect(box).toBeChecked()
    await expect(form.getByLabelText("Total máximo con envío (pesos)")).toHaveValue("2.000.000")
    await expect(form.getByLabelText("Tu indicación (opcional)")).toHaveValue(
      FIXTURE_REQUESTS.delegate,
    )
    await expect(canvas.queryByTestId("purchase-approval-card")).toBeNull()

    await userEvent.click(form.getByRole("button", { name: "Preparar la autorización" }))
    const consent = within(await thread.findByTestId("delegation-consent-card"))
    await userEvent.click(consent.getByRole("button", { name: "Autorizar" }))
    await expect(
      await thread.findByRole("heading", { name: "Delegación activa" }),
    ).toBeVisible()
    await expect(thread.queryByTestId("delegation-consent-card")).toBeNull()
  },
}

/** «Mis delegaciones» abre la lista desde la cabecera, en cualquier modo. */
export const HnpMyDelegations: Story = {
  parameters: HNP_API,
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole("button", { name: "Mis delegaciones" }))
    const page = within(document.body)
    const dialog = await page.findByRole("dialog", { name: "Mis delegaciones" })
    await expect(
      await within(dialog).findByText("Todavía no tienes delegaciones."),
    ).toBeVisible()
    await userEvent.keyboard("{Escape}")
    await waitFor(() => expect(page.queryByRole("dialog")).toBeNull())
    await expect(canvas.getByRole("radio", { name: "Comprar conmigo" })).toBeChecked()
  },
}

const healthAnswered = fn()

/** Con HNP apagado la cabecera queda como siempre: sin modo de compra. */
export const HnpOff: Story = {
  parameters: {
    agentApi: {
      "GET /health": () => {
        healthAnswered()
        return { body: { status: "ok" } }
      },
    },
  },
  play: async ({ canvas }) => {
    await waitFor(() => expect(healthAnswered).toHaveBeenCalled())
    // Lo que la app hace con la respuesta llega en el siguiente render.
    await new Promise((resolve) => setTimeout(resolve, 200))
    await expect(canvas.getByRole("button", { name: "Salir" })).toBeVisible()
    await expect(canvas.queryByRole("group", { name: "Modo de compra" })).toBeNull()
    await expect(canvas.queryByRole("radio")).toBeNull()
  },
}
