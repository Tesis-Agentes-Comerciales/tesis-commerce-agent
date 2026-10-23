import { cn } from "cn"
import {
  BanIcon,
  CheckIcon,
  CircleAlertIcon,
  CircleHelpIcon,
  ClockIcon,
  HandIcon,
  LoaderCircleIcon,
  ShieldCheckIcon,
  ShieldOffIcon,
  type LucideIcon,
} from "lucide-react"
import { useId } from "react"

import { formatMoney } from "@/components/agent/agent-format"
import { pillButton } from "@/components/agent/agent-styles"
import { PurchaseActivity } from "@/components/agent/purchase-activity"
import { useCardFocus } from "@/components/agent/use-card-focus"
import {
  delegationStatus,
  delegationTitle,
  GOVERNANCE_LABEL,
  REVOKE_LABEL,
  SCOPE_LABEL,
  type DelegationStatus,
} from "@/components/delegation/delegation-copy"
import {
  BuyFromChatButton,
  EscalationMessage,
} from "@/components/delegation/delegation-escalation-card"
import {
  CARD_SURFACE,
  CardHeader,
  DateTime,
  ErrorNotice,
  PreferenceQuote,
  ScopeRow,
  ScopeRows,
  VariantList,
  type HeaderTone,
} from "@/components/delegation/delegation-parts"
import { DelegationRevokeReceipt } from "@/components/delegation/delegation-revoke-receipt"
import type { DelegationView, RevokeReceipt } from "@/lib/delegation-contract"
import type { PurchaseTrace } from "@/lib/trace-contract"

const STATUS_VIEW: Record<DelegationStatus, { Icon: LucideIcon; tone: HeaderTone }> = {
  scheduled: { Icon: ShieldCheckIcon, tone: "ink" },
  running: { Icon: LoaderCircleIcon, tone: "ink" },
  purchased: { Icon: CheckIcon, tone: "success" },
  escalated: { Icon: HandIcon, tone: "warning" },
  revoked: { Icon: ShieldOffIcon, tone: "muted" },
  kill_switch: { Icon: BanIcon, tone: "muted" },
  expired: { Icon: ClockIcon, tone: "muted" },
  failed: { Icon: CircleAlertIcon, tone: "danger" },
  uncertain: { Icon: CircleHelpIcon, tone: "warning" },
}

export type DelegationStatusCardProps = {
  delegation: DelegationView
  /**
   * Traza de la compra de la tarea (`GET /purchases/{task.run_id}/trace`, `purchase_mode: "HNP"`):
   * los mismos controles que la compra en el chat. Sin ella, la tarjeta no muestra la gobernanza.
   */
  trace?: PurchaseTrace | null
  /**
   * La gobernanza sale abierta y sus filas nuevas entran a ritmo: la tarjeta del hilo, donde se ve
   * avanzar la compra delegada. En «Mis delegaciones» queda plegada.
   */
  governanceOpen?: boolean
  /** Recibo de `POST /delegations/{id}/revoke`, cuando se acaba de revocar desde esta tarjeta. */
  revokeReceipt?: RevokeReceipt | null
  /** Mientras revocar responde, «Revocar» conserva el foco con `aria-disabled`. */
  revoking?: boolean
  /** Mensaje saneado de la API si revocar falló; el botón sigue para reintentar. */
  error?: string | null
  /** Solo se ofrece con el grant `active` y sin recibo; uno cerrado no se revoca (§5.1). */
  onRevoke?: () => void
  /** CTA del escalamiento: prellena el compositor con `prefill_request`. */
  onBuyFromChat?: (prefillRequest: string) => void
  /** Ocupa el lugar de la tarjeta de consentimiento que tenía el foco: el foco pasa a su título. */
  autoFocus?: boolean
  className?: string
}

/**
 * Una delegación y su tarea: qué pasó, con el mensaje del backend, la gobernanza de su compra y el
 * alcance que se consintió. Un escalamiento muestra su texto canónico y «Comprar desde el chat»;
 * una delegación activa, «Revocar» y, al revocar, su recibo.
 */
export function DelegationStatusCard({
  delegation,
  trace,
  governanceOpen = false,
  revokeReceipt,
  revoking = false,
  error,
  onRevoke,
  onBuyFromChat,
  autoFocus = false,
  className,
}: DelegationStatusCardProps) {
  const titleId = useId()
  const status = delegationStatus(delegation)
  const title = delegationTitle(status, delegation)
  const { scope, task, escalation } = delegation
  const { Icon, tone } = STATUS_VIEW[status]
  // Con el recibo ya no: la vista releída puede tardar, pero esta delegación ya se revocó.
  const canRevoke = delegation.state === "active" && !!onRevoke && !revokeReceipt
  // Revocada o terminada, «Revocar» ya no está: si se pulsó con el teclado, el foco pasa al título.
  const { cardRef, titleRef } = useCardFocus<HTMLElement>(
    `${status}:${canRevoke}`,
    !canRevoke,
    autoFocus,
  )

  return (
    <>
      {/*
        Fuera de la tarjeta, que lleva `aria-busy` mientras revoca (ver PurchaseApprovalCard). El
        título que recibe el foco dice el estado; el recibo dice qué pasó con el pago. La tarjeta es
        un `article` y no una región: «Mis delegaciones» repite títulos («Delegación activa»).
      */}
      <p role="status" className="sr-only">
        {revokeReceipt?.message ?? ""}
      </p>
      <article
        ref={cardRef}
        aria-labelledby={titleId}
        aria-busy={revoking || undefined}
        data-testid="delegation-status-card"
        data-status={status}
        className={cn(CARD_SURFACE, className)}
      >
        <CardHeader
          Icon={Icon}
          tone={tone}
          spin={status === "running"}
          title={title}
          titleId={titleId}
          titleRef={titleRef}
          subtitle={scope.merchant_name}
        />

        <div className="space-y-4 p-5">
          {status === "escalated" && escalation ? (
            <EscalationMessage message={escalation.message} />
          ) : (
            task.message && (
              <p className="rounded-[16px] bg-[#f7f7f7] px-3.5 py-3 text-sm leading-5 wrap-break-word">
                {task.message}
              </p>
            )
          )}

          {trace && <DelegationGovernance trace={trace} open={governanceOpen} />}

          <VariantList variants={scope.variants} />
          {scope.preference_text && <PreferenceQuote text={scope.preference_text} />}

          <ScopeRows>
            <ScopeRow label={SCOPE_LABEL.total}>
              {formatMoney(scope.total_max.minor, scope.total_max.currency)}
            </ScopeRow>
            <ScopeRow label={SCOPE_LABEL.expires}>
              <DateTime value={delegation.expires_at} />
            </ScopeRow>
            <ScopeRow label={SCOPE_LABEL.destination}>{scope.destination_label}</ScopeRow>
            <ScopeRow label={SCOPE_LABEL.instrument}>{scope.instrument_label}</ScopeRow>
            <ScopeRow label={SCOPE_LABEL.lastActivity}>
              <DateTime value={delegation.last_activity_at} />
            </ScopeRow>
          </ScopeRows>

          {error && <ErrorNotice>{error}</ErrorNotice>}

          {status === "escalated" && escalation && onBuyFromChat && (
            <BuyFromChatButton
              prefillRequest={escalation.prefill_request}
              onBuyFromChat={onBuyFromChat}
            />
          )}

          {/* `aria-disabled` y no `disabled`: el botón pulsado conserva el foco. */}
          {canRevoke && (
            <button
              type="button"
              onClick={revoking ? undefined : onRevoke}
              aria-disabled={revoking || undefined}
              className={cn(pillButton({ tone: "outline", size: "lg" }), "w-full")}
            >
              {revoking ? (
                <LoaderCircleIcon className="motion-safe:animate-spin" aria-hidden />
              ) : (
                <ShieldOffIcon aria-hidden />
              )}
              {REVOKE_LABEL}
            </button>
          )}

          {revokeReceipt && <DelegationRevokeReceipt receipt={revokeReceipt} />}
        </div>
      </article>
    </>
  )
}

/**
 * La gobernanza de la compra delegada: la fila de actividad del chat (plegada, salvo con `open`) y
 * al abrirla la línea de tiempo con el consentimiento, las tres decisiones de la política, el uso
 * único y la orden, con su detalle técnico (docs/design/trace-panel.md, «Compra delegada»).
 */
function DelegationGovernance({ trace, open }: { trace: PurchaseTrace; open: boolean }) {
  const labelId = useId()
  // Un grupo y no una región: cada tarjeta de «Mis delegaciones» tiene el suyo, con el mismo nombre.
  return (
    <div
      role="group"
      aria-labelledby={labelId}
      data-testid="delegation-governance"
      className="rounded-[16px] border border-[#ececec] px-3.5 pt-2.5 pb-1.5"
    >
      <h4 id={labelId} className="text-xs leading-5 font-medium text-[#6b6b6b]">
        {GOVERNANCE_LABEL}
      </h4>
      <PurchaseActivity trace={trace} showNames={false} defaultOpen={open} paced={open} />
    </div>
  )
}
