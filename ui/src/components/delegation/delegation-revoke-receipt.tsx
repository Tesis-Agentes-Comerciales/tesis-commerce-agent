import { cn } from "cn"
import { CircleSlashIcon, ClockIcon } from "lucide-react"

import { DateTime } from "@/components/delegation/delegation-parts"
import type { RevokeReceipt } from "@/lib/delegation-contract"

export type DelegationRevokeReceiptProps = {
  receipt: RevokeReceipt
  className?: string
}

/**
 * Recibo de `POST /delegations/{id}/revoke`: el mensaje del backend por efecto (§8.3) y la hora.
 * `in_flight` va en ámbar: el pago ya estaba comprometido y esa compra sigue.
 */
export function DelegationRevokeReceipt({ receipt, className }: DelegationRevokeReceiptProps) {
  const inFlight = receipt.effect === "in_flight"
  const Icon = inFlight ? ClockIcon : CircleSlashIcon
  return (
    <div
      data-testid="delegation-revoke-receipt"
      data-effect={receipt.effect}
      className={cn(
        "flex gap-2.5 rounded-[16px] px-3.5 py-3 text-sm leading-5",
        inFlight ? "bg-amber-50 text-amber-900" : "bg-[#f7f7f7] text-[#0d0d0d]",
        className,
      )}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0 space-y-0.5">
        <p className="wrap-break-word">{receipt.message}</p>
        <p className="text-xs">
          <DateTime value={receipt.revoked_at} />
        </p>
      </div>
    </div>
  )
}
