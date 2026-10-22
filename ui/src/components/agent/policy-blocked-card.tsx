import { cn } from "cn"
import { CircleSlashIcon, ShieldXIcon } from "lucide-react"
import { useId } from "react"

import { formatMoney } from "@/components/agent/agent-format"
import { ProductImage } from "@/components/agent/product-card"
import { PolicyRuleTable } from "@/components/trace/policy-rule-table"
import { TechnicalDetails } from "@/components/trace/technical-details"
import {
  blockedCardCopy,
  decisionRefs,
  type MerchantLabel,
  NOTHING_CHARGED,
  type PolicyBlockReason,
  policyModeNote,
  streamBlockCopy,
} from "@/components/trace/trace-copy"
import type { ProductCard } from "@/lib/agent-contract"
import type { PolicyDecision } from "@/lib/trace-contract"

import "./agent.css"

export type { PolicyBlockReason }

export type PolicyBlockedCardProps = {
  /** La decisión que detuvo la compra (`effect: "blocked"`); `null` mientras no hay traza. */
  decision: PolicyDecision | null
  /**
   * Código del stream (`d1_denied`…): con él la tarjeta sale desde el primer cuadro, antes de que
   * llegue la traza, con el mismo título que tendrá después.
   */
  reason?: PolicyBlockReason
  /** Sin decisión: `true` mientras la traza viene en camino, y lo que solo ella sabe va en esqueleto. */
  loading?: boolean
  /** El producto que se intentó comprar: en una compra bloqueada no hay otra tarjeta del producto. */
  product?: ProductCard | null
  /** Comercio de la compra, para nombrarlo en la regla de comercios. */
  merchant?: MerchantLabel
  className?: string
}

/** Producto de la compra detenida, con su nombre y el precio que vio la política. */
function BlockedProduct({ product }: { product: ProductCard }) {
  return (
    <div className="flex items-center gap-3" data-testid="policy-blocked-product">
      <ProductImage
        image={product.image}
        fallbackAlt={product.title}
        seed={product.product_id}
        caption={false}
        className="size-14 shrink-0 rounded-[12px]"
      />
      <div className="min-w-0">
        <p className="line-clamp-2 text-sm leading-5 font-medium wrap-break-word">{product.title}</p>
        <p className="text-xs leading-5 text-[#6b6b6b] tabular-nums">
          {formatMoney(product.observed_unit_amount_minor, product.currency)} · Precio de catálogo
        </p>
      </div>
    </div>
  )
}

/** Lugar de las reglas mientras llega la traza: la forma de una regla con sus dos valores. */
function RulesSkeleton({ reason }: { reason: PolicyBlockReason }) {
  if (reason !== "denied") {
    return (
      <div className="space-y-2 py-0.5" aria-hidden>
        <div className="agent-skeleton h-3.5 w-full rounded-full" />
        <div className="agent-skeleton h-3.5 w-2/3 rounded-full" />
      </div>
    )
  }
  return (
    <div className="space-y-3 rounded-[16px] border border-[#ececec] px-3.5 py-3" aria-hidden>
      <div className="flex items-center justify-between gap-3">
        <div className="agent-skeleton h-4 w-1/2 rounded-full" />
        <div className="agent-skeleton h-5 w-20 rounded-full" />
      </div>
      <div className="space-y-1.5">
        <div className="agent-skeleton h-3 w-1/4 rounded-full" />
        <div className="agent-skeleton h-3 w-1/3 rounded-full" />
      </div>
      <div className="space-y-1.5">
        <div className="agent-skeleton h-3 w-1/4 rounded-full" />
        <div className="agent-skeleton h-3 w-1/3 rounded-full" />
      </div>
    </div>
  )
}

/**
 * Tarjeta fuera del plegable: qué regla no se cumplió y qué no ocurrió. Un bloqueo es la política
 * funcionando, no un error, así que va en ámbar y nunca dice «error inesperado». Sale con el código
 * del stream; la traza solo completa las reglas, el detalle técnico y el modo.
 */
export function PolicyBlockedCard({
  decision,
  reason = "denied",
  loading = false,
  product,
  merchant,
  className,
}: PolicyBlockedCardProps) {
  const titleId = useId()
  const failedRules = decision ? decision.rules.filter((rule) => rule.result === "fail") : []
  const pending = decision === null && loading
  const stream = streamBlockCopy(reason)
  const copy = decision
    ? blockedCardCopy(decision, failedRules.length)
    : {
        title: stream.title,
        subtitle: stream.subtitle ?? (pending ? null : stream.missing),
        explanation: stream.explanation ?? (pending || reason === "denied" ? null : stream.missing),
      }

  return (
    <section
      aria-labelledby={titleId}
      aria-busy={pending || undefined}
      data-testid="policy-blocked-card"
      data-loading={pending || undefined}
      className={cn(
        "w-full max-w-[30rem] overflow-hidden rounded-[24px] border border-[#ececec] bg-white text-[#0d0d0d] shadow-[0_1px_2px_rgba(0,0,0,0.04),0_12px_32px_-18px_rgba(0,0,0,0.18)]",
        className,
      )}
    >
      <div className="flex items-start gap-3 p-5 pb-0">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-amber-50 text-amber-900">
          <ShieldXIcon className="size-5" aria-hidden />
        </span>
        <div className="min-w-0 flex-1 pt-0.5">
          <h3 id={titleId} className="text-base leading-6 font-semibold tracking-[-0.01em]">
            {copy.title}
          </h3>
          {copy.subtitle !== null ? (
            <p className="text-sm text-[#6b6b6b]">{copy.subtitle}</p>
          ) : (
            <div className="flex h-5 items-center" aria-hidden>
              <div className="agent-skeleton h-3.5 w-40 rounded-full" />
            </div>
          )}
        </div>
      </div>

      <div className="space-y-4 p-5">
        {product && <BlockedProduct product={product} />}

        {failedRules.length > 0 ? (
          <PolicyRuleTable rules={failedRules} merchant={merchant} />
        ) : copy.explanation ? (
          <p className="text-sm leading-5">{copy.explanation}</p>
        ) : (
          pending && <RulesSkeleton reason={reason} />
        )}

        <p className="flex gap-2.5 rounded-[16px] bg-[#f7f7f7] px-3.5 py-3 text-sm leading-5">
          <CircleSlashIcon className="mt-0.5 size-4 shrink-0 text-[#6b6b6b]" aria-hidden />
          {NOTHING_CHARGED}
        </p>

        {decision ? (
          <>
            <TechnicalDetails refs={decisionRefs(decision)} />
            <p className="text-center text-xs leading-5 text-[#6b6b6b]">
              {policyModeNote(decision.mode)}
            </p>
          </>
        ) : (
          pending && (
            <div className="space-y-4" aria-hidden>
              <div className="agent-skeleton h-11 rounded-[16px] sm:h-10" />
              <div className="flex h-5 items-center justify-center">
                <div className="agent-skeleton h-3 w-56 max-w-full rounded-full" />
              </div>
            </div>
          )
        )}
      </div>
    </section>
  )
}
