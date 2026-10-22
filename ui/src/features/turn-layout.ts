import { policyBlockOf, stepErrorCode } from "@/features/policy-block"

// Qué ocupa cada lugar del mensaje mientras el turno corre (docs/design/trace-panel.md, «Mientras
// carga un turno»). Funciones puras: el mensaje las llama con sus partes.

type MessagePart = {
  type: string
  toolName?: string
  text?: string
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
