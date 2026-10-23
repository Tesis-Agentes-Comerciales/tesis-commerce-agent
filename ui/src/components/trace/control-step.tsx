import { cn } from "cn"

import { formatDuration } from "@/components/agent/agent-format"
import { FreshnessChanges } from "@/components/trace/freshness-changes"
import { PolicyRuleTable } from "@/components/trace/policy-rule-table"
import { StepStatusIcon } from "@/components/trace/step-status-icon"
import {
  STEP_STATE_LABEL,
  isSkipped,
  skippedDetail,
  skippedTitle,
  stepDetail,
  stepTitle,
  type MerchantLabel,
  type SkippedControl,
  type StepContext,
} from "@/components/trace/trace-copy"
import type { TraceStep } from "@/lib/trace-contract"

import "@/components/agent/agent.css"

export type ControlStepProps = {
  /** Paso de la traza, o un control que la compra no alcanzó. */
  step: TraceStep | SkippedControl
  /** Comercio de la compra, con nombre: lo dicen el checkout y la regla de comercios. */
  merchant?: MerchantLabel
  /** La compra de la fila: si es delegada (HNP) cambian algunos textos. */
  context?: StepContext
  className?: string
}

function describe(step: TraceStep | SkippedControl, merchant?: MerchantLabel, context?: StepContext) {
  if (isSkipped(step)) {
    return {
      state: "skipped" as const,
      title: skippedTitle(step),
      detail: skippedDetail(step),
      duration: null,
    }
  }
  return {
    state: step.state,
    title: stepTitle(step, context),
    detail: stepDetail(step, merchant?.name, context),
    duration: step.duration_ms === null ? null : formatDuration(step.duration_ms),
  }
}

/** Fila de la línea de tiempo de controles; `ControlTimeline` aporta el `<ol>`. */
export function ControlStep({ step, merchant, context, className }: ControlStepProps) {
  const { state, title, detail, duration } = describe(step, merchant, context)
  const skipped = state === "skipped"

  return (
    <li className={cn("group/step relative flex gap-3 pb-4 last:pb-0", className)} data-state={state}>
      <span
        className="absolute top-6 bottom-1 left-[9.5px] w-px bg-[#e5e5e5] group-last/step:hidden"
        aria-hidden
      />
      <StepStatusIcon state={state} hideLabel className="self-start" />
      <div className="min-w-0 flex-1">
        <div className="flex min-h-5 items-baseline justify-between gap-3">
          <p
            className={cn(
              "min-w-0 text-sm leading-5 font-medium wrap-break-word",
              skipped && "font-normal text-[#6b6b6b]",
              state === "error" && "text-red-700",
            )}
          >
            {state === "running" ? <span className="agent-shimmer">{title}</span> : title}
          </p>
          {/* El título de un control omitido es un infinitivo: el estado se escribe a la vista. */}
          {skipped ? (
            <span className="shrink-0 text-xs text-[#6b6b6b]" aria-hidden>
              {STEP_STATE_LABEL.skipped}
            </span>
          ) : (
            duration && <span className="shrink-0 text-xs text-[#6b6b6b] tabular-nums">{duration}</span>
          )}
        </div>
        {detail && <p className="mt-0.5 text-sm leading-5 text-[#6b6b6b] wrap-break-word">{detail}</p>}
        {/* La revisión de la elección también trae `decision`, pero no es de la política. */}
        {"decision" in step && step.kind !== "choice_reviewed" && (
          <PolicyRuleTable
            rules={step.decision.rules}
            values="failed"
            merchant={merchant}
            className="mt-2"
          />
        )}
        {"changes" in step && <FreshnessChanges step={step} className="mt-2" />}
      </div>
    </li>
  )
}
