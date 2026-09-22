import type { ApprovalTerms } from "@/lib/commerce-types"

// Tipos del contrato Assistant Transport (docs/agent-ui-contract.md). Todo lo que llega del backend
// es no confiable: los componentes validan URLs y toleran campos ausentes antes de mostrarlos.

export type { ApprovalTerms }

/** Etapas de `PurchaseStage` leídas de SQLite; nunca se deducen del estado del cliente. */
export type PurchaseStage =
  | "received"
  | "selected"
  | "checkout_prepared"
  | "awaiting_confirmation"
  | "approved"
  | "completing"
  | "uncertain"
  | "completed"
  | "canceled"
  | "failed"

/** Estados de un checkout ACP. */
export type CheckoutStatus =
  | "not_ready_for_payment"
  | "ready_for_payment"
  | "completed"
  | "canceled"
  | "expired"

// Intención (JSON de `PurchaseIntent`). El backend normaliza textos a minúsculas.

export type OptionConstraint = {
  name: string
  value: string
}

export type HardConstraints = {
  currency: string
  max_unit_amount_minor: number | null
  max_total_amount_minor: number | null
  categories: string[]
  conditions: string[]
  required_options: OptionConstraint[]
  required_terms: string[]
}

export type SoftPreferences = {
  terms: string[]
  categories: string[]
  conditions: string[]
  options: OptionConstraint[]
  selection_strategy: "lowest_observed_price" | null
}

/** Motivos de descarte que cuenta `rank_candidates`. */
export type RejectionReason =
  | "unavailable"
  | "currency"
  | "budget"
  | "category"
  | "condition"
  | "option"
  | "term"

// ProductCard: proyección de presentación del feed.

export type ProductImage = {
  url: string
  alt: string
}

export type ProductOption = {
  name: string
  value: string
}

export type ProductCard = {
  product_id: string
  variant_id: string
  title: string
  variant_title: string
  /** Texto plano truncado a 500 caracteres; `null` si el feed no lo trae. */
  description: string | null
  /** Solo https, sin credenciales y del mismo host que `product_url`; si no, `null`. */
  image: ProductImage | null
  /** Precio observado en el catálogo, no un término autoritativo. */
  observed_unit_amount_minor: number
  currency: string
  available: boolean
  options: ProductOption[]
  /** El backend lo envía `null` si la URL del feed no es http(s) válida. */
  product_url: string | null
}

// Pasos que orquesta el backend, expuestos como tool calls.

export type InterpretAction = "respond" | "clarify" | "purchase"

export type InterpretRequestArgs = Record<string, never>

export type InterpretRequestResult = {
  action: InterpretAction
  constraints: HardConstraints | null
  preferences: SoftPreferences | null
}

export type SearchCatalogArgs = {
  constraints: HardConstraints | null
  preferences: SoftPreferences | null
}

export type SearchCatalogResult = {
  matches: number
  /** Conteos por motivo; las claves pueden crecer, por eso no se restringen a `RejectionReason`. */
  rejections: Record<string, number>
  /** Máximo 6 productos. */
  products: ProductCard[]
}

export type SelectProductArgs = {
  candidate_variant_ids: string[]
}

export type SelectProductResult = {
  decision: "select" | "clarify"
  product: ProductCard | null
  rationale: string
}

export type PrepareCheckoutArgs = {
  variant_id: string
}

export type PrepareCheckoutResult = {
  run_id: string
  checkout_id: string
  checkout_status: CheckoutStatus
  price_changed: boolean
  total_minor: number
  currency: string
}

export type RequestApprovalArgs = {
  run_id: string
}

/** Igual a `GET /purchases/{run_id}/approval`. */
export type RequestApprovalResult = {
  run_id: string
  stage: PurchaseStage
  terms: ApprovalTerms
}

export type PurchaseUpdateArgs = {
  run_id: string
}

export type PurchaseUpdateResult = {
  run_id: string
  stage: PurchaseStage
  order_id: string | null
}

/** `result` de un paso con `isError: true` (lo emite el backend aunque la tabla no lo liste). */
export type AgentToolError = {
  error: { code: string; message: string }
}

export type AgentTools = {
  interpret_request: { args: InterpretRequestArgs; result: InterpretRequestResult }
  search_catalog: { args: SearchCatalogArgs; result: SearchCatalogResult }
  select_product: { args: SelectProductArgs; result: SelectProductResult }
  prepare_checkout: { args: PrepareCheckoutArgs; result: PrepareCheckoutResult }
  request_approval: { args: RequestApprovalArgs; result: RequestApprovalResult }
  purchase_update: { args: PurchaseUpdateArgs; result: PurchaseUpdateResult }
}

export type AgentToolName = keyof AgentTools

export const AGENT_TOOL_NAMES = [
  "interpret_request",
  "search_catalog",
  "select_product",
  "prepare_checkout",
  "request_approval",
  "purchase_update",
] as const satisfies readonly AgentToolName[]

// Mensajes (compatibles con `ThreadMessageLike`).

export type AgentTextPart = {
  type: "text"
  text: string
}

/** Resumen de razonamiento de la Responses API; nunca razonamiento crudo. */
export type AgentReasoningPart = {
  type: "reasoning"
  text: string
}

export type AgentToolCallPart<Name extends AgentToolName = AgentToolName> = {
  [Key in Name]: {
    type: "tool-call"
    toolCallId: string
    toolName: Key
    args: AgentTools[Key]["args"]
    argsText: string
    /** Falta mientras el paso está en curso; con `isError` trae un `AgentToolError`. */
    result?: AgentTools[Key]["result"] | AgentToolError
    isError?: boolean
  }
}[Name]

export type AgentAssistantPart = AgentTextPart | AgentReasoningPart | AgentToolCallPart

export type AgentMessageStatus =
  | { type: "running" }
  | { type: "complete"; reason: "stop" }
  | { type: "incomplete"; reason: "error"; error: string }
  | { type: "incomplete"; reason: "cancelled" }

export type AgentUserMessage = {
  id: string
  role: "user"
  content: AgentTextPart[]
}

export type AgentAssistantMessage = {
  id: string
  role: "assistant"
  status: AgentMessageStatus
  content: AgentAssistantPart[]
}

export type AgentMessage = AgentUserMessage | AgentAssistantMessage

/** Etapa de la compra leída de SQLite; `null` hasta que exista un checkout. */
export type AgentPurchase = {
  run_id: string
  stage: PurchaseStage
  order_id: string | null
}

export type AgentState = {
  messages: AgentMessage[]
  purchase: AgentPurchase | null
}

export const INITIAL_AGENT_STATE: AgentState = { messages: [], purchase: null }

/**
 * Máximo de caracteres de una parte de texto (`MAX_MESSAGE_CHARS` en `assistant_transport.py`).
 * Pasarse devuelve un 422 genérico, así que el compositor no deja llegar hasta ahí.
 */
export const MAX_MESSAGE_CHARS = 2_000

// Comandos. Ninguno transporta evidencia de aprobación: esa va solo por REST.

export type AddMessageCommand = {
  type: "add-message"
  message: { role: "user"; parts: AgentTextPart[] }
  parentId: string | null
  sourceId: string | null
}

export type PurchaseUpdatedCommand = {
  type: "purchase-updated"
  runId: string
}

export type AgentCommand = AddMessageCommand | PurchaseUpdatedCommand
