import { cn } from "cn"
import { LoaderCircleIcon, ShieldCheckIcon, XIcon } from "lucide-react"
import { useId } from "react"

import { formatDateTime, formatMoneyParts } from "@/components/agent/agent-format"
import { chip, pillButton, totalAmount } from "@/components/agent/agent-styles"
import { useCardFocus } from "@/components/agent/use-card-focus"
import {
  authorizeBy,
  CONSENT_COPY,
  expiresOn,
  SCOPE_LABEL,
} from "@/components/delegation/delegation-copy"
import {
  CARD_SURFACE,
  CardHeader,
  ErrorNotice,
  PreferenceQuote,
  ScopeRow,
  ScopeRows,
  VariantList,
} from "@/components/delegation/delegation-parts"
import {
  proposalDeadline,
  type DelegationConsent,
  type DelegationProposal,
} from "@/lib/delegation-contract"

export type { DelegationConsent }

export type DelegationConsentCardProps = {
  /** Respuesta de `POST /delegations/proposals`; todo lo que la tarjeta muestra sale de aquí. */
  proposal: DelegationProposal
  /** Mientras `POST /delegations` responde, los botones conservan el foco con `aria-disabled`. */
  authorizing?: boolean
  /** Mensaje saneado de la API si autorizar falló (propuesta vencida, alcance distinto…). */
  error?: string | null
  /** Sin él, «Autorizar» queda deshabilitado (una propuesta vencida no se reintenta). */
  onAuthorize?: (consent: DelegationConsent) => void
  onCancel?: () => void
  /** Ocupa el lugar del formulario que se envió: el foco pasa a su título. */
  autoFocus?: boolean
  className?: string
}

/**
 * Consentimiento previo de una compra sin el comprador (Trusted Surface, §3.2): alcance, tope,
 * vigencia y comercio exactos de la propuesta, sin texto del modelo. «Autorizar» devuelve el
 * `scope_digest` de lo que se mostró, para que el servidor rechace cualquier otro alcance. Es un
 * `article` y no una región: dos propuestas en la misma página repetirían el nombre de la región.
 */
export function DelegationConsentCard({
  proposal,
  authorizing = false,
  error,
  onAuthorize,
  onCancel,
  autoFocus = false,
  className,
}: DelegationConsentCardProps) {
  const titleId = useId()
  const { cardRef, titleRef } = useCardFocus<HTMLElement>("consent", false, autoFocus)
  const { scope } = proposal
  const total = formatMoneyParts(scope.total_max.minor, scope.total_max.currency)
  const expires = formatDateTime(scope.expires_at) ?? scope.expires_at
  const deadline = proposalDeadline(proposal)
  const consent = { proposal_id: proposal.proposal_id, scope_digest: proposal.scope_digest }
  const authorizeBlocked = authorizing || !onAuthorize

  return (
    <article
      ref={cardRef}
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
        titleRef={titleRef}
        subtitle={scope.merchant_name}
      />

      <div className="space-y-5 p-5">
        <div>
          <p className="text-xs font-medium text-[#6b6b6b]">{CONSENT_COPY.totalIncludesShipping}</p>
          <p className="mt-0.5 flex flex-wrap items-baseline gap-x-1.5">
            <span className={totalAmount}>{total.amount}</span>
            <span className="text-sm font-medium text-[#6b6b6b]">{total.code}</span>
          </p>
        </div>

        <VariantList variants={scope.variants} />
        {scope.preference_text && <PreferenceQuote text={scope.preference_text} />}

        <ScopeRows>
          <ScopeRow label={SCOPE_LABEL.destination}>{scope.destination_label}</ScopeRow>
          <ScopeRow label={SCOPE_LABEL.instrument}>{scope.instrument_label}</ScopeRow>
        </ScopeRows>

        <div className="space-y-2">
          <p className="flex flex-wrap gap-2">
            <span className={chip({ tone: "neutral", size: "sm" })}>{CONSENT_COPY.oneUnit}</span>
            <span className={chip({ tone: "neutral", size: "sm" })}>{CONSENT_COPY.singleUse}</span>
            <span className={chip({ tone: "outline", size: "sm" })}>
              <time dateTime={scope.expires_at}>{expiresOn(expires)}</time>
            </span>
          </p>
          {/* La propuesta vence con su TTL o con la delegación, lo que llegue primero (§5.1). */}
          <p className="text-xs leading-5 text-[#6b6b6b]">
            <time dateTime={deadline}>{authorizeBy(formatDateTime(deadline) ?? deadline)}</time>
          </p>
        </div>

        {error && <ErrorNotice>{error}</ErrorNotice>}

        {/*
          `aria-disabled` y no `disabled`: el botón pulsado conserva el foco. Bajo 20rem (un
          teléfono de 320 px) los botones se apilan: lado a lado se salen del padding.
        */}
        <div className="grid grid-cols-1 gap-2 @xs:grid-cols-[auto_1fr]">
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
            onClick={authorizeBlocked ? undefined : () => onAuthorize(consent)}
            aria-disabled={authorizeBlocked || undefined}
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
    </article>
  )
}
