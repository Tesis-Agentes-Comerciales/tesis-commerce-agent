import { focusRing } from "@/components/agent"
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

  const connectionLabel =
    connection === "online"
      ? "Agente conectado"
      : connection === "offline"
        ? "Agente sin conexión"
        : "Conectando"

  return (
    <>
      <main className="flex h-svh min-h-0 flex-col overflow-hidden bg-white text-[#0d0d0d]">
        <header className="flex h-14 shrink-0 items-center justify-between px-3 sm:px-5">
          <div className="flex min-w-0 items-center gap-2.5">
            <h1 className="truncate text-sm font-semibold">Comprador ACP</h1>
            <span
              className="flex items-center gap-1.5 text-xs text-[#6b6b6b]"
              title={AGENT_API_URL}
            >
              <span
                className={`size-1.5 rounded-full ${
                  connection === "online"
                    ? "bg-emerald-500"
                    : connection === "offline"
                      ? "bg-red-500"
                      : "animate-pulse bg-amber-500"
                }`}
                aria-hidden
              />
              {connectionLabel}
            </span>
          </div>

          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => setProfileOpen(true)}
              className={`flex size-9 items-center justify-center rounded-full transition-colors hover:bg-[#f2f2f2] ${focusRing}`}
              aria-label="Datos de entrega"
            >
              <Settings2Icon className="size-[18px]" aria-hidden />
            </button>
          </div>
        </header>

        <section className="min-h-0 flex-1">
          <CommerceRuntime key={session} profile={profile} onNewPurchase={startNewPurchase}>
            {THREAD}
          </CommerceRuntime>
        </section>
      </main>

      <Dialog open={profileOpen} onOpenChange={setProfileOpen}>
        <DialogContent className="max-h-[calc(100svh-2rem)] overflow-y-auto p-0 sm:max-w-md">
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
