import { useAui, useAuiState } from "@assistant-ui/react"
import { cn } from "cn"
import { PlusIcon } from "lucide-react"
import { useCallback, useContext, useEffect, useMemo, useReducer, useRef } from "react"

import { pillButton } from "@/components/agent/agent-styles"
import { DELEGATIONS_COPY } from "@/components/delegation/delegation-copy"
import { DelegationConsentCard } from "@/components/delegation/delegation-consent-card"
import { DelegationForm } from "@/components/delegation/delegation-form"
import { DelegationList } from "@/components/delegation/delegation-list"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { DelegationsHostContext, type DelegationsHost } from "@/features/delegations-host"
import {
  activeGrantIds,
  delegationsReducer,
  INITIAL_DELEGATIONS,
} from "@/features/delegations-state"
import { seenOffers } from "@/features/seen-offers"
import type { BuyerProfile } from "@/lib/commerce-types"
import {
  consentToDelegation,
  delegationErrorCode,
  delegationErrorText,
  getDelegation,
  myDelegations,
  needsNewProposal,
  proposeDelegation,
  revokeAllDelegations,
  revokeDelegation,
} from "@/lib/delegation-api"
import type { DelegationConsent } from "@/lib/delegation-contract"
import { proposalRequest, type DelegationDraft } from "@/lib/delegation-draft"

/**
 * «Delegaciones» dentro del runtime del chat: lee las opciones que el agente ya mostró y, ante un
 * escalamiento, escribe la compra HP en el compositor. Sin anfitrión (HNP apagado) no monta nada.
 */
export function DelegationsMount() {
  const host = useContext(DelegationsHostContext)
  return host ? <Delegations {...host} /> : null
}

const blankDraft = ({ addressLine, city, state }: BuyerProfile): DelegationDraft => ({
  variantIds: [],
  total: "",
  validForSeconds: 3_600,
  addressLine,
  city,
  state,
  preference: "",
})

/**
 * El formulario, la propuesta con su consentimiento y «Mis delegaciones» (§3.2, §8.4), contra las
 * rutas del comprador (§5.1). Cada acción relee lo que cambió: el estado lo dice el backend.
 */
function Delegations({ open, onOpenChange, profile }: DelegationsHost) {
  const [state, dispatch] = useReducer(delegationsReducer, INITIAL_DELEGATIONS)
  // Los mensajes del thread y no el estado del transporte: mientras el thread carga no hay estado,
  // y este diálogo se monta junto al chat, no dentro de un mensaje.
  const messages = useAuiState((aui) => aui.thread.messages)
  const offers = useMemo(() => seenOffers(messages), [messages])
  const aui = useAui()
  const prefilled = useRef(false)
  const { flow } = state

  const reload = useCallback(async (signal?: AbortSignal) => {
    dispatch({ type: "list-loading" })
    try {
      dispatch({ type: "list-loaded", delegations: (await myDelegations(signal)).delegations })
    } catch (error) {
      if (!signal?.aborted) dispatch({ type: "list-failed", error: delegationErrorText(error) })
    }
  }, [])

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    void reload(controller.signal)
    return () => controller.abort()
  }, [open, reload])

  const propose = async (draft: DelegationDraft) => {
    const checked = proposalRequest(draft, profile)
    if (!checked.ok) return dispatch({ type: "propose-failed", error: checked.problem })
    dispatch({ type: "proposing", draft })
    try {
      dispatch({ type: "proposed", proposal: await proposeDelegation(checked.request) })
    } catch (error) {
      dispatch({ type: "propose-failed", error: delegationErrorText(error) })
    }
  }

  const authorize = async (consent: DelegationConsent) => {
    dispatch({ type: "authorizing" })
    try {
      dispatch({ type: "authorized", delegation: await consentToDelegation(consent) })
    } catch (error) {
      const canAuthorize = !needsNewProposal(error)
      return dispatch({ type: "authorize-failed", error: delegationErrorText(error), canAuthorize })
    }
    await reload()
  }

  const revoke = async (grantId: string) => {
    dispatch({ type: "revoking", grantIds: [grantId] })
    try {
      dispatch({ type: "revoked", receipts: [await revokeDelegation(grantId)] })
    } catch (error) {
      dispatch({ type: "revoke-failed", grantId, error: delegationErrorText(error) })
      // Ya terminó (409): la vista releída dice cómo. Cualquier otro error se reintenta.
      if (delegationErrorCode(error) !== "delegation_not_revocable") return
    }
    try {
      dispatch({ type: "delegation-read", delegation: await getDelegation(grantId) })
    } catch {
      // La tarjeta conserva la última vista y su recibo o su error.
    }
  }

  const revokeAll = async () => {
    dispatch({ type: "revoking", grantIds: activeGrantIds(state), all: true })
    try {
      dispatch({ type: "revoked", receipts: (await revokeAllDelegations()).receipts, all: true })
    } catch (error) {
      return dispatch({ type: "revoke-all-failed", error: delegationErrorText(error) })
    }
    await reload()
  }

  /** §8.2: la decisión vuelve al comprador como una compra HP nueva, prellenada en el chat. */
  const buyFromChat = (prefillRequest: string) => {
    aui.composer().setText(prefillRequest)
    prefilled.current = true
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="delegations"
        onCloseAutoFocus={(event) => {
          if (!prefilled.current) return
          prefilled.current = false
          event.preventDefault()
          document.querySelector<HTMLElement>("[aria-label='Mensaje de compra']")?.focus()
        }}
        className="max-h-[calc(100svh-2rem)] gap-0 overflow-y-auto rounded-[24px] border border-[#ececec] bg-white p-0 text-[#0d0d0d] shadow-[0_1px_2px_rgba(0,0,0,0.04),0_12px_32px_-18px_rgba(0,0,0,0.18)] ring-0 sm:max-w-[32rem]"
      >
        {/* `pr-14` deja libre la esquina del diálogo, donde va el botón de cerrar. */}
        <div className="space-y-1 px-5 pt-5 pr-14">
          <DialogTitle className="text-base leading-6 font-semibold">
            {DELEGATIONS_COPY.title}
          </DialogTitle>
          <DialogDescription className="text-sm leading-5 text-[#6b6b6b]">
            {DELEGATIONS_COPY.description}
          </DialogDescription>
        </div>

        <div className="space-y-6 p-4">
          {flow.step === "idle" && (
            <button
              type="button"
              onClick={() => dispatch({ type: "form-opened" })}
              className={cn(pillButton({ tone: "ink", size: "lg" }), "w-full")}
            >
              <PlusIcon aria-hidden />
              {DELEGATIONS_COPY.newDelegation}
            </button>
          )}
          {flow.step === "form" && (
            <DelegationForm
              offers={offers}
              draft={state.draft ?? blankDraft(profile)}
              submitting={flow.submitting}
              error={flow.error}
              onSubmit={propose}
              onCancel={() => dispatch({ type: "form-closed" })}
              autoFocus
            />
          )}
          {flow.step === "consent" && (
            <DelegationConsentCard
              proposal={flow.proposal}
              authorizing={flow.authorizing}
              error={flow.error}
              onAuthorize={flow.canAuthorize ? authorize : undefined}
              onCancel={() => dispatch({ type: "form-opened" })}
              autoFocus
            />
          )}

          <DelegationList
            delegations={state.delegations}
            revocations={state.revocations}
            loading={state.list === "loading"}
            error={state.list === "failed" ? state.listError : null}
            revokingAll={state.revokingAll}
            revokeAllError={state.revokeAllError}
            focusId={state.consentedId}
            onRevoke={revoke}
            onRevokeAll={revokeAll}
            onBuyFromChat={buyFromChat}
          />
        </div>
      </DialogContent>
    </Dialog>
  )
}
