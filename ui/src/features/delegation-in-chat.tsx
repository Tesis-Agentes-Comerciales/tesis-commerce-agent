import { useAui, useAuiState } from "@assistant-ui/react"
import { useContext, useEffect, useMemo, useReducer, useState } from "react"

import { DelegationInChat } from "@/components/delegation/delegation-in-chat"
import { asDelegationDraft } from "@/features/agent-results"
import { DELEGATION_POLL_MS, useDelegationTraces } from "@/features/delegation-traces"
import { DelegationsHostContext, type DelegationsHost } from "@/features/delegations-host"
import {
  delegationsReducer,
  hasTaskInFlight,
  INITIAL_DELEGATIONS,
  type DelegationsState,
} from "@/features/delegations-state"
import type { DelegationDraftView } from "@/lib/agent-contract"
import {
  consentToDelegation,
  delegationErrorText,
  getDelegation,
  needsNewProposal,
  proposeDelegation,
} from "@/lib/delegation-api"
import type { DelegationConsent } from "@/lib/delegation-contract"
import { chatDraft, proposalRequest, type DelegationDraft } from "@/lib/delegation-draft"

/**
 * La compra delegada en el hilo (docs/design/hnp-chat-delegate.md). Lee `delegation_draft`, que solo
 * publica un turno «Delegar», y monta su tarjeta al pie de los mensajes. Un borrador nuevo empieza
 * otra tarjeta; sin anfitrión (HNP apagado) o sin opciones no hay nada que mostrar.
 */
export function DelegationInChatMount() {
  const host = useContext(DelegationsHostContext)
  // Del estado del transporte sin `useAssistantTransportState`, que lanza mientras el thread carga:
  // esto se monta junto a los mensajes, no dentro de uno.
  const raw = useAuiState((state) => {
    const extras = state.thread.extras as { state?: { delegation_draft?: unknown } } | undefined
    return extras?.state?.delegation_draft
  })
  const view = useMemo(() => asDelegationDraft(raw), [raw])
  if (!host || !view || view.offers.length === 0) return null
  return <DelegationTurn key={draftKey(view)} view={view} host={host} />
}

/** Identidad del borrador: otro turno con otras opciones, otro presupuesto u otro mensaje. */
const draftKey = ({ offers, budget_minor, preference_text }: DelegationDraftView) =>
  JSON.stringify([offers.map((offer) => offer.variant_id), budget_minor, preference_text])

const FORM_OPEN: DelegationsState = {
  ...INITIAL_DELEGATIONS,
  flow: { step: "form", submitting: false, error: null },
}

/**
 * Formulario → consentimiento → estado, contra las mismas rutas que «Mis delegaciones». Autorizada,
 * la delegación y la traza de su compra se releen cada 2 s mientras la tarea espera el disparo o
 * compra: la gobernanza se ve avanzar en el hilo.
 */
function DelegationTurn({ view, host }: { view: DelegationDraftView; host: DelegationsHost }) {
  const [state, dispatch] = useReducer(delegationsReducer, FORM_OPEN)
  // Los datos de entrega del momento en que llegó el borrador; el formulario los deja cambiar.
  const [prefill] = useState(() => chatDraft(view, host.profile))
  const aui = useAui()
  const delegation = state.delegations.find((item) => item.grant_id === state.consentedId) ?? null
  const authorized = useMemo(() => (delegation ? [delegation] : []), [delegation])
  const traces = useDelegationTraces(authorized, true)
  const inFlight = hasTaskInFlight(authorized)
  const grantId = delegation?.grant_id

  useEffect(() => {
    if (!grantId || !inFlight) return
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = () => {
      timer = setTimeout(async () => {
        try {
          const read = await getDelegation(grantId)
          if (active) dispatch({ type: "delegation-read", delegation: read })
        } catch {
          // Una relectura que falla deja la tarjeta como estaba; la siguiente reintenta.
        }
        if (active) poll()
      }, DELEGATION_POLL_MS)
    }
    poll()
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [grantId, inFlight])

  const propose = async (draft: DelegationDraft) => {
    const checked = proposalRequest(draft, host.profile)
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
      dispatch({ type: "authorize-failed", error: delegationErrorText(error), canAuthorize })
    }
  }

  /** §8.2: la decisión vuelve al comprador como una compra HP nueva, prellenada en el chat. */
  const buyFromChat = (prefillRequest: string) => {
    host.onPurchaseModeChange("present")
    aui.composer().setText(prefillRequest)
    document.querySelector<HTMLElement>("[aria-label='Mensaje de compra']")?.focus()
  }

  const runId = delegation?.task.run_id
  return (
    <DelegationInChat
      offers={view.offers}
      draft={state.draft ?? prefill}
      flow={state.flow}
      delegation={delegation}
      trace={runId ? traces[runId] : null}
      onPropose={propose}
      onCloseForm={() => dispatch({ type: "form-closed" })}
      onOpenForm={() => dispatch({ type: "form-opened" })}
      onAuthorize={authorize}
      onBackToForm={() => dispatch({ type: "form-opened" })}
      onBuyFromChat={buyFromChat}
    />
  )
}
