import { mocked } from "storybook/test"

import { authClient, buyerHeaders, buyerToken } from "@/lib/auth-client"
import { BUYER_SESSION, BUYER_TOKEN } from "@/storybook/fixtures"

// Sesión de Better Auth en Storybook. `@/lib/auth-client` es el doble de `src/lib/__mocks__/` (ver
// `sb.mock` en .storybook/preview.tsx); aquí solo se decide qué responde según el parámetro
// `session` de cada historia, con la sesión sintética de `fixtures.ts`.

export type StorySession = "authenticated" | "anonymous" | "pending"

type SessionState = ReturnType<typeof authClient.useSession>

/** El servicio de acceso responde como Better Auth ante credenciales inválidas. */
export const SIGN_IN_REJECTED = {
  data: null,
  error: { status: 401, statusText: "UNAUTHORIZED", code: "INVALID_EMAIL_OR_PASSWORD" },
}

function sessionState(session: StorySession): SessionState {
  return {
    data: session === "authenticated" ? BUYER_SESSION : null,
    isPending: session === "pending",
    isRefetching: false,
    error: null,
    refetch: async () => {},
  } as SessionState
}

/** Aplica la sesión de la historia; las acciones nunca salen del navegador. */
export function mockSession(session: StorySession = "authenticated") {
  mocked(authClient.useSession).mockReturnValue(sessionState(session))
  mocked(authClient.signIn.email).mockResolvedValue(SIGN_IN_REJECTED as never)
  mocked(authClient.signUp.email).mockResolvedValue(SIGN_IN_REJECTED as never)
  mocked(authClient.signOut).mockResolvedValue({ data: { success: true }, error: null } as never)
  mocked(authClient.token).mockResolvedValue({ data: { token: BUYER_TOKEN }, error: null } as never)
  mocked(buyerToken).mockResolvedValue(BUYER_TOKEN)
  mocked(buyerHeaders).mockResolvedValue({ Authorization: `Bearer ${BUYER_TOKEN}` })
}
