import {
  ActionBarPrimitive,
  AuiIf,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  SuggestionPrimitive,
  ThreadPrimitive,
  useAssistantTransportState,
  useAuiState,
  type PartState,
  type ReasoningMessagePartComponent,
  type TextMessagePartComponent,
} from "@assistant-ui/react"
import { ArrowDownIcon, ArrowUpIcon, CheckIcon, CopyIcon, SquareIcon } from "lucide-react"
import { useEffect, useMemo, useState, type ComponentType, type FC } from "react"

import { cn } from "cn"

import {
  AgentPlan,
  derivePlanProgress,
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
import { PROMINENT_TOOLS } from "@/features/agent-toolkit"
import {
  activityVisible,
  blockingDecision,
  useMessagePurchaseTrace,
} from "@/features/purchase-trace"
import {
  AGENT_TOOL_NAMES,
  MAX_MESSAGE_CHARS,
  type AgentMessageStatus,
  type AgentToolName,
} from "@/lib/agent-contract"

type ThreadComponents = {
  Welcome?: ComponentType
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
 * producto elegido y la aprobación, destacados; el texto, al final como respuesta.
 */
function partLayout(parts: readonly PartState[]) {
  const trace: number[] = []
  const prominent: number[] = []
  const text: number[] = []
  parts.forEach((part, index) => {
    if (part.type === "reasoning") trace.push(index)
    else if (part.type === "text") text.push(index)
    else if (isAgentTool(part)) (PROMINENT_TOOLS.has(part.toolName) ? prominent : trace).push(index)
  })
  return { trace, prominent, text }
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

function AgentProgress({
  parts,
  active,
  isLast,
}: {
  parts: readonly PartState[]
  active: boolean
  isLast: boolean
}) {
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
  // de la decisión de aprobar, justo cuando hace falta.
  const pending = own !== null && !SETTLED_STAGES.has(own.stage)
  if (!active && !(isLast && pending)) return null
  const progress = derivePlanProgress({ parts: tools, purchase: own, running: active })
  return <AgentPlan {...progress} className="mb-4" />
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
  const layout = useMemo(() => partLayout(parts), [parts])
  const failedStep = layout.trace.some((index) => {
    const part = parts[index]
    return part?.type === "tool-call" && toolStepStatus(part, status) === "error"
  })
  const purchaseTrace = useMessagePurchaseTrace(parts, status)
  // Un bloqueo de la política no es un error: lo explica su tarjeta, no el aviso rojo.
  const blockedBy = blockingDecision(purchaseTrace)
  const elapsedMs = useActiveElapsed(active)
  const activity = active ? currentActivity(parts) : null
  // El grupo se abre solo mientras el turno corre, hasta que la persona lo toque.
  const [userTraceOpen, setUserTraceOpen] = useState<boolean | null>(null)
  const hasTrace = layout.trace.length > 0
  const traceOpen = hasTrace && (userTraceOpen ?? active)
  // Abierto, las filas ya dicen en qué paso va y cuánto lleva: repetirlo abajo sobra.
  const showActivity = activity !== null && !traceOpen

  return (
    <MessagePrimitive.Root className="group/message px-2 leading-7 wrap-break-word">
      {/* Encabezado solo para lectores de pantalla: mantiene el orden h1 › h2 › h3 de las tarjetas. */}
      <h2 className="sr-only">Respuesta del agente</h2>
      <AgentProgress parts={parts} active={active} isLast={isLast} />

      {hasTrace && (
        <ThinkingGroup
          active={active}
          failed={(failedStep || failedStatus) && blockedBy === null}
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

      {purchaseTrace && activityVisible(purchaseTrace, status) && (
        <PurchaseActivity trace={purchaseTrace} className="mb-3" />
      )}

      {layout.prominent.length > 0 && (
        <div className="mb-4 flex flex-col gap-4">
          {layout.prominent.map((index) => (
            <MessagePrimitive.PartByIndex key={index} index={index} components={PART_COMPONENTS} />
          ))}
        </div>
      )}

      {blockedBy && <PolicyBlockedCard decision={blockedBy} className="mb-4" />}

      {layout.text.map((index) => (
        <MessagePrimitive.PartByIndex key={index} index={index} components={PART_COMPONENTS} />
      ))}

      {/* Plegado, o antes del primer paso, esta línea es la única señal de actividad. Con el grupo
          abierto el tiempo ya está en su encabezado, así que aquí solo va la etiqueta. */}
      {showActivity && (
        <ThinkingIndicator
          label={activity}
          elapsedMs={hasTrace ? undefined : elapsedMs}
          className="mt-2"
        />
      )}

      {/* Sin la traza (404, 503) el aviso sigue: el backend ya lo redacta sin «error inesperado». */}
      {blockedBy === null && (
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
  return (
    <ComposerPrimitive.Root className="relative flex w-full items-end gap-1 rounded-[28px] border border-[#e5e5e5] bg-white p-2 focus-within:border-[#cfcfcf]">
      <ComposerPrimitive.Input
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
