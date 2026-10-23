import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, waitFor, within } from "storybook/test"

import { Thread } from "@/components/assistant-ui/elements/thread.aui"
import { CommerceWelcome } from "@/components/commerce-welcome"
import { CommerceRuntime } from "@/features/commerce-runtime"
import type { AgentAssistantMessage, AgentState } from "@/lib/agent-contract"
import { DEMO_PROFILE } from "@/lib/commerce-types"
import type { PurchaseTrace } from "@/lib/trace-contract"
import type { AgentApiMock, AgentApiRequest } from "@/storybook/agent-api-mock"
import { FIXTURE_REQUESTS, fixtureState, scriptedAssistant } from "@/storybook/assistant-transport"
import { delay } from "@/storybook/delay"
import { runningAnimations, sendComposerMessage } from "@/storybook/interactions"
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
// El plan y la gobernanza avanzan a ritmo (docs/design/trace-panel.md): seis pasos de 700 ms o una
// docena de filas de 350 ms caben de sobra.
const PACED_TIMEOUT = 8000
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

/** La traza tarda en llegar: la UI muestra lo que ya sabe por el stream mientras tanto. */
function slowTraceApi(trace: PurchaseTrace): { agentApi: AgentApiMock } {
  return {
    agentApi: {
      "GET /purchases/:runId/trace": ({ params }) => ({
        body: withRunId(trace, params.runId ?? ""),
        delayMs: 60_000,
      }),
    },
  }
}

/** El último mensaje del hilo, para mirar solo el turno en curso. */
function lastMessage(canvasElement: HTMLElement) {
  const messages = canvasElement.querySelectorAll<HTMLElement>("[data-message-id]")
  const last = messages[messages.length - 1]
  if (!last) throw Error("El hilo no tiene mensajes")
  return { element: last, canvas: within(last) }
}

/** Mientras corre, siempre una línea viva al pie con el paso y el tiempo, aunque el grupo esté abierto. */
async function expectLiveLine(canvasElement: HTMLElement, label: RegExp) {
  const { canvas } = lastMessage(canvasElement)
  const line = await canvas.findByTestId("thinking-indicator")
  await expect(line).toBeVisible()
  await expect(line).toHaveTextContent(label)
  await expect(line).toHaveTextContent(/\d+,\d s|\d+ s/)
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

// Mientras carga un turno (trace-panel.md): una sola línea viva al pie, un marcador bajo y rotulado
// en el lugar del producto, y nada que luego desaparezca.

export const InterpretingRequest: Story = {
  args: { state: fixtureState("purchase", '"toolName": "interpret_request"') },
  play: async ({ canvasElement }) => {
    await expectLiveLine(canvasElement, /Interpretando tu solicitud…/)
  },
}

export const StreamingReasoning: Story = {
  args: { state: fixtureState("purchase", "La persona pide") },
  play: async ({ canvasElement }) => {
    await expectLiveLine(canvasElement, /Razonando…/)
  },
}

/** El esqueleto del carrusel sale solo si la búsqueda tarda más de 300 ms. */
export const SearchingCatalog: Story = {
  args: { state: fixtureState("purchase", '"toolName": "search_catalog"') },
  play: async ({ canvas, canvasElement, userEvent }) => {
    await expectLiveLine(canvasElement, /Buscando en el catálogo…/)
    // El grupo no se abre solo mientras corre: el carrusel está dentro, al abrirlo.
    const group = lastMessage(canvasElement).canvas.getByRole("button", { name: /Pensando…/ })
    await expect(group).toHaveAttribute("aria-expanded", "false")
    await userEvent.click(group)
    await waitFor(() =>
      expect(canvas.getByRole("group", { name: "Resultados del catálogo" })).toHaveAttribute(
        "aria-busy",
        "true",
      ),
    )
  },
}

/** Eligiendo: el marcador dice entre cuántas opciones, sin esqueleto grande y mudo. */
export const SelectingProduct: Story = {
  args: { state: fixtureState("purchase", '"toolName": "select_product"') },
  play: async ({ canvasElement }) => {
    const { canvas } = lastMessage(canvasElement)
    await expect(await canvas.findByTestId("product-choice-marker")).toHaveTextContent(
      /Eligiendo entre \d+ opciones/,
    )
    await expect(canvas.queryByTestId("product-card-hero")).toBeNull()
    await expect(canvas.queryByRole("status", { name: "Cargando la recomendación" })).toBeNull()
    await expectLiveLine(canvasElement, /Eligiendo el producto…/)
  },
}

/** Con el razonamiento abierto en el grupo, la línea viva sigue al pie. */
export const SelectingProductWithReasoning: Story = {
  args: { state: fixtureState("purchase", "Dos aud") },
  play: async ({ canvasElement }) => {
    await expectLiveLine(canvasElement, /Razonando…/)
    await expect(lastMessage(canvasElement).canvas.getByTestId("product-choice-marker")).toBeVisible()
  },
}

/**
 * Producto elegido y checkout en preparación: el marcador ya nombra el producto. La tarjeta grande
 * entra con el checkout listo; si la política lo detiene, nunca entra.
 */
export const PreparingCheckout: Story = {
  args: { state: fixtureState("purchase", '"toolName": "prepare_checkout"') },
  play: async ({ canvasElement }) => {
    const { canvas } = lastMessage(canvasElement)
    await expect(await canvas.findByTestId("product-choice-marker")).toHaveTextContent(
      "Sony WH-1000XM6",
    )
    await expect(canvas.queryByTestId("product-card-hero")).toBeNull()
    await expectLiveLine(canvasElement, /Preparando el checkout…/)
    // La fila de actividad ya está, con el `run_id` de los `args`: la traza no la hará entrar luego.
    const row = canvas.getByTestId("purchase-activity")
    await expect(row).toHaveAttribute("data-source", "stream")
    await expect(row).toHaveTextContent("Comprando…")
    await expect(row).toHaveTextContent("Sony WH-1000XM6 negro")
  },
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
    // La fila de actividad sale de la traza, fuera de la tarjeta y del grupo «Pensando…», y abierta:
    // la gobernanza de la compra se ve sin buscarla.
    const activity = await canvas.findByTestId("purchase-activity")
    await expect(
      await canvas.findByRole("button", { name: /Esperando tu aprobación/ }),
    ).toHaveAttribute("aria-expanded", "true")
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
  play: async ({ canvas }) => {
    await expect(await canvas.findByTestId("approval-receipt")).toHaveTextContent(
      /^Aprobado por ti · \d{2}:\d{2}/,
    )
    const activity = canvas.getByRole("button", { name: /Compra completada en \d+ s/ })
    await expect(activity).toHaveAttribute("aria-expanded", "true")
    // Las filas de la gobernanza entran de a una (`GOVERNANCE_ROW_MS`).
    await waitFor(() => expect(canvas.getByText("Política de compra: permitida")).toBeVisible(), {
      timeout: PACED_TIMEOUT,
    })
    await expect(canvas.getByText("Producto dentro de lo que pediste")).toBeVisible()
    await expect(canvas.getByText("Comercio permitido")).toBeVisible()
  },
}

/** Sin política la compra se completa igual y el control figura como desactivado. */
export const PurchaseCompletedPolicyOff: Story = {
  args: { state: fixtureState("purchaseCompleted") },
  parameters: traceApi(TRACES.policyOff),
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByRole("button", { name: /Compra completada/ }),
    ).toHaveAttribute("aria-expanded", "true")
    await waitFor(() => expect(canvas.getByText("Política desactivada")).toBeVisible(), {
      timeout: PACED_TIMEOUT,
    })
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
  play: async ({ canvas, canvasElement }) => {
    const card = await canvas.findByTestId("policy-blocked-card")
    await expect(card).toHaveTextContent("La política de compra detuvo esta compra")
    // La tarjeta sale con el código del stream; la regla llega con la traza.
    await waitFor(() => expect(card).toHaveTextContent("Comercio permitido"))
    await expect(card).toHaveTextContent(NOTHING_CHARGED)
    await expect(canvas.getByRole("button", { name: /Bloqueada por la política/ })).toBeVisible()
    // Sin tarjeta de aprobación, la fila es la que anuncia el estado de la compra.
    await expect(
      canvas.getByTestId("purchase-activity").querySelector("[role='status']"),
    ).toHaveTextContent("Bloqueada por la política de compra")
    await expect(canvas.queryByRole("alert")).toBeNull()
    await expect(canvas.queryByText(/error inesperado/i)).toBeNull()
    await expect(canvas.queryByText("Se detuvo por un error")).toBeNull()
    // Sin la tarjeta grande del producto: el producto va dentro de la de bloqueo. Orden final: el
    // plan, «Pensó durante», la fila de actividad y la tarjeta.
    await expect(canvas.queryByTestId("product-card-hero")).toBeNull()
    await expect(within(card).getByTestId("policy-blocked-product")).toHaveTextContent(
      "Sony WH-1000XM6",
    )
    const { element } = lastMessage(canvasElement)
    const order = [...element.querySelectorAll("[data-testid]")]
      .map((node) => node.getAttribute("data-testid"))
      .filter((id) =>
        ["agent-plan", "thinking-group", "purchase-activity", "policy-blocked-card"].includes(id ?? ""),
      )
    await expect(order).toEqual([
      "agent-plan",
      "thinking-group",
      "purchase-activity",
      "policy-blocked-card",
    ])
    // El plan se queda donde estaba, detenido en el checkout por la política: en ámbar, no en rojo.
    // Llega ahí a ritmo, un paso cada `PLAN_STEP_MIN_MS`.
    const plan = within(element).getByTestId("agent-plan")
    await waitFor(
      () =>
        expect(within(plan).getByText("Checkout").closest("li")).toHaveTextContent(
          "(detenido por la política de compra)",
        ),
      { timeout: PACED_TIMEOUT },
    )
    await expect(plan.querySelector(".text-red-700, .bg-red-600")).toBeNull()
  },
}

/**
 * `d1_denied` en el stream y la traza todavía en camino: la tarjeta de bloqueo ya está, con el
 * producto y las reglas en esqueleto. Nunca aviso rojo ni «Se detuvo por un error».
 */
export const PolicyBlockedWaitingForTrace: Story = {
  args: { state: fixtureState("purchaseBlocked") },
  parameters: slowTraceApi(TRACES.blockedBudget),
  play: async ({ canvas }) => {
    const card = await canvas.findByTestId("policy-blocked-card")
    await expect(card).toHaveAttribute("aria-busy", "true")
    await expect(card).toHaveTextContent("La política de compra detuvo esta compra")
    await expect(card).toHaveTextContent("Sony WH-1000XM6")
    await expect(card).toHaveTextContent(NOTHING_CHARGED)
    await expect(canvas.queryByRole("alert")).toBeNull()
    await expect(canvas.queryByText("Se detuvo por un error")).toBeNull()
    await expect(canvas.queryByTestId("product-card-hero")).toBeNull()
    await expect(canvas.getByRole("button", { name: /Pasos del agente/ })).toBeVisible()
    // La fila ya está en su lugar, con lo que dice el stream; la traza solo la completa.
    const row = canvas.getByTestId("purchase-activity")
    await expect(row).toHaveAttribute("data-source", "stream")
    await expect(row).toHaveTextContent("Bloqueada por la política de compra")
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
    await waitFor(() => expect(card).toHaveTextContent("Precio dentro de tu presupuesto"))
    await expect(card).toHaveTextContent(NOTHING_CHARGED)
    await expect(canvas.getByRole("button", { name: /Bloqueada por la política/ })).toBeVisible()
    await expect(canvas.queryByRole("alert")).toBeNull()
  },
}

/**
 * Sin traza (503) el chat sigue: la tarjeta de bloqueo se queda con lo que dice el código del
 * stream, sin esqueleto, y sin la fila de actividad. Tampoco aquí hay aviso rojo.
 */
export const PolicyBlockedTraceUnavailable: Story = {
  args: { state: fixtureState("purchaseBlocked") },
  parameters: traceApi({ status: 503 }),
  play: async ({ canvas }) => {
    await traceSettled()
    const card = await canvas.findByTestId("policy-blocked-card")
    await waitFor(() => expect(card).not.toHaveAttribute("aria-busy"))
    await expect(card).toHaveTextContent("No pude mostrar qué regla no se cumplió.")
    await expect(card).toHaveTextContent(NOTHING_CHARGED)
    await expect(canvas.queryByRole("alert")).toBeNull()
    // La fila sale del stream y se queda: el bloqueo ya es definitivo. Sin traza no se abre.
    const row = canvas.getByTestId("purchase-activity")
    await expect(row).toHaveAttribute("data-source", "stream")
    await expect(row).toHaveAttribute("data-status", "blocked")
    await expect(
      canvas.getByRole("button", { name: /Bloqueada por la política de compra/ }),
    ).toBeDisabled()
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

/**
 * La búsqueda no encontró nada y el turno termina preguntando: el plan queda esperando la respuesta
 * y, con la página quieta, nada late.
 */
export const NoCandidate: Story = {
  args: { state: fixtureState("noCandidate") },
  play: async ({ canvasElement }) => {
    const { canvas } = lastMessage(canvasElement)
    await waitFor(
      () => expect(canvas.getByTestId("agent-plan")).toHaveTextContent("Esperando tu respuesta"),
      { timeout: PACED_TIMEOUT },
    )
    await waitFor(() => expect(runningAnimations(canvasElement)).toHaveLength(0), {
      timeout: 3000,
    })
  },
}

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

/**
 * Al aprobar, el seguimiento (`purchase_update`) corre sin plan y sin grupo «Pensando…»: solo la
 * línea viva. El plan de la compra sigue en su mensaje.
 */
export const ApprovingFollowUp: Story = {
  args: { state: fixtureState("purchaseCompleted", '"toolName": "purchase_update"') },
  parameters: traceApi(TRACES.completedShadow),
  play: async ({ canvasElement }) => {
    const { canvas } = lastMessage(canvasElement)
    await expectLiveLine(canvasElement, /Actualizando la compra…/)
    await expect(canvas.queryByTestId("agent-plan")).toBeNull()
    await expect(canvas.queryByTestId("thinking-group")).toBeNull()
    await expect(canvas.queryByText("Plan de compra")).toBeNull()
  },
}

/**
 * Terminado, el seguimiento es solo su texto. El plan de la compra se queda en su mensaje, completo,
 * hasta que la persona vuelve a escribir: quitarlo ahí subiría de golpe el recibo.
 */
export const FollowUpCompleted: Story = {
  args: { state: fixtureState("purchaseCompleted") },
  parameters: traceApi(TRACES.completedShadow),
  play: async ({ canvasElement }) => {
    const { canvas } = lastMessage(canvasElement)
    await expect(await canvas.findByText(/La compra quedó completada/)).toBeVisible()
    await expect(canvas.queryByTestId("thinking-group")).toBeNull()
    await expect(canvas.queryByTestId("agent-plan")).toBeNull()
    const plans = canvasElement.querySelectorAll<HTMLElement>("[data-testid='agent-plan']")
    await expect(plans).toHaveLength(1)
    await waitFor(() => expect(plans[0]).toHaveTextContent("6 de 6"), { timeout: PACED_TIMEOUT })
  },
}

/** Cuando la persona vuelve a escribir, el plan de la compra anterior se retira. */
export const PlanRetiresOnNextMessage: Story = {
  args: {
    state: (() => {
      const state = fixtureState("purchaseCompleted")
      const next = {
        id: "msg_next",
        role: "user" as const,
        content: [{ type: "text" as const, text: "Gracias" }],
      }
      return { ...state, messages: [...state.messages, next] }
    })(),
  },
  parameters: traceApi(TRACES.completedShadow),
  play: async ({ canvas, canvasElement }) => {
    await expect(await canvas.findByText("Gracias")).toBeVisible()
    await expect(canvasElement.querySelector("[data-testid='agent-plan']")).toBeNull()
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

/**
 * Recién enviado, antes del primer frame: el mensaje provisional ya lleva el encabezado del grupo y
 * la línea viva, en el mismo lugar que tendrán en el mensaje del servidor.
 */
export const Sending: Story = {
  parameters: {
    agentApi: {
      "POST /assistant": (request: AgentApiRequest) => ({
        ...scriptedAssistant(request, 10),
        delayMs: 60_000,
      }),
    },
  },
  play: async (context) => {
    await sendComposerMessage(context, FIXTURE_REQUESTS.purchase)
    const { canvas } = lastMessage(context.canvasElement)
    await expect(await canvas.findByRole("button", { name: /Pensando…/ })).toBeDisabled()
    await expectLiveLine(context.canvasElement, /Pensando…/)
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
