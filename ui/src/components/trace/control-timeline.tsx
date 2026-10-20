import { cn } from "cn"

import { ControlStep } from "@/components/trace/control-step"
import { CONTROL_ORDER, type SkippedControl } from "@/components/trace/trace-copy"
import type { PurchaseStatus, PurchaseTrace, StepKind, TraceStep } from "@/lib/trace-contract"

/** Compras que terminaron antes de tiempo: se listan los controles que ya no ocurrieron. */
const ENDED_EARLY: ReadonlySet<PurchaseStatus> = new Set(["blocked", "canceled", "failed"])

type TimelineRow = TraceStep | SkippedControl

function controlIndex(kind: StepKind) {
  return (CONTROL_ORDER as StepKind[]).indexOf(kind)
}

/**
 * La traza solo trae lo que pasó. Con la política apagada se intercala su control como desactivado,
 * una vez elegido el producto: antes, la fila quedaría delante de una elección que aún no ocurrió. Si
 * la compra terminó antes de tiempo se agregan al final los controles que no alcanzó: así se ve qué
 * no ocurrió (ni checkout ni cobro). Una compra por confirmar no los agrega, porque no se sabe.
 */
function timelineRows(trace: PurchaseTrace): TimelineRow[] {
  const rows: TimelineRow[] = [...trace.steps]
  const selected = rows.some((row) => controlIndex(row.kind) >= controlIndex("selection"))
  if (trace.policy_mode === "off" && selected) {
    const policy = controlIndex("policy_decision")
    const after = rows.findIndex((row) => controlIndex(row.kind) > policy)
    rows.splice(after === -1 ? rows.length : after, 0, {
      kind: "policy_decision",
      reason: "policy_off",
    })
  }
  if (ENDED_EARLY.has(trace.status)) {
    const reached = new Set(rows.map((row) => row.kind))
    for (const kind of CONTROL_ORDER) {
      if (!reached.has(kind)) rows.push({ kind, reason: "not_reached" })
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
