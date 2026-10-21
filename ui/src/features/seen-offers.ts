import { asSearchResult } from "@/features/agent-results"
import type { ProductCard } from "@/lib/agent-contract"

/** Lo que `seenOffers` lee de un mensaje: sirve el del estado del agente o el ya convertido. */
type SeenMessage = {
  role: string
  content: readonly { type: string; toolName?: string; result?: unknown; isError?: boolean }[]
}

/**
 * Las opciones que el comprador ya vio en el chat (`search_catalog`), sin repetir, disponibles y en
 * pesos: lo que la propuesta acepta (§5.1). Salen del feed, no del texto del modelo, y el servidor
 * vuelve a validar cada variante contra el catálogo.
 */
export function seenOffers(messages: readonly SeenMessage[]): ProductCard[] {
  const offers = new Map<string, ProductCard>()
  for (const message of messages) {
    if (message.role !== "assistant") continue
    for (const part of message.content) {
      if (part.type !== "tool-call" || part.toolName !== "search_catalog" || part.isError) continue
      for (const product of asSearchResult(part.result)?.products ?? []) {
        if (product.available && product.currency.toUpperCase() === "COP") {
          offers.set(product.variant_id, product)
        }
      }
    }
  }
  return [...offers.values()]
}
