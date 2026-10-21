import { chip, focusRing } from "@/components/agent"
import { Thread } from "@/components/assistant-ui/elements/thread.aui"
import { CommerceWelcome } from "@/components/commerce-welcome"
import { SessionGate } from "@/components/session-gate"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"
import { CommerceRuntime } from "@/features/commerce-runtime"
import { DelegationsMount } from "@/features/delegations"
import { DelegationsHostContext } from "@/features/delegations-host"
import { staleApprovalPart } from "@/features/stale-terms"
import { authClient } from "@/lib/auth-client"
import { INITIAL_AGENT_STATE, type AgentAssistantPart, type AgentState } from "@/lib/agent-contract"
import { AGENT_API_URL, health, pendingPurchase, type PendingPurchase } from "@/lib/commerce-api"
import { DEMO_PROFILE } from "@/lib/commerce-types"
import { Settings2Icon, ShieldCheckIcon } from "lucide-react"
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react"

const BuyerProfileCard = lazy(() =>
  import("@/components/buyer-profile-card").then((module) => ({
    default: module.BuyerProfileCard,
  })),
)

type ConnectionStatus = "checking" | "online" | "offline"

// Elemento estable: `CommerceRuntime` solo puede recibir props que no cambien entre renders.
// «Delegaciones» vive dentro del runtime y recibe lo que cambia por contexto.
const CHAT = (
  <>
    <Thread components={{ Welcome: CommerceWelcome }} autoFocus />
    <DelegationsMount />
  </>
)

function restoredState(pending: PendingPurchase | null): AgentState {
  if (!pending) return INITIAL_AGENT_STATE
  const { purchase, approval_terms: terms, terms_status: termsStatus } = pending
  const content: AgentAssistantPart[] = [
    { type: "text", text: "Recuperé tu compra pendiente. Revisa su estado antes de continuar." },
  ]
  if (terms) {
    const args = { run_id: purchase.run_id }
    content.push({
      type: "tool-call",
      toolCallId: `restored-${purchase.run_id}`,
      toolName: "request_approval",
      args,
      argsText: JSON.stringify(args),
      result: { run_id: purchase.run_id, stage: purchase.stage, terms },
      isError: false,
    })
  } else if (termsStatus && purchase.stage === "awaiting_confirmation") {
    // Los términos ya no valen: la misma forma que el paso que falla en el stream, con «Cancelar».
    content.push(staleApprovalPart(purchase.run_id, termsStatus))
  }
  return {
    messages: [
      {
        id: `restored-${purchase.run_id}`,
        role: "assistant",
        status: { type: "complete", reason: "stop" },
        content,
      },
    ],
    purchase: {
      run_id: purchase.run_id,
      stage: purchase.stage,
      order_id: purchase.order_id,
    },
  }
}

function CommerceApp({ userId }: { userId: string }) {
  const [profile, setProfile] = useState(DEMO_PROFILE)
  const [session, setSession] = useState(0)
  const [connection, setConnection] = useState<ConnectionStatus>("checking")
  const [profileOpen, setProfileOpen] = useState(false)
  const [initialState, setInitialState] = useState<AgentState | null>(null)
  const [bootstrapError, setBootstrapError] = useState(false)
  // Las delegaciones (HNP) solo aparecen si `/health` dice que la API las expone.
  const [hnpEnabled, setHnpEnabled] = useState(false)
  const [delegationsOpen, setDelegationsOpen] = useState(false)
  const delegations = useMemo(
    () =>
      hnpEnabled
        ? { open: delegationsOpen, onOpenChange: setDelegationsOpen, profile }
        : null,
    [hnpEnabled, delegationsOpen, profile],
  )

  useEffect(() => {
    const controller = new AbortController()
    health(controller.signal)
      .then((condition) => {
        setConnection("online")
        setHnpEnabled(condition.hnp_enabled === true)
      })
      .catch(() => {
        if (!controller.signal.aborted) setConnection("offline")
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    let active = true
    pendingPurchase()
      .then((pending) => {
        if (active) setInitialState(restoredState(pending))
      })
      .catch(() => {
        if (active) setBootstrapError(true)
      })
    return () => {
      active = false
    }
  }, [userId])

  // "Nueva compra" reinicia la conversación; la compra anterior ya quedó registrada en el backend.
  const startNewPurchase = useCallback(() => {
    setInitialState(INITIAL_AGENT_STATE)
    setSession((current) => current + 1)
  }, [])

  if (bootstrapError) {
    return (
      <main className="flex min-h-svh items-center justify-center">
        <button onClick={() => window.location.reload()} className="rounded-xl border p-3">
          No pude recuperar tus compras. Reintentar
        </button>
      </main>
    )
  }
  if (initialState === null) {
    return (
      <main className="flex min-h-svh items-center justify-center text-sm text-[#666]">
        Cargando tu sesión…
      </main>
    )
  }

  return (
    <>
      <main className="flex h-svh min-h-0 flex-col overflow-hidden bg-white text-[#0d0d0d]">
        {/* Solo se anuncia la conexión cuando falla: conectado es el caso normal y no necesita aviso. */}
        <header className="flex h-14 shrink-0 items-center justify-end gap-2 px-3 sm:px-5">
          {connection === "offline" && (
            <span className={chip({ tone: "danger" })} title={AGENT_API_URL} role="status">
              <span className="size-1.5 rounded-full bg-red-600" aria-hidden />
              Agente sin conexión
            </span>
          )}
          {hnpEnabled && (
            <button
              type="button"
              onClick={() => setDelegationsOpen(true)}
              className={`flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm hover:bg-[#f2f2f2] ${focusRing}`}
            >
              <ShieldCheckIcon className="size-4" aria-hidden />
              Delegaciones
            </button>
          )}
          <button
            type="button"
            onClick={() => authClient.signOut()}
            className="rounded-xl px-3 py-2 text-sm hover:bg-[#f2f2f2]"
          >
            Salir
          </button>
          <button
            type="button"
            onClick={() => setProfileOpen(true)}
            className={`flex size-9 items-center justify-center rounded-full transition-colors hover:bg-[#f2f2f2] ${focusRing}`}
            aria-label="Datos de entrega"
          >
            <Settings2Icon className="size-[18px]" aria-hidden />
          </button>
        </header>

        <section className="min-h-0 flex-1">
          <DelegationsHostContext.Provider value={delegations}>
            <CommerceRuntime
              key={`${userId}:${session}`}
              profile={profile}
              initialState={initialState}
              onNewPurchase={startNewPurchase}
            >
              {CHAT}
            </CommerceRuntime>
          </DelegationsHostContext.Provider>
        </section>
      </main>

      <Dialog open={profileOpen} onOpenChange={setProfileOpen}>
        <DialogContent className="max-h-[calc(100svh-2rem)] overflow-y-auto rounded-[24px] border border-[#ececec] p-0 shadow-[0_1px_2px_rgba(0,0,0,0.04),0_12px_32px_-18px_rgba(0,0,0,0.18)] ring-0 sm:max-w-md">
          <DialogTitle className="sr-only">Datos de entrega</DialogTitle>
          <DialogDescription className="sr-only">
            Configura el perfil sintético utilizado para el checkout sandbox.
          </DialogDescription>
          <Suspense fallback={<div className="p-6 text-sm">Cargando…</div>}>
            <BuyerProfileCard profile={profile} onChange={setProfile} />
          </Suspense>
        </DialogContent>
      </Dialog>

    </>
  )
}

function App() {
  return (
    <SessionGate>
      {(session) => <CommerceApp key={session.user.id} userId={session.user.id} />}
    </SessionGate>
  )
}

export default App
