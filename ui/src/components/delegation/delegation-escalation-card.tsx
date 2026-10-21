import { cn } from "cn"
import { HandIcon, MessageSquareIcon } from "lucide-react"
import { useId } from "react"

import { pillButton } from "@/components/agent/agent-styles"
import { BUY_FROM_CHAT_LABEL } from "@/components/delegation/delegation-copy"
import { CARD_SURFACE, CardHeader } from "@/components/delegation/delegation-parts"
import type { EscalationView } from "@/lib/delegation-contract"

/** El texto canónico del escalamiento, tal como llega del backend. */
export function EscalationMessage({ message }: { message: string }) {
  return (
    <p className="rounded-[16px] bg-amber-50 px-3.5 py-3 text-sm leading-5 wrap-break-word text-amber-900">
      {message}
    </p>
  )
}

/** La única salida de un escalamiento (§8.2): una compra HP nueva, prellenada en el chat. */
export function BuyFromChatButton({
  prefillRequest,
  onBuyFromChat,
}: {
  prefillRequest: string
  onBuyFromChat: (prefillRequest: string) => void
}) {
  return (
    <button
      type="button"
      onClick={() => onBuyFromChat(prefillRequest)}
      className={cn(pillButton({ tone: "ink", size: "lg" }), "w-full")}
    >
      <MessageSquareIcon aria-hidden />
      {BUY_FROM_CHAT_LABEL}
    </button>
  )
}

export type DelegationEscalationCardProps = {
  escalation: EscalationView
  /** Prellena el compositor con `prefill_request`; el comprador aprueba esa compra él mismo. */
  onBuyFromChat?: (prefillRequest: string) => void
  className?: string
}

/**
 * Aviso de que la delegación volvió al comprador: «Necesito tu decisión». No se firmó ni se cobró
 * nada, o el proveedor de pagos lo rechazó; el texto del backend dice cuál.
 */
export function DelegationEscalationCard({
  escalation,
  onBuyFromChat,
  className,
}: DelegationEscalationCardProps) {
  const titleId = useId()
  return (
    <article
      aria-labelledby={titleId}
      data-testid="delegation-escalation-card"
      data-reason={escalation.reason}
      className={cn(CARD_SURFACE, className)}
    >
      <CardHeader Icon={HandIcon} tone="warning" title={escalation.title} titleId={titleId} />
      <div className="space-y-4 p-5">
        <EscalationMessage message={escalation.message} />
        {onBuyFromChat && (
          <BuyFromChatButton
            prefillRequest={escalation.prefill_request}
            onBuyFromChat={onBuyFromChat}
          />
        )}
      </div>
    </article>
  )
}
