import type { StreamPurchase } from "@/components/agent"
import { streamPurchaseSummary } from "@/components/trace/trace-copy"
import { asSelectResult } from "@/features/agent-results"
import { policyBlockOf, stepErrorCode } from "@/features/policy-block"
import { runIdOf } from "@/features/purchase-trace"

// Qué ocupa cada lugar del mensaje mientras el turno corre (docs/design/trace-panel.md, «Mientras
// carga un turno»). Funciones puras: el mensaje las llama con sus partes.

type MessagePart = {
  type: string
  toolName?: string
  text?: string
  args?: unknown
  result?: unknown
  isError?: boolean
}

/**
 * El mensaje solo trae `purchase_update`: es el seguimiento que envía la tarjeta al aprobar o
 * cancelar. No es un turno de compra: va sin plan y sin grupo «Pensando…».
 */
export function isFollowUpOnly(parts: readonly MessagePart[]) {
  const tools = parts.filter((part) => part.type === "tool-call")
  return tools.length > 0 && tools.every((part) => part.toolName === "purchase_update")
}

/**
 * El turno es «Delegar» (docs/design/hnp-chat-delegate.md): no lleva el plan HP, porque nadie
 * aprueba al final y su seguimiento es la tarjeta del hilo. Mientras corre lo dice el modo del chat;
 * al terminar, el borrador que dejó en el estado (`delegating` ya puede haber vuelto a «Comprar
 * conmigo»). Solo el último mensaje: una compra HP de un mensaje anterior conserva su plan.
 */
export function isDelegateTurn({
  latest,
  active,
  delegating,
  drafted,
}: {
  /** La persona no volvió a escribir después de este mensaje. */
  latest: boolean
  /** El mensaje está en curso. */
  active: boolean
  /** El chat está en «Delegar». */
  delegating: boolean
  /** El estado trae `delegation_draft`, aunque sea `null`: lo deja todo turno `delegate`. */
  drafted: boolean
}) {
  return latest && (drafted || (active && delegating))
}

export type ProductSlot = "marker" | "card" | "none"

/**
 * Lugar del producto elegido. Mientras el agente elige, o mientras prepara el checkout del producto
 * que eligió, un marcador bajo. La tarjeta grande entra con el checkout listo (o si el turno terminó
 * sin él). Si la política detuvo la compra, el producto va dentro de la tarjeta de bloqueo: así la
 * tarjeta grande nunca aparece para irse un instante después.
 */
export function productSlot({
  select,
  prepare,
  messageRunning,
}: {
  select: "running" | "complete"
  prepare: MessagePart | undefined
  messageRunning: boolean
}): ProductSlot {
  if (select === "running") return "marker"
  if (policyBlockOf(stepErrorCode(prepare))) return "none"
  if (messageRunning && (!prepare || prepare.result === undefined)) return "marker"
  return "card"
}

function toolPart(parts: readonly MessagePart[], toolName: string) {
  return parts.find((part) => part.type === "tool-call" && part.toolName === toolName)
}

/**
 * La fila de actividad según el stream, desde que `prepare_checkout` empieza con el `run_id` en sus
 * `args`: sale a su altura final y la traza solo la completa. `null` si el mensaje no empezó una
 * compra, o si el paso falló por otra cosa que la política (eso lo explica su aviso).
 */
export function streamPurchaseOf(parts: readonly MessagePart[]): StreamPurchase | null {
  const prepare = toolPart(parts, "prepare_checkout")
  if (!prepare || runIdOf(prepare.args) === null) return null
  const select = toolPart(parts, "select_product")
  const product = select?.isError ? null : (asSelectResult(select?.result)?.product ?? null)
  // Como lo nombra la traza: la variante; sin ella, el producto.
  const name = product?.variant_title || product?.title || null
  const reason = policyBlockOf(stepErrorCode(prepare))
  if (reason) return { status: "blocked", summary: streamPurchaseSummary("blocked", reason), product: name }
  if (prepare.isError) return null
  const approval = toolPart(parts, "request_approval")
  const status =
    approval && approval.result !== undefined && !approval.isError ? "awaiting_approval" : "in_progress"
  return { status, summary: streamPurchaseSummary(status), product: name }
}
