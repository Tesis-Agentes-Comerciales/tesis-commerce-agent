import { cn } from "cn"
import { ShieldCheckIcon } from "lucide-react"

import { pillButton } from "@/components/agent/agent-styles"
import {
  DelegationConsentCard,
  type DelegationConsent,
} from "@/components/delegation/delegation-consent-card"
import { IN_CHAT_COPY } from "@/components/delegation/delegation-copy"
import { DelegationForm } from "@/components/delegation/delegation-form"
import { DelegationStatusCard } from "@/components/delegation/delegation-status-card"
import type { ProductCard } from "@/lib/agent-contract"
import type { DelegationProposal, DelegationView } from "@/lib/delegation-contract"
import type { DelegationDraft } from "@/lib/delegation-draft"
import type { PurchaseTrace } from "@/lib/trace-contract"

/** En qué va la delegación del hilo; la misma forma que el flujo de `delegationsReducer`. */
export type DelegationInChatFlow =
  | { step: "idle" }
  | { step: "form"; submitting: boolean; error: string | null }
  | {
      step: "consent"
      proposal: DelegationProposal
      authorizing: boolean
      error: string | null
      canAuthorize: boolean
    }

export type DelegationInChatProps = {
  /** La lista corta del turno `delegate` (`delegation_draft.offers`). */
  offers: ProductCard[]
  /** El formulario prellenado con el borrador o lo último que se envió. */
  draft: DelegationDraft
  flow: DelegationInChatFlow
  /** La delegación que se acaba de autorizar aquí: su estado toma el lugar de la propuesta. */
  delegation?: DelegationView | null
  /** Traza HNP de la compra de su tarea: la gobernanza en vivo. */
  trace?: PurchaseTrace | null
  onPropose?: (draft: DelegationDraft) => void
  onCloseForm?: () => void
  onOpenForm?: () => void
  onAuthorize?: (consent: DelegationConsent) => void
  onBackToForm?: () => void
  /** CTA de un escalamiento: la compra vuelve al chat como compra HP. */
  onBuyFromChat?: (prefillRequest: string) => void
  className?: string
}

/**
 * La compra delegada en el hilo (docs/design/hnp-chat-delegate.md): debajo de la respuesta del
 * turno «Delegar» va una sola de las tarjetas de delegación, según el paso. El formulario, el
 * consentimiento y el estado son los mismos de «Mis delegaciones»: aquí solo se elige cuál.
 */
export function DelegationInChat({
  offers,
  draft,
  flow,
  delegation,
  trace,
  onPropose,
  onCloseForm,
  onOpenForm,
  onAuthorize,
  onBackToForm,
  onBuyFromChat,
  className,
}: DelegationInChatProps) {
  return (
    <section
      aria-label={IN_CHAT_COPY.label}
      data-testid="delegation-in-chat"
      className={cn("px-2", className)}
    >
      {delegation ? (
        <DelegationStatusCard
          delegation={delegation}
          trace={trace}
          onBuyFromChat={onBuyFromChat}
          autoFocus
        />
      ) : flow.step === "form" ? (
        <DelegationForm
          offers={offers}
          draft={draft}
          submitting={flow.submitting}
          error={flow.error}
          onSubmit={onPropose}
          onCancel={onCloseForm}
        />
      ) : flow.step === "consent" ? (
        <DelegationConsentCard
          proposal={flow.proposal}
          authorizing={flow.authorizing}
          error={flow.error}
          onAuthorize={flow.canAuthorize ? onAuthorize : undefined}
          onCancel={onBackToForm}
          autoFocus
        />
      ) : (
        <button
          type="button"
          onClick={onOpenForm}
          className={pillButton({ tone: "outline", size: "md" })}
        >
          <ShieldCheckIcon aria-hidden />
          {IN_CHAT_COPY.reopen}
        </button>
      )}
    </section>
  )
}
