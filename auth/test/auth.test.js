import assert from "node:assert/strict"
import { spawn } from "node:child_process"
import { createPublicKey, verify } from "node:crypto"
import { mkdtempSync, rmSync } from "node:fs"
import { createServer } from "node:net"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { setTimeout as delay } from "node:timers/promises"
import test from "node:test"

async function freePort() {
  const server = createServer()
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
  const port = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return port
}

function jsonPart(value) {
  return JSON.parse(Buffer.from(value, "base64url").toString("utf8"))
}

test("email/password session issues a short-lived JWKS-verifiable buyer token", async () => {
  const directory = mkdtempSync(join(tmpdir(), "tesis-auth-test-"))
  const port = await freePort()
  const origin = `http://127.0.0.1:${port}`
  const child = spawn(process.execPath, ["server.js"], {
    cwd: new URL("..", import.meta.url),
    env: {
      ...process.env,
      BETTER_AUTH_URL: origin,
      BETTER_AUTH_SECRET: "synthetic-test-secret-with-at-least-32-characters",
      AUTH_DATABASE_PATH: join(directory, "auth.db"),
      PORT: String(port),
    },
    stdio: "ignore",
  })

  try {
    let ready = false
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (child.exitCode !== null) break
      try {
        const response = await fetch(`${origin}/api/auth/jwks`)
        if (response.ok) {
          ready = true
          break
        }
      } catch {
        // Server migration and startup may still be running.
      }
      await delay(50)
    }
    assert.equal(ready, true, "auth service must become ready")

    const signup = await fetch(`${origin}/api/auth/sign-up/email`, {
      method: "POST",
      headers: { "content-type": "application/json", origin },
      body: JSON.stringify({
        name: "Demo Buyer",
        email: "buyer@example.test",
        password: "demo-password-123",
      }),
    })
    assert.equal(signup.status, 200)
    const user = await signup.json()
    const cookies = signup.headers.getSetCookie().map((cookie) => cookie.split(";")[0])
    assert.ok(cookies.length > 0)

    const response = await fetch(`${origin}/api/auth/token`, {
      headers: { cookie: cookies.join("; ") },
    })
    assert.equal(response.status, 200)
    const { token } = await response.json()
    const [headerPart, payloadPart, signaturePart] = token.split(".")
    const header = jsonPart(headerPart)
    const payload = jsonPart(payloadPart)
    const jwks = await (await fetch(`${origin}/api/auth/jwks`)).json()
    const jwk = jwks.keys.find((key) => key.kid === header.kid)
    assert.ok(jwk)
    assert.equal(header.alg, "ES256")
    assert.equal(payload.sub, user.user.id)
    assert.equal(payload.iss, origin)
    assert.equal(payload.aud, origin)
    assert.ok(payload.exp - payload.iat <= 300)
    assert.equal(
      verify(
        "sha256",
        Buffer.from(`${headerPart}.${payloadPart}`),
        { key: createPublicKey({ key: jwk, format: "jwk" }), dsaEncoding: "ieee-p1363" },
        Buffer.from(signaturePart, "base64url"),
      ),
      true,
    )
  } finally {
    child.kill()
    rmSync(directory, { recursive: true, force: true })
  }
})
