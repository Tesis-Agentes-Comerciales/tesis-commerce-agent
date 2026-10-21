import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, waitFor } from "storybook/test"

import { Thread } from "@/components/assistant-ui/elements/thread.aui"
import { CommerceWelcome } from "@/components/commerce-welcome"
import { CommerceRuntime } from "@/features/commerce-runtime"
import type { AgentAssistantMessage, AgentState } from "@/lib/agent-contract"
import { DEMO_PROFILE } from "@/lib/commerce-types"
import type { PurchaseTrace } from "@/lib/trace-contract"
import type { AgentApiMock, AgentApiRequest } from "@/storybook/agent-api-mock"
import { FIXTURE_REQUESTS, fixtureState, scriptedAssistant } from "@/storybook/assistant-transport"
import { delay } from "@/storybook/delay"
import { sendComposerMessage } from "@/storybook/interactions"
import { TRACES, withRunId } from "@/storybook/trace-fixtures"

type ThreadStoryArgs = {
  /** Estado inicial del runtime; se reconstruye aplicando los frames reales de un fixture. */
  state?: AgentState
}

// Lecturas de la traza que respondió el mock, para esperar a la respuesta antes de comprobar que
// algo no aparece.
let traceReads = 0

const meta = {
  title: "Chat/Thread",
  parameters: { layout: "fullscreen" },
  argTypes: { state: { control: false } },
  beforeEach: () => {
    traceReads = 0
  },
  // `main` reproduce el landmark que aporta la app; sin él las historias fallan la regla `region`.
  render: ({ state }) => (
    <main className="h-svh">
      <CommerceRuntime profile={DEMO_PROFILE} initialState={state}>
        <Thread components={{ Welcome: CommerceWelcome }} autoFocus={false} />
      </CommerceRuntime>
    </main>
  ),
} satisfies Meta<ThreadStoryArgs>

export default meta
type Story = StoryObj<typeof meta>

// Cada historia congela un momento de un stream real de `POST /assistant`: los marcadores cortan
// el fixture en el primer frame que los contiene. Sin temporizadores, así el render es determinista.

const AWAITING = fixtureState("purchase")
const NOTHING_CHARGED = "No se creó ningún checkout ni se hizo ningún cobro."

/**
 * `GET /purchases/:runId/trace` responde esta traza con el `run_id` del stream, o el estado HTTP
 * indicado.
 */
function traceApi(trace: PurchaseTrace | { status: number }): { agentApi: AgentApiMock } {
  return {
    agentApi: {
      "GET /purchases/:runId/trace": ({ params }) => {
        traceReads += 1
        return "run_id" in trace
          ? { body: withRunId(trace, params.runId ?? "") }
          : { status: trace.status, body: { detail: "Purchase trace unavailable" } }
      },
    },
  }
}

/** Espera a que la UI reciba al menos una lectura de la traza de esta historia. */
async function traceSettled() {
  await waitFor(() => expect(traceReads).toBeGreaterThan(0))
  await delay(100)
}

function withPurchaseStage(stage: "approved" | "failed"): AgentState {
  const state = fixtureState("purchase")
  return { ...state, purchase: state.purchase && { ...state.purchase, stage } }
}

function interrupted(state: AgentState): AgentState {
  const messages = state.messages.map((message) =>
    message.role === "assistant" && message.status.type === "running"
      ? { ...message, status: { type: "incomplete", reason: "cancelled" } as const }
      : message,
  )
  return { ...state, messages }
}

const UNKNOWN_PURCHASE_REPLY: AgentAssistantMessage = {
  id: "msg_storybook_unknown",
  role: "assistant",
  status: { type: "incomplete", reason: "error", error: "No encontré esa compra." },
  content: [
    {
      type: "tool-call",
      toolCallId: "call_storybook_unknown",
      toolName: "purchase_update",
      args: { run_id: "run_9d4e2b7f1a6c4e8b0f3a5d7c9e1b2f46" },
      argsText: JSON.stringify({ run_id: "run_9d4e2b7f1a6c4e8b0f3a5d7c9e1b2f46" }),
      result: { error: { code: "unknown_run", message: "No encontré esa compra." } },
      isError: true,
    },
  ],
}

export const Empty: Story = {
  play: async ({ canvas }) => {
    await expect(await canvas.findByRole("group", { name: "Sugerencias para empezar" })).toBeVisible()
    await expect(
      canvas.getByRole("button", { name: /Audífonos con cancelación de ruido por menos de 2 millones/ }),
    ).toBeVisible()
  },
}

export const Greeting: Story = { args: { state: fixtureState("greeting") } }

export const Clarify: Story = { args: { state: fixtureState("clarify") } }

export const InterpretingRequest: Story = {
  args: { state: fixtureState("purchase", '"toolName": "interpret_request"') },
}

export const StreamingReasoning: Story = {
  args: { state: fixtureState("purchase", "La persona pide") },
}

export const SearchingCatalog: Story = {
  args: { state: fixtureState("purchase", '"toolName": "search_catalog"') },
}

export const SelectingProduct: Story = {
  args: { state: fixtureState("purchase", "Tres aud") },
}

export const PreparingCheckout: Story = {
  args: { state: fixtureState("purchase", '"toolName": "prepare_checkout"') },
}

export const RequestingApproval: Story = {
  args: { state: fixtureState("purchase", '"toolName": "request_approval"') },
}

export const AwaitingApproval: Story = {
  args: { state: AWAITING },
  play: async ({ canvas }) => {
    await expect(await canvas.findByTestId("purchase-approval-card")).toHaveAttribute(
      "data-status",
      "awaiting_approval",
    )
    await expect(canvas.getByTestId("product-card-hero")).toBeVisible()
    // La fila de actividad sale de la traza, fuera de la tarjeta y del grupo «Pensando…».
    const activity = await canvas.findByTestId("purchase-activity")
    await expect(
      await canvas.findByRole("button", { name: /Esperando tu aprobación/ }),
    ).toHaveAttribute("aria-expanded", "false")
    // La tarjeta anuncia lo que pasa al aprobar o cancelar; que espera la aprobación lo anuncia la
    // fila, una sola vez.
    await expect(activity.querySelector("[role='status']")).toHaveTextContent(
      "Esperando tu aprobación",
    )
    const announced = canvas
      .getAllByRole("status")
      .filter((region) => region.textContent?.includes("aprobación"))
    await expect(announced).toHaveLength(1)
  },
}

/** `state.purchase` en `approved`: la aprobación quedó registrada y solo falta completar. */
export const ApprovalRecorded: Story = { args: { state: withPurchaseStage("approved") } }

/** La tarjeta queda como recibo y la fila, abierta, muestra la política permitida. */
export const PurchaseCompleted: Story = {
  args: { state: fixtureState("purchaseCompleted") },
  parameters: traceApi(TRACES.completedShadow),
  play: async ({ canvas, userEvent }) => {
    await expect(await canvas.findByTestId("approval-receipt")).toHaveTextContent(
      /^Aprobado por ti · \d{2}:\d{2}/,
    )
    const activity = canvas.getByRole("button", { name: /Compra completada en \d+ s/ })
    await userEvent.click(activity)
    // El contenido entra con un fundido corto.
    await waitFor(() => expect(canvas.getByText("Política de compra: permitida")).toBeVisible())
    await expect(canvas.getByText("Producto dentro de lo que pediste")).toBeVisible()
    await expect(canvas.getByText("Comercio permitido")).toBeVisible()
  },
}

/** Sin política la compra se completa igual y el control figura como desactivado. */
export const PurchaseCompletedPolicyOff: Story = {
  args: { state: fixtureState("purchaseCompleted") },
  parameters: traceApi(TRACES.policyOff),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole("button", { name: /Compra completada/ }))
    await waitFor(() => expect(canvas.getByText("Política desactivada")).toBeVisible())
    await expect(canvas.getByText("No se revisó esta compra")).toBeVisible()
  },
}

export const PurchaseCanceled: Story = {
  args: { state: fixtureState("purchaseCanceled") },
  parameters: traceApi(TRACES.canceled),
}

export const PurchaseFailed: Story = {
  args: { state: withPurchaseStage("failed") },
  parameters: traceApi(TRACES.failed),
}

/**
 * Modo activo con una lista de comercios que excluye la tienda (`purchase-blocked.sse`): la tarjeta
 * de bloqueo explica la regla y reemplaza al aviso rojo, porque no es un error.
 */
export const PolicyBlocked: Story = {
  args: { state: fixtureState("purchaseBlocked") },
  parameters: traceApi(TRACES.blockedMerchant),
  play: async ({ canvas }) => {
    const card = await canvas.findByTestId("policy-blocked-card")
    await expect(card).toHaveTextContent("La política de compra detuvo esta compra")
    await expect(card).toHaveTextContent("Comercio permitido")
    await expect(card).toHaveTextContent(NOTHING_CHARGED)
    await expect(canvas.getByRole("button", { name: /Bloqueada por la política/ })).toBeVisible()
    // Sin tarjeta de aprobación, la fila es la que anuncia el estado de la compra.
    await expect(
      canvas.getByTestId("purchase-activity").querySelector("[role='status']"),
    ).toHaveTextContent("Bloqueada por la política de compra")
    await expect(canvas.queryByRole("alert")).toBeNull()
    await expect(canvas.queryByText(/error inesperado/i)).toBeNull()
    await expect(canvas.queryByText("Se detuvo por un error")).toBeNull()
  },
}

/**
 * El mismo `d1_denied` del stream, ahora por la regla de presupuesto de `b2-hp-v2` (S02): el chat no
 * cambia su texto y la tarjeta dice qué regla no se cumplió.
 */
export const PolicyBlockedByBudget: Story = {
  args: { state: fixtureState("purchaseBlocked") },
  parameters: traceApi(TRACES.blockedBudget),
  play: async ({ canvas }) => {
    const card = await canvas.findByTestId("policy-blocked-card")
    await expect(card).toHaveTextContent("Precio dentro de tu presupuesto")
    await expect(card).toHaveTextContent(NOTHING_CHARGED)
    await expect(canvas.getByRole("button", { name: /Bloqueada por la política/ })).toBeVisible()
    await expect(canvas.queryByRole("alert")).toBeNull()
  },
}

/** Sin traza (503) el chat sigue: queda el aviso que redacta el backend, sin fila ni tarjeta. */
export const PolicyBlockedTraceUnavailable: Story = {
  args: { state: fixtureState("purchaseBlocked") },
  parameters: traceApi({ status: 503 }),
  play: async ({ canvas }) => {
    await traceSettled()
    await expect(await canvas.findByRole("alert")).toHaveTextContent(NOTHING_CHARGED)
    await expect(canvas.queryByTestId("purchase-activity")).toBeNull()
    await expect(canvas.queryByTestId("policy-blocked-card")).toBeNull()
  },
}

// Texto de `assistant_transport.ERROR_MESSAGES["checkout_terms_changed"]`.
const TERMS_CHANGED_TEXT =
  "Las condiciones del comercio cambiaron (por ejemplo, el precio, el envío o la disponibilidad), así que no seguí con la compra. No se hizo ningún cobro."

/** `request_approval` falla en el stream porque la oferta cambió al presentar los términos (S04). */
function termsChangedWhilePresenting(): AgentState {
  const messages = AWAITING.messages.map((message) => {
    if (message.role !== "assistant") return message
    const content = message.content.map((part) =>
      part.type === "tool-call" && part.toolName === "request_approval"
        ? {
            ...part,
            result: { error: { code: "checkout_terms_changed", message: TERMS_CHANGED_TEXT } },
            isError: true,
          }
        : part,
    )
    return {
      ...message,
      content,
      status: { type: "incomplete", reason: "error", error: TERMS_CHANGED_TEXT } as const,
    }
  })
  return { ...AWAITING, messages }
}

/**
 * La tarjeta explica la parada y ofrece cancelar: ni aviso rojo ni «Se detuvo por un error», ni el
 * plan o la fila «Esperando tu aprobación», que la contradirían.
 */
export const TermsChangedWhilePresenting: Story = {
  args: { state: termsChangedWhilePresenting() },
  parameters: traceApi(TRACES.awaitingApproval),
  play: async ({ canvas }) => {
    await traceSettled()
    await expect(canvas.queryByTestId("purchase-activity")).toBeNull()
    await expect(canvas.queryByText("Esperando tu aprobación")).toBeNull()
    const card = await canvas.findByTestId("stale-terms-card")
    await expect(card).toHaveTextContent("Las condiciones del comercio cambiaron")
    await expect(card).toHaveTextContent("Sony WH-1000XM6")
    await expect(canvas.getByRole("button", { name: "Cancelar compra" })).toBeEnabled()
    await expect(canvas.queryByTestId("purchase-approval-card")).toBeNull()
    await expect(canvas.queryByRole("alert")).toBeNull()
    await expect(canvas.queryByText("Se detuvo por un error")).toBeNull()
  },
}

export const PendingPurchase: Story = { args: { state: fixtureState("pendingPurchase") } }

export const NoCandidate: Story = { args: { state: fixtureState("noCandidate") } }

/**
 * `prepare_checkout` falla con `isError` y el mensaje termina en `incomplete`/`error`. La compra
 * sigue «en curso» en la traza, pero este turno ya no la moverá: no se muestra la fila.
 */
export const CheckoutError: Story = {
  args: { state: fixtureState("error") },
  parameters: traceApi(TRACES.inProgress),
  play: async ({ canvas }) => {
    await traceSettled()
    await expect(await canvas.findByRole("alert")).toBeVisible()
    await expect(canvas.queryByTestId("purchase-activity")).toBeNull()
  },
}

/** Un `purchase-updated` con un `run_id` que el backend no conoce. */
export const UnknownPurchase: Story = {
  args: { state: { ...AWAITING, messages: [...AWAITING.messages, UNKNOWN_PURCHASE_REPLY] } },
}

/** El stream se cortó durante la búsqueda: los pasos sin resultado quedan interrumpidos. */
export const InterruptedTurn: Story = {
  args: { state: interrupted(fixtureState("purchase", '"toolName": "search_catalog"')) },
}

export const LongConversation: Story = {
  args: {
    state: (() => {
      // Los ids se prefijan: ambos fixtures numeran sus mensajes desde msg_001.
      const conversation = fixtureState("clarify").messages.map((message) => ({
        ...message,
        id: `previo-${message.id}`,
      }))
      const purchase = fixtureState("pendingPurchase")
      return { ...purchase, messages: [...conversation, ...purchase.messages] }
    })(),
  },
}

/** Reproduce `purchase.sse` en vivo contra el mock de `/assistant`. */
export const LivePurchase: Story = {
  parameters: {
    agentApi: { "POST /assistant": (request: AgentApiRequest) => scriptedAssistant(request, 10) },
  },
  play: async (context) => {
    await sendComposerMessage(context, FIXTURE_REQUESTS.purchase)
    await expect(
      await context.canvas.findByTestId("purchase-approval-card", {}, { timeout: 30000 }),
    ).toBeVisible()
  },
}
