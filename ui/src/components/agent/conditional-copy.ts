import { formatMoney } from "@/components/agent/agent-format"
import type { ConditionalBranchView, ConditionalView } from "@/lib/agent-contract"

// Lo que dice cada rama de una intención condicional (docs/design/p0-conditional-intent.md §3.8 y
// §3.10). Solo hechos de la vista, que decidió el backend con la oferta del comercio: el texto del
// turno ya explica la decisión, así que aquí no se repite.

export const CONDITIONAL_COPY = {
  title: "Tus opciones, en orden",
} as const

export type BranchTone = "success" | "neutral" | "warning"

const STATUS_LABEL: Record<ConditionalBranchView["status"], string> = {
  eligible: "Cumple",
  over_limit: "Supera tu límite",
  out_of_stock: "Sin stock",
  inactive: "No está a la venta",
  intent_mismatch: "No cumple tu pedido",
  unknown: "Sin verificar",
  not_triggered: "No aplicaba",
  not_evaluated: "Sin revisar",
}

const isChosen = (view: ConditionalView, branch: ConditionalBranchView) =>
  view.chosen_index === branch.index

/** El estado de la rama en una etiqueta; el color nunca es lo único que lo dice. */
export function branchStatus(view: ConditionalView, branch: ConditionalBranchView) {
  if (isChosen(view, branch)) {
    return view.outcome === "offer_changed"
      ? { label: "El total cambió", tone: "warning" as BranchTone }
      : { label: "Elegida", tone: "success" as BranchTone }
  }
  const tone: BranchTone = branch.status === "unknown" ? "warning" : "neutral"
  return { label: STATUS_LABEL[branch.status], tone }
}

const cop = (minor: number) => formatMoney(minor, "COP")

/** El contrato: un `over_limit` dentro del límite de total (o sin él) es por precio unitario. */
function overUnitLimit({ total_minor, limit_minor, unit_limit_minor }: ConditionalBranchView) {
  return unit_limit_minor !== null && (limit_minor === null || (total_minor ?? 0) <= limit_minor)
}

function totals({ total_minor, limit_minor }: ConditionalBranchView) {
  if (total_minor === null) return null
  const total = `Total con envío ${cop(total_minor)}`
  return limit_minor === null ? total : `${total} · Tu límite ${cop(limit_minor)}`
}

/** Los montos o el motivo de la rama, en una línea; `null` si la etiqueta ya lo dice todo. */
export function branchDetail(view: ConditionalView, branch: ConditionalBranchView) {
  const previous = view.branches[branch.index - 1]
  switch (branch.status) {
    case "eligible":
      if (view.outcome === "offer_changed" && isChosen(view, branch)) {
        const checkout = view.checkout_total_minor ?? 0
        return `Verificado ${cop(branch.total_minor ?? 0)} · En el checkout ${cop(checkout)}`
      }
      return totals(branch)
    case "over_limit":
      if (overUnitLimit(branch) && branch.items_total_minor !== null) {
        const unit = cop(branch.unit_limit_minor ?? 0)
        return `Precio sin envío ${cop(branch.items_total_minor)} · Tu límite por unidad ${unit}`
      }
      return totals(branch)
    case "unknown":
      return "No pude verificarla con el comercio."
    case "not_triggered":
      return previous ? `Solo si ${previous.title} se agotaba.` : null
    case "not_evaluated": {
      if (view.outcome !== "undetermined") return "Ya elegí una opción anterior."
      const unverified = view.branches.find((item) => item.status === "unknown")
      return unverified ? `No la revisé sin saber si ${unverified.title} cumplía.` : null
    }
    default:
      return null
  }
}
