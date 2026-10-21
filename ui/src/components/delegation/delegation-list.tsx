import { cn } from "cn"
import { LoaderCircleIcon, ShieldOffIcon } from "lucide-react"
import { useId } from "react"

import { pillButton } from "@/components/agent/agent-styles"
import { DELEGATIONS_COPY } from "@/components/delegation/delegation-copy"
import { ErrorNotice } from "@/components/delegation/delegation-parts"
import { DelegationStatusCard } from "@/components/delegation/delegation-status-card"
import type { DelegationView, RevokeReceipt } from "@/lib/delegation-contract"

/** Lo que pasa con la revocación de una delegación, desde su tarjeta o desde «Revocar todo». */
export type DelegationRevocation = {
  revoking: boolean
  error: string | null
  receipt: RevokeReceipt | null
}

export type DelegationListProps = {
  /** `GET /me/delegations`, más recientes primero. */
  delegations: DelegationView[]
  revocations?: Record<string, DelegationRevocation>
  loading?: boolean
  /** Mensaje saneado si no se pudo leer la lista. */
  error?: string | null
  revokingAll?: boolean
  revokeAllError?: string | null
  /** La delegación recién autorizada: su tarjeta toma el foco de la propuesta. */
  focusId?: string | null
  onRevoke?: (grantId: string) => void
  onRevokeAll?: () => void
  onBuyFromChat?: (prefillRequest: string) => void
  className?: string
}

/**
 * «Mis delegaciones» (§2.1): cada delegación con su estado, alcance, vigencia y última actividad,
 * «Revocar» en las activas y «Revocar todo» mientras quede alguna.
 */
export function DelegationList({
  delegations,
  revocations = {},
  loading = false,
  error,
  revokingAll = false,
  revokeAllError,
  focusId,
  onRevoke,
  onRevokeAll,
  onBuyFromChat,
  className,
}: DelegationListProps) {
  const titleId = useId()
  const revocable = delegations.some(
    (item) => item.state === "active" && !revocations[item.grant_id]?.receipt,
  )

  return (
    <section
      aria-labelledby={titleId}
      aria-busy={loading || revokingAll || undefined}
      data-testid="delegation-list"
      className={cn("space-y-3", className)}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={titleId} className="text-base leading-6 font-semibold">
          {DELEGATIONS_COPY.mine}
        </h2>
        {onRevokeAll && (revocable || revokingAll) && (
          <button
            type="button"
            onClick={revokingAll ? undefined : onRevokeAll}
            aria-disabled={revokingAll || undefined}
            className={pillButton({ tone: "outline", size: "md" })}
          >
            {revokingAll ? (
              <LoaderCircleIcon className="motion-safe:animate-spin" aria-hidden />
            ) : (
              <ShieldOffIcon aria-hidden />
            )}
            {DELEGATIONS_COPY.revokeAll}
          </button>
        )}
      </div>

      {revokeAllError && <ErrorNotice>{revokeAllError}</ErrorNotice>}

      {error ? (
        <ErrorNotice>{error}</ErrorNotice>
      ) : delegations.length === 0 ? (
        <p className="text-sm leading-5 text-[#6b6b6b]">
          {loading ? DELEGATIONS_COPY.loading : DELEGATIONS_COPY.none}
        </p>
      ) : (
        <ul className="space-y-3">
          {delegations.map((delegation) => {
            const revocation = revocations[delegation.grant_id]
            return (
              <li key={delegation.grant_id}>
                <DelegationStatusCard
                  delegation={delegation}
                  revokeReceipt={revocation?.receipt}
                  revoking={revocation?.revoking}
                  error={revocation?.error}
                  onRevoke={onRevoke && (() => onRevoke(delegation.grant_id))}
                  onBuyFromChat={onBuyFromChat}
                  autoFocus={delegation.grant_id === focusId}
                />
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
