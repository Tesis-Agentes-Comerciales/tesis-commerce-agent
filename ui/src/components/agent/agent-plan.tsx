import { cn } from "cn"
import { CheckIcon, LoaderCircleIcon, XIcon } from "lucide-react"
import { useId } from "react"

import {
  PLAN_STEPS,
  planStepStates,
  type PlanProgress,
  type PlanStepState,
} from "@/components/agent/agent-steps"

import "./agent.css"

const STATE_TEXT: Record<PlanStepState, string> = {
  pending: "pendiente",
  active: "en curso",
  done: "hecho",
  error: "detenido",
}

export type AgentPlanProps = PlanProgress & {
  /** Nota bajo el paso detenido, por ejemplo "Cancelaste la compra". */
  failedNote?: string
  className?: string
}

function StepIcon({ state, waiting }: { state: PlanStepState; waiting: boolean }) {
  return (
    <span
      className={cn(
        "flex size-5 shrink-0 items-center justify-center rounded-full",
        state === "done" && "bg-[#0d0d0d] text-white",
        state === "active" && "text-[#0d0d0d]",
        state === "active" && waiting && "ring-[1.5px] ring-[#0d0d0d] ring-inset",
        state === "pending" && "ring-[1.5px] ring-[#8e8e8e] ring-inset",
        state === "error" && "bg-red-600 text-white",
      )}
      aria-hidden
    >
      {state === "done" && <CheckIcon className="size-3" strokeWidth={3} />}
      {state === "active" &&
        (waiting ? (
          <span className="agent-pulse-dot size-2 rounded-full bg-[#0d0d0d]" />
        ) : (
          <LoaderCircleIcon className="size-5 motion-safe:animate-spin" strokeWidth={2.25} />
        ))}
      {state === "error" && <XIcon className="size-3" strokeWidth={3} />}
    </span>
  )
}

/** Recorrido de la compra: Entender, Buscar, Elegir, Checkout, Aprobación y Completar. */
export function AgentPlan({ current, failed, waiting = false, failedNote, className }: AgentPlanProps) {
  const titleId = useId()
  const states = planStepStates({ current, failed })
  const doneCount = PLAN_STEPS.filter((step) => states[step.id] === "done").length
  const percent = Math.round((doneCount / PLAN_STEPS.length) * 100)

  return (
    <section
      aria-labelledby={titleId}
      className={cn(
        "@container w-full rounded-[16px] border border-[#ececec] bg-white p-4",
        className,
      )}
      data-testid="agent-plan"
    >
      <div className="flex items-baseline justify-between gap-3">
        <h3 id={titleId} className="text-sm font-semibold">
          Plan de compra
        </h3>
        <p className="text-xs text-[#6b6b6b] tabular-nums">
          {doneCount} de {PLAN_STEPS.length}
        </p>
      </div>

      <div
        className="mt-3 h-1 overflow-hidden rounded-full bg-[#ececec] @xl:hidden"
        role="progressbar"
        aria-label="Progreso de la compra"
        aria-valuemin={0}
        aria-valuemax={PLAN_STEPS.length}
        aria-valuenow={doneCount}
      >
        <div
          className={cn(
            "h-full rounded-full transition-[width] duration-500",
            failed ? "bg-red-600" : "bg-[#0d0d0d]",
          )}
          style={{ width: `${percent}%` }}
        />
      </div>

      <ol className="mt-3 grid gap-2.5 @xl:mt-4 @xl:grid-cols-6 @xl:gap-2">
        {PLAN_STEPS.map((step) => {
          const state = states[step.id]
          const note =
            state === "error"
              ? failedNote
              : state === "active"
                ? waiting
                  ? step.id === "approval"
                    ? "Esperando tu aprobación"
                    : "Esperando tu respuesta"
                  : "En curso"
                : undefined

          return (
            <li
              key={step.id}
              aria-current={state === "active" ? "step" : undefined}
              className="flex min-w-0 items-center gap-2.5 @xl:flex-col @xl:items-start @xl:gap-2"
            >
              <span
                className={cn(
                  "hidden h-1 w-full rounded-full @xl:block",
                  state === "done" && "bg-[#0d0d0d]",
                  state === "active" && "bg-[#0d0d0d]/35",
                  state === "pending" && "bg-[#ececec]",
                  state === "error" && "bg-red-600",
                )}
                aria-hidden
              />
              <div className="flex min-w-0 items-center gap-2.5 @xl:gap-1.5">
                <StepIcon state={state} waiting={waiting} />
                <div className="min-w-0">
                  <p
                    className={cn(
                      "truncate text-sm leading-5",
                      state === "active" && "font-semibold text-[#0d0d0d]",
                      state === "done" && "text-[#0d0d0d]",
                      state === "pending" && "text-[#6b6b6b]",
                      state === "error" && "font-semibold text-red-700",
                    )}
                  >
                    {step.label}
                    <span className="sr-only"> ({STATE_TEXT[state]})</span>
                  </p>
                  {note && (
                    <p
                      className={cn(
                        "text-xs leading-4 @xl:hidden",
                        state === "error" ? "text-red-700" : "text-[#6b6b6b]",
                      )}
                    >
                      {note}
                    </p>
                  )}
                </div>
              </div>
            </li>
          )
        })}
      </ol>

      {/* En horizontal no caben notas por paso: se resume la del paso detenido. */}
      {failed && failedNote && (
        <p className="mt-3 hidden text-xs text-red-700 @xl:block">{failedNote}</p>
      )}
    </section>
  )
}
