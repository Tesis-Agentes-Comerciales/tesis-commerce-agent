import {
  AssistantRuntimeProvider,
  AuiConfig,
  Suggestions,
  Tools,
  unstable_convertExternalMessages as convertExternalMessages,
  unstable_createExternalMessageConversionCache as createConversionCache,
  useAssistantTransportRuntime,
  useAssistantTransportSendCommand,
  type AssistantTransportCommand,
  type AssistantTransportConnectionMetadata,
  type ThreadMessageLike,
  type useExternalMessageConverter,
} from "@assistant-ui/react"
import { memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"

import "@/features/assistant-augmentation"
import { agentToolkit } from "@/features/agent-toolkit"
import { PurchaseSessionContext, type LocalPurchase } from "@/features/purchase-session"
import {
  AGENT_TOOL_NAMES,
  INITIAL_AGENT_STATE,
  type AgentState,
} from "@/lib/agent-contract"
import { AGENT_API_URL, checkoutDetails } from "@/lib/commerce-api"
import { buyerHeaders } from "@/lib/auth-client"
import type { BuyerProfile } from "@/lib/commerce-types"

const STARTER_PROMPTS = [
  "Quiero un celular de máximo 3 millones de pesos",
  "Busco un iPhone; ayúdame a elegir",
  "¿Qué productos tienen en el catálogo?",
] as const

type ConverterMessage = useExternalMessageConverter.Message
type MessagePart = Exclude<ThreadMessageLike["content"], string>[number]

const KNOWN_TOOLS = new Set<string>(AGENT_TOOL_NAMES)
const EMPTY_METADATA = {}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/**
 * El estado llega del backend, pero se valida como cualquier entrada no confiable: solo pasan
 * partes de texto, razonamiento y los pasos conocidos, y una parte desconocida nunca rompe el
 * thread.
 */
function messageParts(content: unknown): MessagePart[] {
  if (!Array.isArray(content)) return []
  return content.flatMap((part: unknown): MessagePart[] => {
    if (!isRecord(part)) return []
    if (part.type === "text" && typeof part.text === "string") {
      return [{ type: "text", text: part.text }]
    }
    if (part.type === "reasoning" && typeof part.text === "string") {
      return [{ type: "reasoning", text: part.text }]
    }
    if (
      part.type === "tool-call" &&
      typeof part.toolCallId === "string" &&
      typeof part.toolName === "string" &&
      KNOWN_TOOLS.has(part.toolName)
    ) {
      const args = isRecord(part.args) ? part.args : {}
      return [
        {
          type: "tool-call",
          toolCallId: part.toolCallId,
          toolName: part.toolName,
          args,
          argsText: typeof part.argsText === "string" ? part.argsText : JSON.stringify(args),
          ...(part.result !== undefined && { result: part.result }),
          ...(part.isError === true && { isError: true }),
        } as MessagePart,
      ]
    }
    return []
  })
}

function messageStatus(status: unknown): ThreadMessageLike["status"] {
  if (!isRecord(status)) return { type: "complete", reason: "stop" }
  if (status.type === "running") return { type: "running" }
  if (status.type === "incomplete") {
    return status.reason === "error"
      ? {
          type: "incomplete",
          reason: "error",
          error: typeof status.error === "string" ? status.error : DEFAULT_ERROR,
        }
      : { type: "incomplete", reason: "cancelled" }
  }
  return { type: "complete", reason: "stop" }
}

/** Estable a nivel de módulo: la caché de conversión se indexa por identidad del callback. */
function convertMessage(message: object): ConverterMessage | ConverterMessage[] {
  if (!isRecord(message)) return []
  const id = typeof message.id === "string" ? message.id : undefined
  if (message.role === "user") {
    const content = messageParts(message.content).filter((part) => part.type === "text")
    const optimistic = isRecord(message.metadata) && message.metadata.isOptimistic === true
    return { id, role: "user", content, ...(optimistic && { metadata: { isOptimistic: true } }) }
  }
  if (message.role === "assistant") {
    return {
      id,
      role: "assistant",
      content: messageParts(message.content),
      status: messageStatus(message.status),
      // Cada respuesta del backend es un mensaje propio; sin esto se unirían los consecutivos.
      convertConfig: { joinStrategy: "none" },
    }
  }
  return []
}

const optimisticMessages = new WeakMap<AssistantTransportCommand, ConverterMessage>()

/** Mensaje optimista del `add-message` en cola, estable mientras el comando siga pendiente. */
function pendingUserMessage(command: AssistantTransportCommand, index: number) {
  if (command.type !== "add-message" || command.message.role !== "user") return null
  let message = optimisticMessages.get(command)
  if (!message) {
    message = {
      id: `pending-${index}-${crypto.randomUUID()}`,
      role: "user",
      content: command.message.parts.flatMap((part) =>
        part.type === "text" ? [{ type: "text" as const, text: part.text }] : [],
      ),
      metadata: { isOptimistic: true },
    }
    optimisticMessages.set(command, message)
  }
  return message
}

function useAgentConverter() {
  const [cache] = useState(() => createConversionCache())
  return useCallback(
    (state: AgentState, connection: AssistantTransportConnectionMetadata) => {
      const messages: object[] = Array.isArray(state.messages) ? [...state.messages] : []
      connection.pendingCommands.forEach((command, index) => {
        const message = pendingUserMessage(command, index)
        if (message) messages.push(message)
      })
      return {
        messages: convertExternalMessages(
          messages,
          convertMessage,
          connection.isSending,
          EMPTY_METADATA,
          cache,
        ),
        isRunning: connection.isSending,
      }
    },
    [cache],
  )
}

const DEFAULT_ERROR = "Ocurrió un error inesperado. Intenta de nuevo."

/**
 * El runtime reporta `Status <código>: <cuerpo>` o el error de `fetch`. Nunca se muestra el texto
 * crudo: solo el `message` en español de un 422 del backend, acotado, o un mensaje fijo.
 */
function transportErrorMessage(error: unknown) {
  if (error instanceof TypeError) {
    return "No pude comunicarme con el agente. Revisa que esté en ejecución."
  }
  const raw = error instanceof Error ? error.message : ""
  const match = /^Status (\d{3}): ([\s\S]*)$/.exec(raw)
  if (!match) return DEFAULT_ERROR
  const status = Number(match[1])
  if (status === 413) return "El mensaje es demasiado largo. Resume tu solicitud."
  if (status === 422) {
    try {
      const body: unknown = JSON.parse(match[2] ?? "")
      const message = isRecord(body) ? body.message : undefined
      if (typeof message === "string" && message.trim() && message.length <= 300) {
        return message.trim()
      }
    } catch {
      // Cuerpo no JSON: se usa el mensaje genérico.
    }
    return "El agente no pudo procesar la solicitud. Revisa tus datos de entrega."
  }
  return status >= 500
    ? "El agente no pudo responder en este momento. Intenta de nuevo."
    : DEFAULT_ERROR
}

function commandText(command: AssistantTransportCommand) {
  if (command.type !== "add-message") return null
  return command.message.parts
    .flatMap((part) => (part.type === "text" ? [part.text] : []))
    .join("\n")
    .trim()
}

function localId() {
  return `local-${crypto.randomUUID()}`
}

/**
 * Cierra el turno en el cliente cuando el stream no terminó: el asistente que quedó en curso pasa a
 * error o cancelado, y los mensajes que nunca llegaron al backend se conservan para no perderlos.
 * El backend vuelve a sanear este estado en la siguiente petición.
 */
function settleState(
  state: AgentState,
  commands: readonly AssistantTransportCommand[],
  error: string | null,
): AgentState {
  const messages = [...state.messages]
  const settled = error
    ? ({ type: "incomplete", reason: "error", error } as const)
    : ({ type: "incomplete", reason: "cancelled" } as const)

  for (const command of commands) {
    const text = commandText(command)
    if (!text) continue
    const known = messages.some(
      (message) =>
        message.role === "user" && message.content.map((part) => part.text).join("\n").trim() === text,
    )
    if (!known) {
      messages.push({ id: localId(), role: "user", content: [{ type: "text", text }] })
    }
  }

  const last = messages.at(-1)
  if (last?.role === "assistant" && last.status.type === "running") {
    messages[messages.length - 1] = { ...last, status: settled }
  } else if (error) {
    messages.push({ id: localId(), role: "assistant", status: settled, content: [] })
  }
  return { ...state, messages }
}

/**
 * `purchase-updated` que el runtime descarta al cancelar o al fallar el envío. Sin reintento el
 * hilo nunca registra una compra que ya se aprobó por REST. Se reintenta una vez por compra: si
 * vuelve a fallar, la tarjeta ya muestra el resultado y el siguiente turno relee la etapa.
 */
function createUpdateRetries() {
  const pending: string[] = []
  const attempted = new Set<string>()
  let flush: (() => void) | null = null

  return {
    keep(commands: readonly AssistantTransportCommand[]) {
      for (const command of commands) {
        if (command.type !== "purchase-updated" || attempted.has(command.runId)) continue
        attempted.add(command.runId)
        pending.push(command.runId)
      }
      if (pending.length > 0) flush?.()
    },
    listen(resend: (runId: string) => void) {
      // Fuera del callback del runtime: el turno que falló todavía se está cerrando.
      flush = () => setTimeout(() => pending.splice(0).forEach(resend), 0)
      if (pending.length > 0) flush()
      return () => {
        flush = null
      }
    },
  }
}

type UpdateRetries = ReturnType<typeof createUpdateRetries>

function PurchaseUpdateRetries({ retries }: { retries: UpdateRetries }) {
  const sendCommand = useAssistantTransportSendCommand()
  useEffect(
    () => retries.listen((runId) => sendCommand({ type: "purchase-updated", runId })),
    [retries, sendCommand],
  )
  return null
}

const NEW_PURCHASE: LocalPurchase = { status: "idle", approvalRecorded: false }

/**
 * Estado local de las compras de esta sesión. Vive por debajo del runtime: `useAssistantTransportRuntime`
 * recrea su lista de threads cada vez que su componente se vuelve a renderizar, lo que cancelaría el
 * stream en curso, así que ningún estado que cambie durante un turno puede vivir ahí arriba.
 */
function PurchaseSessionProvider({
  children,
  onNewPurchase,
}: {
  children: ReactNode
  onNewPurchase?: () => void
}) {
  const [purchases, setPurchases] = useState<Record<string, LocalPurchase>>({})
  const session = useMemo(
    () => ({
      purchases,
      updatePurchase: (runId: string, update: Partial<LocalPurchase>) =>
        setPurchases((current) => ({
          ...current,
          [runId]: { ...(current[runId] ?? NEW_PURCHASE), ...update },
        })),
      onNewPurchase,
    }),
    [purchases, onNewPurchase],
  )

  return <PurchaseSessionContext.Provider value={session}>{children}</PurchaseSessionContext.Provider>
}

const AGENT_CONFIG = AuiConfig({
  tools: Tools({ toolkit: agentToolkit }),
  suggestions: Suggestions([...STARTER_PROMPTS]),
})

type AgentRuntimeProps = {
  children: ReactNode
  /** Datos de entrega del momento del envío; se lee por referencia, nunca como prop reactiva. */
  getDetails: () => object
  initialState: AgentState
  onNewPurchase?: () => void
}

/**
 * Monta el runtime. Está memoizado a propósito: todas sus props deben ser estables, porque un
 * re-render suyo reinicia el thread y aborta la respuesta en curso.
 */
const AgentRuntime = memo(function AgentRuntime({
  children,
  getDetails,
  initialState,
  onNewPurchase,
}: AgentRuntimeProps) {
  const converter = useAgentConverter()
  // Estable como el resto: un re-render de este componente reiniciaría el thread.
  const [retries] = useState(createUpdateRetries)
  const [conversationId] = useState(() => crypto.randomUUID())

  const runtime = useAssistantTransportRuntime<AgentState>({
    api: `${AGENT_API_URL}/assistant`,
    protocol: "assistant-transport",
    initialState,
    converter,
    headers: buyerHeaders,
    body: async () => ({
      details: getDetails(),
      turn_id: crypto.randomUUID(),
      conversation_id: conversationId,
    }),
    onError: (error, { commands, updateState }) => {
      const message = transportErrorMessage(error)
      retries.keep(commands)
      updateState((state) => settleState(state, commands, message))
    },
    // Tras un error llegan solo los comandos en cola: `onError` ya cerró el turno.
    onCancel: ({ commands, updateState }) => {
      retries.keep(commands)
      updateState((state) => settleState(state, commands, null))
    },
  })

  return (
    <AssistantRuntimeProvider runtime={runtime} config={AGENT_CONFIG}>
      <PurchaseUpdateRetries retries={retries} />
      <PurchaseSessionProvider onNewPurchase={onNewPurchase}>{children}</PurchaseSessionProvider>
    </AssistantRuntimeProvider>
  )
})

type CommerceRuntimeProps = {
  children: ReactNode
  profile: BuyerProfile
  /** Estado inicial (Storybook); en la app siempre empieza vacío. */
  initialState?: AgentState
  /** Reinicia la conversación después de una compra terminada. */
  onNewPurchase?: () => void
}

export function CommerceRuntime({
  children,
  profile,
  initialState = INITIAL_AGENT_STATE,
  onNewPurchase,
}: CommerceRuntimeProps) {
  // El perfil cambia sin tocar el runtime: cada envío lee la referencia más reciente.
  const profileRef = useRef(profile)
  useEffect(() => {
    profileRef.current = profile
  }, [profile])
  const getDetails = useCallback(() => checkoutDetails(profileRef.current), [])

  return (
    <AgentRuntime getDetails={getDetails} initialState={initialState} onNewPurchase={onNewPurchase}>
      {children}
    </AgentRuntime>
  )
}
