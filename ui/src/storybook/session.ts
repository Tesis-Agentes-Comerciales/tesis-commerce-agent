import { mocked } from "storybook/test"

import { authClient, buyerHeaders, buyerToken } from "@/lib/auth-client"

// Sesión de Better Auth en Storybook. `@/lib/auth-client` es el doble de `src/lib/__mocks__/` (ver
// `sb.mock` en .storybook/preview.tsx); aquí solo se decide qué responde según el parámetro
// `session` de cada historia. Datos sintéticos, alineados con DEMO_PROFILE.

export type StorySession = "authenticated" | "anonymous" | "pending"

type SessionState = ReturnType<typeof authClient.useSession>
type SessionData = NonNullable<SessionState["data"]>

const ISSUED_AT = new Date("2026-09-20T12:00:00Z")

export const STORY_SESSION: SessionData = {
  user: {
    id: "storybook-user",
    name: "Ana Demo",
    email: "ana@example.test",
    emailVerified: true,
    image: null,
    createdAt: ISSUED_AT,
    updatedAt: ISSUED_AT,
  },
  session: {
    id: "storybook-session",
    userId: "storybook-user",
    token: "storybook-session-token",
    expiresAt: new Date("2099-09-20T13:00:00Z"),
    createdAt: ISSUED_AT,
    updatedAt: ISSUED_AT,
  },
}

export const STORY_BUYER_TOKEN = "storybook-buyer-token"

/** El servicio de acceso responde como Better Auth ante credenciales inválidas. */
export const SIGN_IN_REJECTED = {
  data: null,
  error: { status: 401, statusText: "UNAUTHORIZED", code: "INVALID_EMAIL_OR_PASSWORD" },
}

function sessionState(session: StorySession): SessionState {
  return {
    data: session === "authenticated" ? STORY_SESSION : null,
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
  mocked(authClient.token).mockResolvedValue({ data: { token: STORY_BUYER_TOKEN }, error: null } as never)
  mocked(buyerToken).mockResolvedValue(STORY_BUYER_TOKEN)
  mocked(buyerHeaders).mockResolvedValue({ Authorization: `Bearer ${STORY_BUYER_TOKEN}` })
}
