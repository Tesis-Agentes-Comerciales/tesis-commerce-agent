import { cn } from "cn"
import { LoaderCircleIcon, ShieldCheckIcon, XIcon } from "lucide-react"
import { useId } from "react"

import { formatDateTime, formatMoneyParts } from "@/components/agent/agent-format"
import { chip, pillButton } from "@/components/agent/agent-styles"
import { CONSENT_COPY, expiresOn, SCOPE_LABEL } from "@/components/delegation/delegation-copy"
import {
  CARD_SURFACE,
  CardHeader,
  ErrorNotice,
  PreferenceQuote,
  ScopeRow,
  ScopeRows,
  VariantList,
} from "@/components/delegation/delegation-parts"
import type { DelegationProposal } from "@/lib/delegation-contract"

/** Lo que `POST /delegations` necesita: la propuesta y el digest del alcance que se mostró. */
export type DelegationConsent = Pick<DelegationProposal, "proposal_id" | "scope_digest">

export type DelegationConsentCardProps = {
  /** Respuesta de `POST /delegations/proposals`; todo lo que la tarjeta muestra sale de aquí. */
  proposal: DelegationProposal
  /** Mientras `POST /delegations` responde, los botones conservan el foco con `aria-disabled`. */
  authorizing?: boolean
  /** Mensaje saneado de la API si autorizar falló (propuesta vencida, alcance distinto…). */
  error?: string | null
  onAuthorize?: (consent: DelegationConsent) => void
  onCancel?: () => void
  className?: string
}

/**
 * Consentimiento previo de una compra sin el comprador (Trusted Surface, §3.2): alcance, tope,
 * vigencia y comercio exactos de la propuesta, sin texto del modelo. «Autorizar» devuelve el
 * `scope_digest` de lo que se mostró, para que el servidor rechace cualquier otro alcance.
 */
export function DelegationConsentCard({
  proposal,
  authorizing = false,
  error,
  onAuthorize,
  onCancel,
  className,
}: DelegationConsentCardProps) {
  const titleId = useId()
  const { scope } = proposal
  const total = formatMoneyParts(scope.total_max.minor, scope.total_max.currency)
  const expires = formatDateTime(scope.expires_at) ?? scope.expires_at
  const consent = { proposal_id: proposal.proposal_id, scope_digest: proposal.scope_digest }

  return (
    <section
      aria-labelledby={titleId}
      aria-busy={authorizing || undefined}
      data-testid="delegation-consent-card"
      className={cn(CARD_SURFACE, className)}
    >
      <CardHeader
        Icon={ShieldCheckIcon}
        tone="ink"
        title={CONSENT_COPY.title}
        titleId={titleId}
        subtitle={scope.merchant_name}
      />

      <div className="space-y-5 p-5">
        <div>
          <p className="text-xs font-medium text-[#6b6b6b]">{CONSENT_COPY.totalIncludesShipping}</p>
          <p className="mt-0.5 flex flex-wrap items-baseline gap-x-1.5">
            <span className="text-[2rem] leading-10 font-semibold tracking-[-0.04em] tabular-nums">
              {total.amount}
            </span>
            <span className="text-sm font-medium text-[#6b6b6b]">{total.code}</span>
          </p>
        </div>

        <VariantList variants={scope.variants} />
        {scope.preference_text && <PreferenceQuote text={scope.preference_text} />}

        <ScopeRows>
          <ScopeRow label={SCOPE_LABEL.destination}>{scope.destination_label}</ScopeRow>
          <ScopeRow label={SCOPE_LABEL.instrument}>{scope.instrument_label}</ScopeRow>
        </ScopeRows>

        <p className="flex flex-wrap gap-2">
          <span className={chip({ tone: "neutral", size: "sm" })}>{CONSENT_COPY.singleUse}</span>
          <span className={chip({ tone: "outline", size: "sm" })}>
            <time dateTime={scope.expires_at}>{expiresOn(expires)}</time>
          </span>
        </p>

        {error && <ErrorNotice>{error}</ErrorNotice>}

        {/* `aria-disabled` y no `disabled`: el botón pulsado conserva el foco. */}
        <div className="grid grid-cols-[auto_1fr] gap-2">
          <button
            type="button"
            onClick={authorizing ? undefined : onCancel}
            aria-disabled={authorizing || undefined}
            className={pillButton({ tone: "soft", size: "lg" })}
          >
            <XIcon aria-hidden />
            {CONSENT_COPY.cancel}
          </button>
          <button
            type="button"
            onClick={authorizing ? undefined : () => onAuthorize?.(consent)}
            aria-disabled={authorizing || undefined}
            className={cn(pillButton({ tone: "ink", size: "lg" }), "w-full")}
          >
            {authorizing ? (
              <LoaderCircleIcon className="motion-safe:animate-spin" aria-hidden />
            ) : (
              <ShieldCheckIcon aria-hidden />
            )}
            {CONSENT_COPY.authorize}
          </button>
        </div>

        <p className="text-center text-xs leading-5 text-[#6b6b6b]">{CONSENT_COPY.revocable}</p>
      </div>
    </section>
  )
}
