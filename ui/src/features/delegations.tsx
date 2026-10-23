import { useAui } from "@assistant-ui/react"
import { useCallback, useContext, useEffect, useReducer, useRef } from "react"

import { DELEGATIONS_COPY } from "@/components/delegation/delegation-copy"
import { DelegationList } from "@/components/delegation/delegation-list"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { DELEGATION_POLL_MS, useDelegationTraces } from "@/features/delegation-traces"
import { DelegationsHostContext, type DelegationsHost } from "@/features/delegations-host"
import {
  activeGrantIds,
  delegationsReducer,
  hasTaskInFlight,
  INITIAL_DELEGATIONS,
} from "@/features/delegations-state"
import {
  delegationErrorCode,
  delegationErrorText,
  getDelegation,
  myDelegations,
  revokeAllDelegations,
  revokeDelegation,
} from "@/lib/delegation-api"

/**
 * «Mis delegaciones» dentro del runtime del chat: ante un escalamiento, vuelve a «Comprar conmigo» y
 * escribe la compra HP en el compositor. Sin anfitrión (HNP apagado) no monta nada. Delegar una
 * compra nueva ya no pasa por aquí: va en el hilo (`delegation-in-chat.tsx`).
 */
export function DelegationsMount() {
  const host = useContext(DelegationsHostContext)
  return host ? <Delegations {...host} /> : null
}

/**
 * «Mis delegaciones» (§8.4), contra las rutas del comprador (§5.1). Cada acción relee lo que cambió:
 * el estado lo dice el backend. Con el diálogo abierto, la lista y la gobernanza de cada compra se
 * releen cada 2 s mientras alguna tarea espera el disparo o está comprando.
 */
function Delegations({ open, onOpenChange, onPurchaseModeChange }: DelegationsHost) {
  const [state, dispatch] = useReducer(delegationsReducer, INITIAL_DELEGATIONS)
  const aui = useAui()
  // Al cerrar para volver al chat con un escalamiento, el foco va al compositor.
  const toComposer = useRef(false)
  // Lecturas de la lista empezadas y la más reciente aplicada: una lenta no pisa a una posterior.
  const listReads = useRef({ started: 0, applied: 0 })
  const traces = useDelegationTraces(state.delegations, open)
  const inFlight = hasTaskInFlight(state.delegations)

  const reload = useCallback(async (signal?: AbortSignal, { quiet = false } = {}) => {
    const reads = listReads.current
    const read = ++reads.started
    if (!quiet) dispatch({ type: "list-loading" })
    try {
      const { delegations } = await myDelegations(signal)
      if (read < reads.applied) return
      reads.applied = read
      dispatch({ type: "list-loaded", delegations })
    } catch (error) {
      // Una relectura del sondeo que falla deja la lista como estaba; la siguiente reintenta.
      if (signal?.aborted || quiet || read < reads.applied) return
      dispatch({ type: "list-failed", error: delegationErrorText(error) })
    }
  }, [])

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    void reload(controller.signal)
    return () => controller.abort()
  }, [open, reload])

  useEffect(() => {
    if (!open || !inFlight) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = () => {
      timer = setTimeout(async () => {
        await reload(controller.signal, { quiet: true })
        if (!controller.signal.aborted) poll()
      }, DELEGATION_POLL_MS)
    }
    poll()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [open, inFlight, reload])

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

  /** De vuelta al chat, con el foco en el compositor, listo para escribir. */
  const backToChat = () => {
    toComposer.current = true
    onOpenChange(false)
  }

  /** §8.2: la decisión vuelve al comprador como una compra HP nueva, prellenada en el chat. */
  const buyFromChat = (prefillRequest: string) => {
    onPurchaseModeChange("present")
    aui.composer().setText(prefillRequest)
    backToChat()
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="delegations"
        onCloseAutoFocus={(event) => {
          if (!toComposer.current) return
          toComposer.current = false
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
          <DelegationList
            delegations={state.delegations}
            revocations={state.revocations}
            traces={traces}
            loading={state.list === "loading"}
            error={state.list === "failed" ? state.listError : null}
            revokingAll={state.revokingAll}
            revokeAllError={state.revokeAllError}
            onRevoke={revoke}
            onRevokeAll={revokeAll}
            onBuyFromChat={buyFromChat}
            titleHidden
          />
        </div>
      </DialogContent>
    </Dialog>
  )
}
