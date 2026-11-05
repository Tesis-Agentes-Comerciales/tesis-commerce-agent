import { cn } from "cn"
import { useId } from "react"

import { chip } from "@/components/agent/agent-styles"
import { branchDetail, branchStatus, CONDITIONAL_COPY } from "@/components/agent/conditional-copy"
import type { ConditionalView } from "@/lib/agent-contract"

export type ConditionalBranchesProps = {
  /** `select_product.conditional`, ya validada con `parseConditionalView`. */
  view: ConditionalView
  className?: string
}

/**
 * Las opciones de una instrucción condicional, en el orden del comprador (p0-conditional-intent.md
 * §3.10): por rama, su título, su estado, el total con envío y el límite, y cuál se eligió. La rama
 * la decidió el backend con la oferta del comercio; aquí solo se muestra. No es una región: dos
 * turnos condicionales en el mismo hilo repetirían su nombre.
 */
export function ConditionalBranches({ view, className }: ConditionalBranchesProps) {
  const titleId = useId()
  return (
    <div
      data-testid="conditional-branches"
      data-outcome={view.outcome}
      className={cn(
        "w-full max-w-[30rem] rounded-[20px] border border-[#ececec] bg-white p-4 text-[#0d0d0d]",
        className,
      )}
    >
      <h3 id={titleId} className="text-sm leading-5 font-semibold">
        {CONDITIONAL_COPY.title}
      </h3>
      <ol aria-labelledby={titleId} className="mt-2 space-y-1">
        {view.branches.map((branch) => {
          const chosen = view.chosen_index === branch.index
          const status = branchStatus(view, branch)
          const detail = branchDetail(view, branch)
          return (
            <li
              key={branch.index}
              data-status={branch.status}
              data-chosen={chosen || undefined}
              className={cn("flex gap-3 rounded-[14px] px-2.5 py-2", chosen && "bg-[#f7f7f7]")}
            >
              <span
                aria-hidden
                className={cn(
                  "mt-px flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold tabular-nums",
                  chosen ? "bg-[#0d0d0d] text-white" : "bg-[#f2f2f2] text-[#525252]",
                )}
              >
                {branch.index + 1}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
                  <p className="min-w-0 text-sm leading-5 font-medium wrap-break-word">
                    {branch.title}
                  </p>
                  <span className={chip({ tone: status.tone, size: "sm" })}>{status.label}</span>
                </div>
                {detail && (
                  <p className="mt-0.5 text-xs leading-5 wrap-break-word text-[#6b6b6b] tabular-nums">
                    {detail}
                  </p>
                )}
              </div>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
