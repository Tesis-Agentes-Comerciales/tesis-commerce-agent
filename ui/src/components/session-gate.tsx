import { authClient } from "@/lib/auth-client"
import { useState, type FormEvent, type ReactNode } from "react"

type Session = NonNullable<ReturnType<typeof authClient.useSession>["data"]>

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

/** Better Auth gate shared by every entry: nothing inside renders without a session. */
export function SessionGate({ children }: { children: (session: Session) => ReactNode }) {
  const { data: session, isPending } = authClient.useSession()
  if (isPending) {
    return <main className="flex min-h-svh items-center justify-center text-sm">Cargando sesión…</main>
  }
  if (!session) return <SignIn />
  return children(session)
}
