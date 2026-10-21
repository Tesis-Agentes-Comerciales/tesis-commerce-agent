import { cn } from "cn"
import { CheckIcon, MinusIcon, XIcon } from "lucide-react"

import { formatMoney } from "@/components/agent/agent-format"
import { chip } from "@/components/agent/agent-styles"
import { type MerchantLabel, RULE_RESULT_LABEL, ruleCopy } from "@/components/trace/trace-copy"
import type { Money, PolicyRule } from "@/lib/trace-contract"

const RESULT_VIEW = {
  pass: { tone: "neutral", Icon: CheckIcon },
  fail: { tone: "warning", Icon: XIcon },
  not_evaluated: { tone: "soft", Icon: MinusIcon },
} as const

export type PolicyRuleTableProps = {
  rules: PolicyRule[]
  /** En el chat basta con ver por qué falló una regla; la auditoría muestra todos los valores. */
  values?: "all" | "failed"
  /** Comercio de la compra: si es el que observó la regla de comercios, se nombra. */
  merchant?: MerchantLabel
  className?: string
}

/** Reglas de una decisión con su resultado y, si la traza los trae, el valor observado y el esperado. */
export function PolicyRuleTable({
  rules,
  values = "all",
  merchant,
  className,
}: PolicyRuleTableProps) {
  return (
    <ul
      aria-label="Reglas de la política de compra"
      className={cn("divide-y divide-[#ececec] rounded-[16px] border border-[#ececec]", className)}
    >
      {rules.map((rule) => (
        <RuleRow
          key={rule.id}
          rule={rule}
          showValues={values === "all" || rule.result === "fail"}
          merchant={merchant}
        />
      ))}
    </ul>
  )
}

type RuleRowProps = { rule: PolicyRule; showValues: boolean; merchant?: MerchantLabel }

function RuleRow({ rule, showValues, merchant }: RuleRowProps) {
  const copy = ruleCopy(rule.id)
  const { tone, Icon } = RESULT_VIEW[rule.result]
  // La política compara orígenes: el nombre acompaña al observado, la lista esperada sigue igual.
  const observedName =
    rule.id === "POL-02" && rule.observed === merchant?.origin ? merchant.name : null

  return (
    <li className="px-3.5 py-2.5" data-result={rule.result}>
      <div className="flex items-start justify-between gap-3">
        <p className="min-w-0 text-sm leading-5 font-medium wrap-break-word">{copy.name}</p>
        <span className={cn(chip({ tone, size: "sm" }), "shrink-0")}>
          <Icon className="size-3" strokeWidth={2.5} aria-hidden />
          {RULE_RESULT_LABEL[rule.result]}
        </span>
      </div>
      {showValues && (rule.observed !== null || rule.expected !== null) && (
        // Término sobre valor: en un teléfono de 320 px, a dos columnas, la URL quedaba en un hueco
        // de pocos caracteres y se partía en muchas líneas.
        <dl className="mt-1.5 space-y-1.5 text-xs leading-4">
          {rule.observed !== null && (
            <RuleValue label={copy.observed} value={rule.observed} name={observedName} />
          )}
          {rule.expected !== null && <RuleValue label={copy.expected} value={rule.expected} />}
        </dl>
      )}
    </li>
  )
}

type RuleValueProps = { label: string; value: string | Money; name?: string | null }

/**
 * Con nombre, el valor técnico pasa a segundo plano bajo él. Un monto (el precio y el presupuesto de
 * la regla de presupuesto) se escribe como los demás montos de la traza, no como un valor técnico.
 */
function RuleValue({ label, value, name = null }: RuleValueProps) {
  if (typeof value !== "string") {
    return (
      <div>
        <dt className="text-[#6b6b6b]">{label}</dt>
        <dd className="mt-0.5 text-[#0d0d0d] tabular-nums">
          {formatMoney(value.minor, value.currency)}
        </dd>
      </div>
    )
  }
  return (
    <div>
      <dt className="text-[#6b6b6b]">{label}</dt>
      {name && <dd className="mt-0.5 wrap-break-word text-[#0d0d0d]">{name}</dd>}
      <dd className={cn("mt-0.5 font-mono break-all", name ? "text-[#6b6b6b]" : "text-[#0d0d0d]")}>
        {value}
      </dd>
    </div>
  )
}
