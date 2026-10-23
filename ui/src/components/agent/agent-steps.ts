import type {
  AgentMessageStatus,
  AgentPurchase,
  AgentToolError,
  AgentToolName,
} from "@/lib/agent-contract"

export type StepStatus = "running" | "complete" | "error"

type ToolLabels = { running: string; complete: string; error: string }

const TOOL_LABELS: Record<AgentToolName, ToolLabels> = {
  interpret_request: {
    running: "Interpretando tu solicitud",
    complete: "Solicitud interpretada",
    error: "No pude interpretar la solicitud",
  },
  search_catalog: {
    running: "Buscando en el catálogo",
    complete: "Catálogo revisado",
    error: "No pude revisar el catálogo",
  },
  select_product: {
    running: "Eligiendo el producto",
    complete: "Producto elegido",
    error: "No pude elegir un producto",
  },
  prepare_checkout: {
    running: "Preparando el checkout",
    complete: "Checkout preparado",
    error: "No pude preparar el checkout",
  },
  request_approval: {
    running: "Preparando los términos de aprobación",
    complete: "Términos listos para aprobar",
    error: "No pude obtener los términos",
  },
  purchase_update: {
    running: "Actualizando la compra",
    complete: "Compra actualizada",
    error: "No pude actualizar la compra",
  },
}

/** Título de un paso según su estado; `running` lleva puntos suspensivos. */
export function toolStepLabel(toolName: AgentToolName, status: StepStatus) {
  const labels = TOOL_LABELS[toolName]
  return status === "running" ? `${labels.running}…` : labels[status]
}

/**
 * Estado visual de un tool call. Sin `result` el paso sigue en curso, salvo que el mensaje ya
 * terminó (por error o cancelación): entonces el paso quedó interrumpido.
 */
export function toolStepStatus(
  part: { result?: unknown; isError?: boolean },
  messageStatus?: AgentMessageStatus,
): StepStatus {
  if (part.isError) return "error"
  if (part.result !== undefined) return "complete"
  if (messageStatus && messageStatus.type !== "running") return "error"
  return "running"
}

/**
 * Mensaje de un paso fallido. El `result` no es confiable: solo se acepta `error.message` como
 * texto no vacío y acotado; si no, se usa un mensaje genérico.
 */
export function toolErrorMessage(part: { result?: unknown; isError?: boolean }) {
  if (!part.isError) return null
  const error =
    typeof part.result === "object" && part.result !== null && "error" in part.result
      ? (part.result as { error?: unknown }).error
      : undefined
  const message =
    typeof error === "object" && error !== null && "message" in error
      ? (error as { message?: unknown }).message
      : undefined
  return typeof message === "string" && message.trim().length > 0
    ? message.trim().slice(0, 300)
    : "El paso no se pudo completar."
}

/** `result` exitoso de un paso, o `undefined` si sigue en curso o falló. */
export function toolResult<Result>(part: {
  result?: Result | AgentToolError
  isError?: boolean
}): Result | undefined {
  return part.isError ? undefined : (part.result as Result | undefined)
}

export const PLAN_STEPS = [
  { id: "understand", label: "Entender" },
  { id: "search", label: "Buscar" },
  { id: "choose", label: "Elegir" },
  { id: "checkout", label: "Checkout" },
  { id: "approval", label: "Aprobación" },
  { id: "complete", label: "Completar" },
] as const

export type PlanStepId = (typeof PLAN_STEPS)[number]["id"]
/** `stopped`: la política de compra detuvo la compra en ese paso; no es un error. */
export type PlanStepState = "pending" | "active" | "done" | "error" | "stopped"

export type PlanProgress = {
  /** Paso en curso; los anteriores se consideran hechos. `"done"` marca todo como hecho. */
  current: PlanStepId | "done" | null
  /** Paso que falló o se detuvo; los siguientes quedan pendientes. */
  failed?: PlanStepId | null
  /** Paso en que la política de compra detuvo la compra; los siguientes quedan pendientes. */
  stopped?: PlanStepId | null
  /** El paso en curso espera a la persona (por ejemplo, la aprobación) en vez de trabajar. */
  waiting?: boolean
}

export function planStepStates({ current, failed, stopped }: PlanProgress) {
  const ids = PLAN_STEPS.map((step) => step.id)
  const end = failed ?? stopped
  const failedIndex = end ? ids.indexOf(end) : -1
  const endState: PlanStepState = failed ? "error" : "stopped"
  const currentIndex =
    current === "done" ? ids.length : current === null ? -1 : ids.indexOf(current)

  return Object.fromEntries(
    ids.map((id, index): [PlanStepId, PlanStepState] => {
      if (failedIndex >= 0) {
        return [id, index < failedIndex ? "done" : index === failedIndex ? endState : "pending"]
      }
      if (index < currentIndex) return [id, "done"]
      if (index === currentIndex) return [id, "active"]
      return [id, "pending"]
    }),
  ) as Record<PlanStepId, PlanStepState>
}

/**
 * Pasos que el plan alcanzó, en orden y hasta el que está en curso, se detuvo o falló; con `"done"`
 * al final si terminó. Es lo que el plan visible recorre a ritmo (`usePacedPlan`).
 */
export function planReach({ current, failed, stopped }: PlanProgress) {
  const end = failed ?? stopped ?? current
  const ids: Array<PlanStepId | "done"> = PLAN_STEPS.map((step) => step.id)
  if (end === null) return []
  return end === "done" ? [...ids, "done"] : ids.slice(0, ids.indexOf(end) + 1)
}

/**
 * El plan que se ve cuando solo se revelaron algunos de los pasos alcanzados: el último revelado,
 * en curso. El estado real (detenido, falla, esperando, completado) solo cuando se revelaron todos.
 */
export function pacedPlanProgress(progress: PlanProgress, revealed: ReadonlySet<string>) {
  const reach = planReach(progress)
  const shown = reach.filter((id) => revealed.has(id)).length
  if (shown >= reach.length) return progress
  return { current: shown === 0 ? null : reach[shown - 1] } as PlanProgress
}

const TOOL_PLAN_STEP: Record<AgentToolName, PlanStepId> = {
  interpret_request: "understand",
  search_catalog: "search",
  select_product: "choose",
  prepare_checkout: "checkout",
  request_approval: "approval",
  purchase_update: "complete",
}

const NEXT_STEP_AFTER_RESULT: Partial<Record<AgentToolName, PlanStepId>> = {
  interpret_request: "search",
  search_catalog: "choose",
  select_product: "checkout",
  prepare_checkout: "approval",
}

/**
 * Deriva el plan de los tool calls del turno y de `state.purchase`. La etapa de la compra viene de
 * SQLite, así que manda sobre lo que sugieran los pasos.
 */
export function derivePlanProgress({
  parts,
  purchase,
  running,
}: {
  parts: ReadonlyArray<{ toolName: AgentToolName; result?: unknown; isError?: boolean }>
  purchase: AgentPurchase | null
  running: boolean
}): PlanProgress {
  switch (purchase?.stage) {
    case "completed":
      return { current: "done" }
    case "canceled":
      return { current: "approval", failed: "approval" }
    case "failed":
      return { current: "complete", failed: "complete" }
    case "approved":
    case "completing":
    case "uncertain":
      return { current: "complete" }
    case "awaiting_confirmation":
      return { current: "approval", waiting: true }
  }

  const last = parts.at(-1)
  if (!last) return { current: running ? "understand" : null }
  const step = TOOL_PLAN_STEP[last.toolName]
  if (last.isError) return { current: step, failed: step }
  if (last.result === undefined) return { current: step }
  const next = NEXT_STEP_AFTER_RESULT[last.toolName]
  return running && next ? { current: next } : { current: step, waiting: true }
}
