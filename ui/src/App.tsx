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
import { authClient } from "@/lib/auth-client"
import { INITIAL_AGENT_STATE, type AgentAssistantPart, type AgentState } from "@/lib/agent-contract"
import { AGENT_API_URL, health, pendingPurchase, type PendingPurchase } from "@/lib/commerce-api"
import { DEMO_PROFILE } from "@/lib/commerce-types"
import { Settings2Icon } from "lucide-react"
import { lazy, Suspense, useCallback, useEffect, useState, type FormEvent } from "react"

const BuyerProfileCard = lazy(() =>
  import("@/components/buyer-profile-card").then((module) => ({
    default: module.BuyerProfileCard,
  })),
)

type ConnectionStatus = "checking" | "online" | "offline"

// Elemento estable: `CommerceRuntime` solo puede recibir props que no cambien entre renders.
const THREAD = <Thread components={{ Welcome: CommerceWelcome }} autoFocus />

function restoredState(pending: PendingPurchase | null): AgentState {
  if (!pending) return INITIAL_AGENT_STATE
  const { purchase, approval_terms: terms } = pending
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

function SignIn() {
  const [register, setRegister] = useState(false)
  const [name, setName] = useState("")
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError("")
    try {
      const result = register
        ? await authClient.signUp.email({ name: name.trim(), email: email.trim(), password })
        : await authClient.signIn.email({ email: email.trim(), password })
      if (result.error) setError("No pude iniciar sesión. Revisa tus datos e intenta de nuevo.")
    } catch {
      setError("No pude conectar con el servicio de acceso. Intenta de nuevo.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="flex min-h-svh items-center justify-center bg-white px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-3xl border border-[#ececec] p-8"
      >
        <h1 className="text-xl font-semibold">Comprador ACP</h1>
        <p className="text-sm text-[#666]">
          {register ? "Crea una cuenta para la demo." : "Inicia sesión para continuar tu compra."}
        </p>
        {register && (
          <label className="block text-sm">
            Nombre
            <input
              required
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="mt-1 w-full rounded-xl border p-3"
              autoComplete="name"
            />
          </label>
        )}
        <label className="block text-sm">
          Correo
          <input
            required
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="mt-1 w-full rounded-xl border p-3"
            autoComplete="email"
          />
        </label>
        <label className="block text-sm">
          Contraseña
          <input
            required
            type="password"
            minLength={8}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="mt-1 w-full rounded-xl border p-3"
            autoComplete={register ? "new-password" : "current-password"}
          />
        </label>
        {error && (
          <p role="alert" className="text-sm text-red-700">
            {error}
          </p>
        )}
        <button disabled={busy} className="w-full rounded-xl bg-[#111] p-3 text-white disabled:opacity-50">
          {busy ? "Espera…" : register ? "Crear cuenta" : "Entrar"}
        </button>
        <button
          type="button"
          onClick={() => {
            setRegister(!register)
            setError("")
          }}
          className="w-full text-sm underline"
        >
          {register ? "Ya tengo cuenta" : "Crear cuenta"}
        </button>
      </form>
    </main>
  )
}

function CommerceApp({ userId }: { userId: string }) {
  const [profile, setProfile] = useState(DEMO_PROFILE)
  const [session, setSession] = useState(0)
  const [connection, setConnection] = useState<ConnectionStatus>("checking")
  const [profileOpen, setProfileOpen] = useState(false)
  const [initialState, setInitialState] = useState<AgentState | null>(null)
  const [bootstrapError, setBootstrapError] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    health(controller.signal)
      .then(() => setConnection("online"))
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
          <CommerceRuntime
            key={`${userId}:${session}`}
            profile={profile}
            initialState={initialState}
            onNewPurchase={startNewPurchase}
          >
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

function App() {
  const { data: session, isPending } = authClient.useSession()
  if (isPending) {
    return <main className="flex min-h-svh items-center justify-center text-sm">Cargando sesión…</main>
  }
  if (!session) return <SignIn />
  return <CommerceApp key={session.user.id} userId={session.user.id} />
}

export default App
