import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, waitFor, within } from "storybook/test"

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

// Términos que ya no valen (S04): la oferta cambió o no se pudo leer. El `message` es el texto
// interno del backend; la tarjeta no lo muestra.
const STALE_APPROVAL = {
  changed: {
    status: 409,
    body: { code: "checkout_terms_changed", message: "Live offer no longer matches the checkout terms" },
  },
  unavailable: {
    status: 503,
    body: { code: "offer_freshness_unavailable", message: "Live offer could not be read" },
  },
} as const

const STALE_TITLE: Record<PendingTermsStatus, string> = {
  changed: "Las condiciones del comercio cambiaron",
  unavailable: "No pude confirmar las condiciones con el comercio",
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
    await expect(context.canvas.queryByText(/Merchant|expected checkout/)).toBeNull()
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
