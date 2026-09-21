import { Thread } from "@/components/assistant-ui/elements/thread.aui"
import { BuyerProfileCard } from "@/components/buyer-profile-card"
import { CommerceWelcome } from "@/components/commerce-welcome"
import { PurchaseReceipt } from "@/components/purchase-receipt"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"
import { TooltipProvider } from "@/components/ui/tooltip"
import { CommerceRuntime } from "@/features/commerce-runtime"
import { AGENT_API_URL, health } from "@/lib/commerce-api"
import {
  DEMO_PROFILE,
  EMPTY_FLOW,
  type ApprovalTerms,
  type PurchaseFlow,
  type PurchasePreparation,
} from "@/lib/commerce-types"
import { Settings2Icon, ShoppingBagIcon } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

type ConnectionStatus = "checking" | "online" | "offline"

function App() {
  const [profile, setProfile] = useState(DEMO_PROFILE)
  const [flow, setFlow] = useState<PurchaseFlow>(EMPTY_FLOW)
  const [session, setSession] = useState(0)
  const [connection, setConnection] = useState<ConnectionStatus>("checking")
  const [profileOpen, setProfileOpen] = useState(false)
  const [receiptOpen, setReceiptOpen] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    health(controller.signal)
      .then(() => setConnection("online"))
      .catch(() => {
        if (!controller.signal.aborted) setConnection("offline")
      })
    return () => controller.abort()
  }, [])

  const handlePreparing = useCallback((request: string) => {
    setReceiptOpen(false)
    setFlow({ status: "preparing", request })
  }, [])

  const handlePrepared = useCallback(
    (request: string, preparation: PurchasePreparation, terms: ApprovalTerms) => {
      setFlow({ status: "awaiting_approval", request, preparation, terms })
      setReceiptOpen(true)
    },
    [],
  )

  const handleError = useCallback((request: string, error: string) => {
    setFlow({ status: "error", request, error })
  }, [])

  const reset = () => {
    setReceiptOpen(false)
    setFlow(EMPTY_FLOW)
    setSession((current) => current + 1)
  }

  const hasReceipt = flow.preparation !== undefined && flow.terms !== undefined
  const connectionLabel =
    connection === "online"
      ? "Agente conectado"
      : connection === "offline"
        ? "Agente sin conexión"
        : "Conectando"
  const purchaseBusy = flow.status === "approving" || flow.status === "completing"

  return (
    <TooltipProvider>
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
            {hasReceipt && (
              <button
                type="button"
                onClick={() => setReceiptOpen(true)}
                className="flex h-9 items-center gap-2 rounded-full px-3 text-sm font-medium transition-colors hover:bg-[#f2f2f2] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-black/20"
              >
                <ShoppingBagIcon className="size-4" aria-hidden />
                <span className="hidden sm:inline">
                  {flow.status === "completed" ? "Ver compra" : "Revisar compra"}
                </span>
              </button>
            )}
            <button
              type="button"
              onClick={() => setProfileOpen(true)}
              className="flex size-9 items-center justify-center rounded-full transition-colors hover:bg-[#f2f2f2] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-black/20"
              aria-label="Datos de entrega"
            >
              <Settings2Icon className="size-[18px]" aria-hidden />
            </button>
          </div>
        </header>

        <section className="min-h-0 flex-1">
          <CommerceRuntime
            key={session}
            profile={profile}
            flowStatus={flow.status}
            onPreparing={handlePreparing}
            onPrepared={handlePrepared}
            onError={handleError}
          >
            <Thread components={{ Welcome: CommerceWelcome }} autoFocus />
          </CommerceRuntime>
        </section>
      </main>

      <Dialog open={profileOpen} onOpenChange={setProfileOpen}>
        <DialogContent className="max-h-[calc(100svh-2rem)] overflow-y-auto p-0 sm:max-w-md">
          <DialogTitle className="sr-only">Datos de entrega</DialogTitle>
          <DialogDescription className="sr-only">
            Configura el perfil sintético utilizado para el checkout sandbox.
          </DialogDescription>
          <BuyerProfileCard profile={profile} onChange={setProfile} />
        </DialogContent>
      </Dialog>

      <Dialog open={hasReceipt && receiptOpen} onOpenChange={setReceiptOpen}>
        <DialogContent
          className="max-h-[calc(100svh-2rem)] overflow-y-auto rounded-3xl p-0 sm:max-w-md"
          showCloseButton={!purchaseBusy}
          onOpenAutoFocus={(event) => event.preventDefault()}
        >
          <DialogTitle className="sr-only">Confirmación de compra</DialogTitle>
          <DialogDescription className="sr-only">
            Revisa los términos autoritativos antes de aprobar la compra.
          </DialogDescription>
          <PurchaseReceipt flow={flow} onChange={setFlow} onReset={reset} />
        </DialogContent>
      </Dialog>
    </TooltipProvider>
  )
}

export default App
