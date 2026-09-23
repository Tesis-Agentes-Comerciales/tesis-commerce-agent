import { chip, focusRing } from "@/components/agent"
import { Thread } from "@/components/assistant-ui/elements/thread.aui"
import { CommerceWelcome } from "@/components/commerce-welcome"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"
import { CommerceRuntime } from "@/features/commerce-runtime"
import { AGENT_API_URL, health } from "@/lib/commerce-api"
import { DEMO_PROFILE } from "@/lib/commerce-types"
import { Settings2Icon } from "lucide-react"
import { lazy, Suspense, useCallback, useEffect, useState } from "react"

const BuyerProfileCard = lazy(() =>
  import("@/components/buyer-profile-card").then((module) => ({
    default: module.BuyerProfileCard,
  })),
)

type ConnectionStatus = "checking" | "online" | "offline"

// Elemento estable: `CommerceRuntime` solo puede recibir props que no cambien entre renders.
const THREAD = <Thread components={{ Welcome: CommerceWelcome }} autoFocus />

function App() {
  const [profile, setProfile] = useState(DEMO_PROFILE)
  const [session, setSession] = useState(0)
  const [connection, setConnection] = useState<ConnectionStatus>("checking")
  const [profileOpen, setProfileOpen] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    health(controller.signal)
      .then(() => setConnection("online"))
      .catch(() => {
        if (!controller.signal.aborted) setConnection("offline")
      })
    return () => controller.abort()
  }, [])

  // "Nueva compra" reinicia la conversación; la compra anterior ya quedó registrada en el backend.
  const startNewPurchase = useCallback(() => setSession((current) => current + 1), [])

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
          <CommerceRuntime key={session} profile={profile} onNewPurchase={startNewPurchase}>
            {THREAD}
          </CommerceRuntime>
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

export default App
