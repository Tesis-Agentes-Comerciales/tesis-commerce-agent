import { cn } from "cn"

import { ControlStep } from "@/components/trace/control-step"
import { CONTROL_ORDER, type SkippedControl } from "@/components/trace/trace-copy"
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

function controlIndex(kind: StepKind) {
  return (CONTROL_ORDER as StepKind[]).indexOf(kind)
}

const POLICY = controlIndex("policy_decision")

/**
 * Dónde va la política apagada: justo después de la elección, que es cuando se habría revisado. Lo
 * que siga (el checkout o un fallo al crearlo) ocurrió después. Sin elección no tiene sitio aún,
 * salvo que la traza ya traiga un control posterior.
 */
function policyOffSlot(rows: TimelineRow[]) {
  const selection = rows.findLastIndex((row) => row.kind === "selection")
  if (selection !== -1) return selection + 1
  return rows.findIndex((row) => controlIndex(row.kind) > POLICY)
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
  const policyOff = trace.policy_mode === "off"
  const slot = policyOff ? policyOffSlot(rows) : -1
  if (slot !== -1) rows.splice(slot, 0, { kind: "policy_decision", reason: "policy_off" })
  if (ENDED_EARLY.has(trace.status)) {
    const reached = new Set(rows.map((row) => row.kind))
    for (const kind of CONTROL_ORDER) {
      if (reached.has(kind)) continue
      const reason = policyOff && kind === "policy_decision" ? "policy_off" : "not_reached"
      rows.push({ kind, reason })
    }
  }
  return rows
}

export type ControlTimelineProps = {
  trace: PurchaseTrace
  className?: string
}

export function ControlTimeline({ trace, className }: ControlTimelineProps) {
  return (
    <ol aria-label="Controles de la compra" className={cn("pl-0.5", className)}>
      {timelineRows(trace).map((row) => (
        <ControlStep key={"reason" in row ? `${row.reason}:${row.kind}` : row.id} step={row} />
      ))}
    </ol>
  )
}
