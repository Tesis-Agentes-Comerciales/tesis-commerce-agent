import { chip, focusRing } from "@/components/agent"
import { Thread } from "@/components/assistant-ui/elements/thread.aui"
import { CommerceWelcome } from "@/components/commerce-welcome"
import { PurchaseModeSwitch, type PurchaseMode } from "@/components/purchase-mode-switch"
import { SessionGate } from "@/components/session-gate"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"
import { CommerceRuntime } from "@/features/commerce-runtime"
import { DelegationInChatMount } from "@/features/delegation-in-chat"
import { DelegationsMount } from "@/features/delegations"
import { DelegationsHostContext } from "@/features/delegations-host"
import { staleApprovalPart } from "@/features/stale-terms"
import { authClient } from "@/lib/auth-client"
import { INITIAL_AGENT_STATE, type AgentAssistantPart, type AgentState } from "@/lib/agent-contract"
import { AGENT_API_URL, health, pendingPurchase, type PendingPurchase } from "@/lib/commerce-api"
import { DEMO_PROFILE } from "@/lib/commerce-types"
import { ListChecksIcon, Settings2Icon } from "lucide-react"
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react"

const BuyerProfileCard = lazy(() =>
  import("@/components/buyer-profile-card").then((module) => ({
    default: module.BuyerProfileCard,
  })),
)

type ConnectionStatus = "checking" | "online" | "offline"

const HEADER = "flex h-14 shrink-0 items-center justify-end gap-2 px-3 sm:px-5"
/**
 * Con HNP la cabecera suma el modo de compra: a la izquierda en una pantalla ancha y, en un
 * teléfono, en una fila propia debajo de «Salir», a todo el ancho (a 320 px no cabe al lado).
 */
const HEADER_WITH_MODES =
  "flex shrink-0 flex-wrap items-center justify-end gap-x-2 gap-y-1 px-3 pt-2 pb-1 sm:h-14 sm:flex-nowrap sm:px-5 sm:py-0"

// Elemento estable: `CommerceRuntime` solo puede recibir props que no cambien entre renders.
// La compra delegada del hilo y «Mis delegaciones» viven dentro del runtime y reciben lo que cambia
// por contexto.
const THREAD_COMPONENTS = { Welcome: CommerceWelcome, AfterMessages: DelegationInChatMount }
const CHAT = (
  <>
    <Thread components={THREAD_COMPONENTS} autoFocus />
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
  // El modo del chat: con «Delegar», cada envío busca y prepara una delegación en el hilo.
  const [purchaseMode, setPurchaseMode] = useState<PurchaseMode>("present")
  const [delegationsOpen, setDelegationsOpen] = useState(false)
  const delegations = useMemo(
    () =>
      hnpEnabled
        ? {
            open: delegationsOpen,
            onOpenChange: setDelegationsOpen,
            profile,
            purchaseMode,
            onPurchaseModeChange: setPurchaseMode,
          }
        : null,
    [hnpEnabled, delegationsOpen, profile, purchaseMode],
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
        <header className={hnpEnabled ? HEADER_WITH_MODES : HEADER}>
          {/* El modo del chat: «Delegar» busca y deja la delegación en el hilo, sin diálogo. */}
          {hnpEnabled && (
            <PurchaseModeSwitch
              mode={purchaseMode}
              onModeChange={setPurchaseMode}
              className="order-last w-full sm:order-none sm:mr-auto sm:w-auto"
            />
          )}
          {hnpEnabled && (
            <button
              type="button"
              onClick={() => setDelegationsOpen(true)}
              className={`flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm hover:bg-[#f2f2f2] ${focusRing}`}
            >
              <ListChecksIcon className="size-4" aria-hidden />
              Mis delegaciones
            </button>
          )}
          {connection === "offline" && (
            <span className={chip({ tone: "danger" })} title={AGENT_API_URL} role="status">
              <span className="size-1.5 rounded-full bg-red-600" aria-hidden />
              Agente sin conexión
            </span>
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
              purchaseMode={hnpEnabled ? purchaseMode : "present"}
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
