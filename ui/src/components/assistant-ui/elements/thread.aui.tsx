import {
  ActionBarPrimitive,
  AuiIf,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  SuggestionPrimitive,
  ThreadPrimitive,
  useAssistantTransportState,
  useAuiEvent,
  useAuiState,
  type PartState,
  type ReasoningMessagePartComponent,
  type TextMessagePartComponent,
} from "@assistant-ui/react"
import { ArrowDownIcon, ArrowUpIcon, CheckIcon, CopyIcon, SquareIcon } from "lucide-react"
import { useEffect, useMemo, useRef, useState, type ComponentType, type FC } from "react"

import { cn } from "cn"

import {
  AgentPlan,
  derivePlanProgress,
  type PlanProgress,
  PolicyBlockedCard,
  PurchaseActivity,
  ReasoningStep,
  ThinkingGroup,
  ThinkingIndicator,
  focusRing,
  toolStepLabel,
  toolStepStatus,
  useElapsedMs,
} from "@/components/agent"
import { traceMerchant } from "@/components/trace/trace-copy"
import { asSelectResult } from "@/features/agent-results"
import { PROMINENT_TOOLS } from "@/features/agent-toolkit"
import { messagePolicyBlock } from "@/features/policy-block"
import {
  activityVisible,
  blockingDecision,
  useMessagePurchaseTrace,
} from "@/features/purchase-trace"
import { messageHasPurchaseCard, useMessageStaleTerms } from "@/features/stale-terms"
import { isFollowUpOnly, streamPurchaseOf } from "@/features/turn-layout"
import {
  AGENT_TOOL_NAMES,
  MAX_MESSAGE_CHARS,
  type AgentMessageStatus,
  type AgentToolName,
} from "@/lib/agent-contract"

type ThreadComponents = {
  Welcome?: ComponentType
  /** Al pie de los mensajes, dentro del hilo: la compra delegada del turno «Delegar». */
  AfterMessages?: ComponentType
}

type ThreadProps = {
  components?: ThreadComponents
  autoFocus?: boolean
}

const DefaultWelcome: FC = () => (
  <h2 className="mb-6 text-center text-2xl tracking-[-0.025em]">¿Qué quieres comprar?</h2>
)

const TextPart: TextMessagePartComponent = ({ text }) => (
  <p className="whitespace-pre-wrap">{text}</p>
)

const ReasoningPart: ReasoningMessagePartComponent = ({ text, status }) => (
  <ReasoningStep text={text} streaming={status.type === "running"} />
)

const PART_COMPONENTS = { Text: TextPart, Reasoning: ReasoningPart }

const KNOWN_TOOLS = new Set<string>(AGENT_TOOL_NAMES)
const SETTLED_STAGES = new Set<string>(["completed", "canceled", "failed"])
const STOPPED_NOTICE = "El agente no terminó esta respuesta. Vuelve a enviar tu mensaje."

type ToolCallPart = Extract<PartState, { type: "tool-call" }>

function isAgentTool(part: PartState): part is ToolCallPart & { toolName: AgentToolName } {
  return part.type === "tool-call" && KNOWN_TOOLS.has(part.toolName)
}

/**
 * Reparte las partes del mensaje: razonamiento y pasos intermedios van al grupo "Pensando…"; el
 * producto elegido y la aprobación, destacados; el texto, al final como respuesta. El seguimiento
 * de aprobar o cancelar (`purchase_update` solo) no lleva grupo: su texto lo dice todo.
 */
function partLayout(parts: readonly PartState[], followUp: boolean) {
  const trace: number[] = []
  const prominent: number[] = []
  const text: number[] = []
  parts.forEach((part, index) => {
    if (part.type === "reasoning") trace.push(index)
    else if (part.type === "text") text.push(index)
    else if (isAgentTool(part) && !(followUp && part.toolName === "purchase_update")) {
      ;(PROMINENT_TOOLS.has(part.toolName) ? prominent : trace).push(index)
    }
  })
  return { trace, prominent, text }
}

/** El producto que eligió el turno, para nombrarlo en la tarjeta de bloqueo. */
function selectedProduct(parts: readonly PartState[]) {
  const part = parts.find(
    (item) => item.type === "tool-call" && item.toolName === "select_product" && !item.isError,
  )
  return part?.type === "tool-call" ? (asSelectResult(part.result)?.product ?? null) : null
}

/** Qué hace el agente ahora, para la línea de estado bajo el mensaje en curso. */
function currentActivity(parts: readonly PartState[]) {
  const last = parts.at(-1)
  if (!last) return "Pensando…"
  if (last.type === "text") return null
  if (last.type === "reasoning") return "Razonando…"
  if (isAgentTool(last) && last.result === undefined) return toolStepLabel(last.toolName, "running")
  return "Pensando…"
}

/** Tiempo que el mensaje estuvo en curso durante esta sesión; sin dato si ya llegó terminado. */
function useActiveElapsed(active: boolean) {
  const [span, setSpan] = useState<{ start?: number; end?: number }>({})
  useEffect(() => {
    // Se marca fuera del render; el desfase de un tick no cambia los segundos que se muestran.
    const timer = setTimeout(() => {
      const now = Date.now()
      setSpan((current) => {
        const open = current.start !== undefined && current.end === undefined
        if (active) return open ? current : { start: now }
        return open ? { ...current, end: now } : current
      })
    }, 0)
    return () => clearTimeout(timer)
  }, [active])
  return useElapsedMs(span.start, active, span.end)
}

type AgentProgressProps = {
  parts: readonly PartState[]
  active: boolean
  status: AgentMessageStatus | undefined
  /** La política detuvo la compra en el checkout (código del stream). */
  blocked: boolean
  /** La persona no volvió a escribir después de este mensaje. */
  latest: boolean
}

function AgentProgress({ parts, active, status, blocked, latest }: AgentProgressProps) {
  const purchase = useAssistantTransportState((state) => state.purchase)
  const tools = parts.filter(isAgentTool)
  const interpreted = tools.find((part) => part.toolName === "interpret_request")?.result
  const purchaseTurn =
    tools.some((part) => part.toolName !== "interpret_request") ||
    (typeof interpreted === "object" &&
      interpreted !== null &&
      (interpreted as { action?: unknown }).action === "purchase")
  if (!purchaseTurn) return null

  // La etapa de SQLite solo aplica si es la compra de este mensaje, no una anterior.
  const prepared = tools.find((part) => part.toolName === "prepare_checkout")?.result
  const runId =
    typeof prepared === "object" && prepared !== null
      ? (prepared as { run_id?: unknown }).run_id
      : undefined
  const own = purchase && purchase.run_id === runId ? purchase : null
  // El plan acompaña al turno en curso y, después, a la compra que sigue abierta: es el contexto
  // de la decisión de aprobar, y sigue mientras se aprueba (el seguimiento no lleva el suyo).
  const pending = own !== null && !SETTLED_STAGES.has(own.stage)
  // Al terminar (compra resuelta, bloqueo, pregunta) queda en su último estado hasta que la persona
  // vuelve a escribir: quitarlo a la vista subiría de golpe todo lo de abajo. Un turno cortado no
  // lo deja: su paso quedó a medias.
  const settled = own !== null && SETTLED_STAGES.has(own.stage)
  const lastWord = latest && (blocked || settled || status?.type === "complete")
  if (!active && !pending && !lastWord) return null
  const progress: PlanProgress = blocked
    ? { current: "checkout", stopped: "checkout" }
    : derivePlanProgress({ parts: tools, purchase: own, running: active })
  const failedNote = own?.stage === "canceled" ? "Cancelaste la compra" : undefined
  // Con la página quieta nada se anima: el plan que queda como última palabra no late.
  return (
    <AgentPlan
      {...progress}
      failedNote={failedNote}
      still={!active && !pending}
      className="mb-4"
    />
  )
}

function AssistantMessage() {
  const parts = useAuiState((state) => state.message.parts)
  const running = useAuiState((state) => state.message.status?.type === "running")
  const isLast = useAuiState((state) => state.message.isLast)
  const status = useAuiState((state) => state.message.status) as AgentMessageStatus | undefined
  const failedStatus = status?.type === "incomplete" && status.reason === "error"
  // Un turno interrumpido no trae error: sus pasos quedaron sin `result` y el mensaje, cerrado.
  const stopped = status?.type === "incomplete" && status.reason === "cancelled"
  const hasText = parts.some((part) => part.type === "text" && part.text.trim().length > 0)
  // Solo el último mensaje en curso muestra actividad; uno viejo que quedó "running" no.
  const active = running && isLast
  const followUp = isFollowUpOnly(parts)
  const layout = useMemo(() => partLayout(parts, followUp), [parts, followUp])
  const failedStep = layout.trace.some((index) => {
    const part = parts[index]
    return part?.type === "tool-call" && toolStepStatus(part, status) === "error"
  })
  const {
    trace: purchaseTrace,
    pending: tracePending,
    failed: traceFailed,
  } = useMessagePurchaseTrace(parts, status)
  // Un bloqueo de la política no es un error: lo explica su tarjeta, no el aviso rojo. El código del
  // stream ya lo dice, así que la tarjeta sale desde el primer cuadro y la traza solo la completa.
  // Tampoco unos términos que ya no valen: su tarjeta lo dice y ofrece cancelar.
  const streamBlock = messagePolicyBlock(parts)
  const blockedBy = blockingDecision(purchaseTrace)
  const policyBlocked = blockedBy !== null || streamBlock !== null
  const staleTerms = useMessageStaleTerms(parts)
  const explained = policyBlocked || staleTerms
  const elapsedMs = useActiveElapsed(active)
  // Mientras el turno corre, una sola línea viva al pie con el paso y el tiempo, aunque el grupo
  // esté abierto: es lo que queda a la vista cuando el chat baja con el mensaje.
  const activity = active ? currentActivity(parts) : null
  // El grupo no se abre solo: abierto, cada paso que llega empujaría el mensaje mientras corre.
  const [userTraceOpen, setUserTraceOpen] = useState<boolean | null>(null)
  const hasTrace = layout.trace.length > 0
  const traceOpen = hasTrace && (userTraceOpen ?? false)
  // El mensaje provisional que sigue a un envío ya lleva el encabezado del grupo: el del servidor
  // llega con su primer paso y ocupa el mismo lugar, sin que la línea viva salte.
  const repliesToUser = useAuiState(
    (state) => state.thread.messages[state.message.index - 1]?.role === "user",
  )
  const awaitingFirstStep = active && parts.length === 0 && repliesToUser
  const latest = useAuiState(
    (state) => !state.thread.messages.slice(state.message.index + 1).some((m) => m.role === "user"),
  )
  // La fila de actividad sale con el `run_id` del stream, a su altura final; la traza la completa.
  // Sin traza solo se queda el bloqueo, que el stream ya da por terminado.
  const streamPurchase = purchaseTrace ? null : streamPurchaseOf(parts)
  const streamRow =
    streamPurchase !== null &&
    !staleTerms &&
    (!traceFailed || streamPurchase.status === "blocked") &&
    !(streamPurchase.status === "in_progress" && status?.type === "incomplete")

  return (
    <MessagePrimitive.Root className="group/message px-2 leading-7 wrap-break-word">
      {/* Encabezado solo para lectores de pantalla: mantiene el orden h1 › h2 › h3 de las tarjetas. */}
      <h2 className="sr-only">Respuesta del agente</h2>
      {/* Con términos que ya no valen, el plan esperaría una aprobación que ya no es posible: lo dice
          su tarjeta. Un bloqueo del stream lo deja detenido en el checkout, en ámbar; uno que solo
          trae la traza (al aprobar) lo dice su tarjeta. El seguimiento de aprobar no lleva plan: el
          de la compra sigue en su mensaje. */}
      {!staleTerms && !(blockedBy && !streamBlock) && !followUp && (
        <AgentProgress
          parts={parts}
          active={active}
          status={status}
          blocked={streamBlock !== null}
          latest={latest}
        />
      )}

      {(hasTrace || awaitingFirstStep) && (
        <ThinkingGroup
          active={active}
          failed={(failedStep || failedStatus) && !explained}
          stopped={stopped}
          elapsedMs={elapsedMs}
          stepCount={layout.trace.length}
          open={traceOpen}
          onOpenChange={setUserTraceOpen}
          className="mb-3"
        >
          {layout.trace.map((index) => (
            <MessagePrimitive.PartByIndex key={index} index={index} components={PART_COMPONENTS} />
          ))}
        </ThinkingGroup>
      )}

      {/* Con términos que ya no valen, «Esperando tu aprobación» contradiría a la tarjeta. */}
      {purchaseTrace &&
        activityVisible(purchaseTrace, status) &&
        !(staleTerms && purchaseTrace.status === "awaiting_approval") && (
        <PurchaseActivity
          trace={purchaseTrace}
          // La tarjeta anuncia lo que pasa al aprobar o cancelar, pero no que espera la aprobación:
          // eso lo dice esta fila, que ya estaba en el mensaje antes de la tarjeta.
          announce={
            purchaseTrace.status === "awaiting_approval" || !messageHasPurchaseCard(parts)
          }
          className="mb-3"
        />
      )}
      {streamRow && (
        <PurchaseActivity
          trace={null}
          stream={streamPurchase}
          announce={streamPurchase.status === "awaiting_approval" || !messageHasPurchaseCard(parts)}
          className="mb-3"
        />
      )}

      {/* Vacío si el producto fue a la tarjeta de bloqueo: sin hueco. */}
      {layout.prominent.length > 0 && (
        <div className="mb-4 flex flex-col gap-4 empty:hidden">
          {layout.prominent.map((index) => (
            <MessagePrimitive.PartByIndex key={index} index={index} components={PART_COMPONENTS} />
          ))}
        </div>
      )}

      {/* Con la compra bloqueada no hay tarjeta del producto: el producto va en esta. */}
      {policyBlocked && (
        <PolicyBlockedCard
          decision={blockedBy}
          reason={streamBlock?.reason}
          loading={tracePending}
          product={selectedProduct(parts)}
          merchant={purchaseTrace ? traceMerchant(purchaseTrace) : undefined}
          className="mb-4"
        />
      )}

      {layout.text.map((index) => (
        <MessagePrimitive.PartByIndex key={index} index={index} components={PART_COMPONENTS} />
      ))}

      {activity !== null && (
        <ThinkingIndicator label={activity} elapsedMs={elapsedMs} className="mt-2" />
      )}

      {/* Sin la traza (404, 503) el aviso sigue: el backend ya lo redacta sin «error inesperado». Un
          bloqueo de la política nunca lo muestra: su tarjeta sale del código del stream. */}
      {!explained && (
        <MessagePrimitive.Error>
          <ErrorPrimitive.Root className="mt-2 rounded-xl bg-red-50 p-3 text-sm text-red-700" role="alert">
            <ErrorPrimitive.Message />
          </ErrorPrimitive.Root>
        </MessagePrimitive.Error>
      )}

      {/* Un turno interrumpido no tiene error que mostrar: sin este aviso la respuesta queda vacía. */}
      {stopped && (
        <p
          role="status"
          data-testid="stopped-notice"
          className="mt-2 rounded-xl bg-[#f7f7f7] p-3 text-sm text-[#6b6b6b]"
        >
          {STOPPED_NOTICE}
        </p>
      )}

      {hasText && !running && (
        <ActionBarPrimitive.Root
          hideWhenRunning
          autohide="not-last"
          autohideFloat="always"
          className="mt-1 -ml-2 flex gap-1 data-floating:opacity-0 data-floating:transition-opacity group-hover/message:data-floating:opacity-100 group-focus-within/message:data-floating:opacity-100"
        >
          <ActionBarPrimitive.Copy
            className={cn(
              "group/copy flex size-9 items-center justify-center rounded-full text-[#6b6b6b] transition-colors hover:bg-[#f2f2f2] hover:text-[#0d0d0d]",
              focusRing,
            )}
            aria-label="Copiar respuesta"
          >
            <CopyIcon className="size-4 group-data-copied/copy:hidden" aria-hidden />
            <CheckIcon className="hidden size-4 group-data-copied/copy:block" aria-hidden />
          </ActionBarPrimitive.Copy>
        </ActionBarPrimitive.Root>
      )}
    </MessagePrimitive.Root>
  )
}

function ThreadMessage() {
  const role = useAuiState((state) => state.message.role)

  if (role === "user") {
    return (
      <MessagePrimitive.Root className="flex justify-end px-2">
        <div className="max-w-[75%] rounded-[22px] bg-[#0d0d0d] px-4 py-2.5 leading-6 text-white wrap-break-word">
          <MessagePrimitive.Parts components={{ Text: TextPart }} />
        </div>
      </MessagePrimitive.Root>
    )
  }

  return <AssistantMessage />
}

function StarterSuggestion() {
  return (
    <SuggestionPrimitive.Trigger send asChild>
      <button
        type="button"
        className={cn(
          "min-h-11 rounded-[16px] border border-[#e5e5e5] bg-white px-4 py-3 text-left text-sm leading-5 transition-colors hover:bg-[#f7f7f7]",
          focusRing,
        )}
      >
        <SuggestionPrimitive.Title />
      </button>
    </SuggestionPrimitive.Trigger>
  )
}

function Composer({ autoFocus }: { autoFocus: boolean }) {
  const inputRef = useRef<HTMLTextAreaElement>(null)
  // Al empezar una respuesta el compositor toma el foco (así lo recupera quien pulsó una sugerencia,
  // que desaparece). Aprobar o cancelar una compra también empieza un turno (`purchase-updated`):
  // con el mouse el foco vuelve aquí, listo para escribir; con el teclado se queda en la tarjeta.
  useAuiEvent("thread.runStart", () => {
    const active = document.activeElement
    if (!autoFocus || (active?.closest("[data-message-id]") && active.matches(":focus-visible"))) {
      return
    }
    inputRef.current?.focus({ preventScroll: true })
  })

  return (
    <ComposerPrimitive.Root className="relative flex w-full items-end gap-1 rounded-[28px] border border-[#e5e5e5] bg-white p-2 focus-within:border-[#cfcfcf]">
      <ComposerPrimitive.Input
        ref={inputRef}
        unstable_focusOnRunStart={false}
        placeholder="Describe lo que quieres comprar..."
        className="max-h-52 min-h-9 flex-1 resize-none bg-transparent py-1.5 pr-2 pl-3 text-base leading-6 outline-none placeholder:text-[#8e8e8e]"
        rows={1}
        autoFocus={autoFocus}
        enterKeyHint="send"
        aria-label="Mensaje de compra"
        maxLength={MAX_MESSAGE_CHARS}
      />
      <AuiIf condition={(state) => !state.thread.isRunning}>
        <ComposerPrimitive.Send asChild>
          <button
            type="button"
            className="flex size-9 shrink-0 items-center justify-center rounded-full bg-[#0d0d0d] text-white transition-opacity hover:opacity-85 disabled:bg-[#999]"
            aria-label="Enviar mensaje"
          >
            <ArrowUpIcon className="size-4" aria-hidden />
          </button>
        </ComposerPrimitive.Send>
      </AuiIf>
      <AuiIf condition={(state) => state.thread.isRunning}>
        <ComposerPrimitive.Cancel asChild>
          <button
            type="button"
            className="flex size-9 shrink-0 items-center justify-center rounded-full bg-[#0d0d0d] text-white"
            aria-label="Detener respuesta"
          >
            <SquareIcon className="size-3.5 fill-current" aria-hidden />
          </button>
        </ComposerPrimitive.Cancel>
      </AuiIf>
    </ComposerPrimitive.Root>
  )
}

export function Thread({ components = {}, autoFocus = true }: ThreadProps) {
  const Welcome = components.Welcome ?? DefaultWelcome
  const AfterMessages = components.AfterMessages

  return (
    <ThreadPrimitive.Root className="flex h-full flex-col bg-white text-[#0d0d0d]">
      <ThreadPrimitive.Viewport className="relative flex flex-1 flex-col overflow-y-auto scroll-smooth">
        <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col px-4 pt-4">
          <AuiIf condition={(state) => state.thread.isEmpty}>
            <div className="flex flex-1 flex-col justify-center pb-[12vh]">
              <Welcome />
              <div
                className="mx-2 grid gap-2 sm:grid-cols-3"
                role="group"
                aria-label="Sugerencias para empezar"
              >
                <ThreadPrimitive.Suggestions>{() => <StarterSuggestion />}</ThreadPrimitive.Suggestions>
              </div>
            </div>
          </AuiIf>

          <div className="mb-14 flex flex-col gap-8 empty:hidden">
            <ThreadPrimitive.Messages>{() => <ThreadMessage />}</ThreadPrimitive.Messages>
            {AfterMessages && <AfterMessages />}
          </div>

          <ThreadPrimitive.ViewportFooter className="sticky bottom-0 mt-auto flex flex-col gap-2 bg-white pb-2">
            <ThreadPrimitive.ScrollToBottom asChild>
              <button
                type="button"
                className="absolute -top-12 self-center rounded-full border border-[#e5e5e5] bg-white p-2 shadow-sm disabled:invisible"
                aria-label="Ir al final"
              >
                <ArrowDownIcon className="size-4" aria-hidden />
              </button>
            </ThreadPrimitive.ScrollToBottom>
            <Composer autoFocus={autoFocus} />
            <p className="text-center text-xs text-[#6b6b6b]">
              Revisa los datos importantes antes de aprobar una compra.
            </p>
          </ThreadPrimitive.ViewportFooter>
        </div>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  )
}
