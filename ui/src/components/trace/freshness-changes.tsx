import { cn } from "cn"

import { FRESHNESS_CHANGES_LABEL, freshnessChanges } from "@/components/trace/trace-copy"
import type { TraceStep } from "@/lib/trace-contract"

export type FreshnessChangesProps = {
  step: Extract<TraceStep, { kind: "freshness_check" }>
  className?: string
}

/**
 * Qué cambió entre los términos del checkout y la oferta actual del comercio. Misma forma que las
 * reglas de la política (término sobre valor), pero es un control de la aplicación: no lleva
 * resultado de regla.
 */
export function FreshnessChanges({ step, className }: FreshnessChangesProps) {
  const changes = freshnessChanges(step)
  if (changes.length === 0) return null
  return (
    <ul
      aria-label={FRESHNESS_CHANGES_LABEL}
      className={cn("divide-y divide-[#ececec] rounded-[16px] border border-[#ececec]", className)}
    >
      {changes.map((change) => (
        <li key={change.field} className="px-3.5 py-2.5" data-field={change.field}>
          <p className="min-w-0 text-sm leading-5 font-medium wrap-break-word">{change.name}</p>
          <dl className="mt-1.5 space-y-1.5 text-xs leading-4">
            <ChangeValue label={change.labels.presented} value={change.presented} />
            <ChangeValue label={change.labels.current} value={change.current} />
          </dl>
        </li>
      ))}
    </ul>
  )
}

function ChangeValue({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[#6b6b6b]">{label}</dt>
      <dd className="mt-0.5 text-[#0d0d0d] tabular-nums wrap-break-word">{value}</dd>
    </div>
  )
}
