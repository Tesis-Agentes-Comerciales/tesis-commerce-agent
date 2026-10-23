import { cn } from "cn"
import { useState } from "react"

import type { PacedReveal } from "@/components/agent/pacing"
import { usePacedKeys } from "@/components/agent/use-pacing"
import { ControlStep } from "@/components/trace/control-step"
import {
  controlOrder,
  isSkipped,
  type ControlKind,
  type SkippedControl,
  traceMerchant,
} from "@/components/trace/trace-copy"
import type { PurchaseStatus, PurchaseTrace, StepKind, TraceStep } from "@/lib/trace-contract"

/** Compras que terminaron antes de tiempo: se listan los controles que ya no ocurrieron. */
const ENDED_EARLY: ReadonlySet<PurchaseStatus> = new Set([
  "blocked",
  // Con términos que ya no valen solo queda cancelar: ni firma, ni pago, ni orden.
  "stale_terms",
  "canceled",
  "failed",
])

type TimelineRow = TraceStep | SkippedControl

/**
 * Dónde va la política apagada: justo después de la elección, que es cuando se habría revisado. Lo
 * que siga (el checkout o un fallo al crearlo) ocurrió después. Sin elección (una compra delegada no
 * la trae) va antes del primer control posterior; si no hay ninguno, todavía no tiene sitio.
 */
function policyOffSlot(rows: TimelineRow[], order: ControlKind[]) {
  const selection = rows.findLastIndex((row) => row.kind === "selection")
  if (selection !== -1) return selection + 1
  const policy = order.indexOf("policy_decision")
  return rows.findIndex((row) => order.indexOf(row.kind as ControlKind) > policy)
}

/**
 * La traza solo trae lo que pasó. Con la política apagada se intercala su control como desactivado
 * en el orden real (ver `policyOffSlot`). Si la compra terminó antes de tiempo se agregan al final
 * los controles que no alcanzó: así se ve qué no ocurrió (ni checkout ni cobro); con la política
 * apagada, la suya sigue diciendo «desactivada». Una compra por confirmar no los agrega, porque no se
 * sabe.
 */
function timelineRows(trace: PurchaseTrace): TimelineRow[] {
  const rows: TimelineRow[] = [...trace.steps]
  const order = controlOrder(trace)
  const policyOff = trace.policy_mode === "off"
  const slot = policyOff ? policyOffSlot(rows, order) : -1
  if (slot !== -1) rows.splice(slot, 0, { kind: "policy_decision", reason: "policy_off" })
  if (ENDED_EARLY.has(trace.status)) {
    const reached = new Set<StepKind>(rows.map((row) => row.kind))
    for (const kind of order) {
      if (reached.has(kind)) continue
      const reason = policyOff && kind === "policy_decision" ? "policy_off" : "not_reached"
      rows.push({ kind, reason })
    }
  }
  return rows
}

export type ControlTimelineProps = {
  trace: PurchaseTrace
  /**
   * Ritmo de las filas nuevas (`GOVERNANCE_ROW_MS`), sin cambiar su texto ni su orden. Lo crea quien
   * monta la línea de tiempo (`usePacer`): así, al plegarla y volver a abrirla, las filas que ya se
   * vieron salen enteras y no se animan otra vez. Sin él, todas a la vez.
   */
  pacer?: PacedReveal | null
  className?: string
}

export function ControlTimeline({ trace, pacer = null, className }: ControlTimelineProps) {
  const merchant = traceMerchant(trace)
  const rows = timelineRows(trace).map((row) => ({
    row,
    key: isSkipped(row) ? `${row.reason}:${row.kind}` : row.id,
  }))
  const shown = usePacedKeys(rows.map(({ key }) => key), pacer)
  // Se funden solo las filas que entran mientras la línea de tiempo está a la vista: no las que ya
  // estaban al montarla (al volver a abrirla, o en un mensaje del historial).
  const [seen] = useState(() => shown)
  return (
    <ol aria-label="Controles de la compra" className={cn("pl-0.5", className)}>
      {rows
        .filter(({ key }) => shown.has(key))
        .map(({ row, key }) => (
          <ControlStep
            key={key}
            step={row}
            merchant={merchant}
            context={trace}
            className={pacer && !seen.has(key) ? "agent-enter" : undefined}
          />
        ))}
    </ol>
  )
}
