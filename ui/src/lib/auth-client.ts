import { createAuthClient } from "better-auth/react"
import { jwtClient } from "better-auth/client/plugins"

export const authClient = createAuthClient({
  plugins: [jwtClient()],
})

export async function buyerToken(): Promise<string> {
  const { data, error } = await authClient.token()
  if (error || !data?.token) throw new Error("Tu sesión expiró. Inicia sesión de nuevo.")
  return data.token
}

export async function buyerHeaders(): Promise<Record<string, string>> {
  return { Authorization: `Bearer ${await buyerToken()}` }
}
