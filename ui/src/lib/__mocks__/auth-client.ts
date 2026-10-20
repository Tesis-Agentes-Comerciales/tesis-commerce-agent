import { fn } from "storybook/test"

// Doble de `../auth-client.ts` que registra `sb.mock` en `.storybook/preview.tsx`: Better Auth nunca se
// carga en Storybook ni en sus tests, así que ninguna historia llama a /api/auth. Cada historia elige
// la sesión con el parámetro `session`; `src/storybook/session.ts` da las respuestas.

export const authClient = {
  useSession: fn().mockName("authClient.useSession"),
  signIn: { email: fn().mockName("authClient.signIn.email") },
  signUp: { email: fn().mockName("authClient.signUp.email") },
  signOut: fn().mockName("authClient.signOut"),
  token: fn().mockName("authClient.token"),
}

export const buyerToken = fn().mockName("buyerToken")

export const buyerHeaders = fn().mockName("buyerHeaders")
