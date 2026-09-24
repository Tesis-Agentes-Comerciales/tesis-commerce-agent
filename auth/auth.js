import { DatabaseSync } from "node:sqlite"
import { mkdirSync } from "node:fs"
import { dirname } from "node:path"
import { betterAuth } from "better-auth"
import { jwt } from "better-auth/plugins"

const baseURL = process.env.BETTER_AUTH_URL
const secret = process.env.BETTER_AUTH_SECRET
const databasePath = process.env.AUTH_DATABASE_PATH ?? ".local/auth.db"

if (!baseURL || !secret || secret.length < 32) {
  throw new Error("BETTER_AUTH_URL and a 32-character BETTER_AUTH_SECRET are required")
}
const authOrigin = new URL(baseURL)
if (
  authOrigin.origin !== baseURL ||
  (authOrigin.protocol !== "https:" &&
    !["localhost", "127.0.0.1"].includes(authOrigin.hostname))
) {
  throw new Error("BETTER_AUTH_URL must be an HTTPS origin outside local development")
}

mkdirSync(dirname(databasePath), { recursive: true })

export const auth = betterAuth({
  baseURL,
  secret,
  database: new DatabaseSync(databasePath),
  emailAndPassword: { enabled: true },
  rateLimit: { enabled: true, storage: "database", window: 60, max: 100 },
  advanced: { ipAddress: { ipAddressHeaders: ["x-real-ip"] } },
  trustedOrigins: [baseURL],
  plugins: [
    jwt({
      jwks: { keyPairConfig: { alg: "ES256" } },
      jwt: {
        issuer: baseURL,
        audience: baseURL,
        expirationTime: "5m",
        definePayload: ({ user }) => ({ sub: user.id }),
      },
    }),
  ],
})
