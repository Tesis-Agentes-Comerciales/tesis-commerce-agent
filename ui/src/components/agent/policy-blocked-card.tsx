import { cn } from "cn"
import { CircleSlashIcon, ShieldXIcon } from "lucide-react"
import { useId } from "react"

import { PolicyRuleTable } from "@/components/trace/policy-rule-table"
import { TechnicalDetails } from "@/components/trace/technical-details"
import { decisionRefs, POLICY_MODE_LABEL, policyUnavailable } from "@/components/trace/trace-copy"
import type { PolicyDecision } from "@/lib/trace-contract"

export type PolicyBlockedCardProps = {
  /** La decisión que detuvo la compra (`effect: "blocked"`). */
  decision: PolicyDecision
  className?: string
}

function blockedCopy(decision: PolicyDecision) {
  if (policyUnavailable(decision)) {
    return {
      title: "No se pudo verificar la política de compra",
      subtitle: "Por precaución, la compra se detuvo.",
    }
  }
  const failed = decision.rules.filter((rule) => rule.result === "fail").length
  return {
    title: "La política de compra detuvo esta compra",
    subtitle: failed === 1 ? "No se cumplió una regla." : `No se cumplieron ${failed} reglas.`,
  }
}

/**
 * Tarjeta fuera del plegable: qué regla no se cumplió y qué no ocurrió. Un bloqueo es la política
 * funcionando, no un error, así que va en ámbar y nunca dice «error inesperado».
 */
export function PolicyBlockedCard({ decision, className }: PolicyBlockedCardProps) {
  const titleId = useId()
  const copy = blockedCopy(decision)
  const failedRules = decision.rules.filter((rule) => rule.result === "fail")

  return (
    <section
      aria-labelledby={titleId}
      data-testid="policy-blocked-card"
      className={cn(
        "w-full max-w-[30rem] overflow-hidden rounded-[24px] border border-[#ececec] bg-white text-[#0d0d0d] shadow-[0_1px_2px_rgba(0,0,0,0.04),0_12px_32px_-18px_rgba(0,0,0,0.18)]",
        className,
      )}
    >
      <div className="flex items-start gap-3 p-5 pb-0">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-amber-50 text-amber-900">
          <ShieldXIcon className="size-5" aria-hidden />
        </span>
        <div className="min-w-0 pt-0.5">
          <h3 id={titleId} className="text-base leading-6 font-semibold tracking-[-0.01em]">
            {copy.title}
          </h3>
          <p className="text-sm text-[#6b6b6b]">{copy.subtitle}</p>
        </div>
      </div>

      <div className="space-y-4 p-5">
        {failedRules.length > 0 ? (
          <PolicyRuleTable rules={failedRules} />
        ) : (
          <p className="text-sm leading-5">
            La política no estaba disponible para revisar esta compra. Ante la duda, la compra se
            detiene antes de llegar al comercio.
          </p>
        )}

        <p className="flex gap-2.5 rounded-[16px] bg-[#f7f7f7] px-3.5 py-3 text-sm leading-5">
          <CircleSlashIcon className="mt-0.5 size-4 shrink-0 text-[#6b6b6b]" aria-hidden />
          {/* Las decisiones de política ocurren antes de crear el checkout (D1). */}
          No se creó ningún checkout ni se hizo ningún cobro.
        </p>

        <TechnicalDetails refs={decisionRefs(decision)} />

        <p className="text-center text-xs leading-5 text-[#6b6b6b]">
          Política de compra · {POLICY_MODE_LABEL[decision.mode]}
        </p>
      </div>
    </section>
  )
}
