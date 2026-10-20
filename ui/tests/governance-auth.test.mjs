import "./monitor-test-loader.mjs"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { registerHooks } from "node:module"
import test from "node:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"

// Only Better Auth's network client is doubled; the session gate and the monitor route are the real modules.
const authDouble = "export const authClient = { useSession: () => globalThis.governanceSession }"
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/lib/auth-client") return { url: `data:text/javascript,${encodeURIComponent(authDouble)}`, shortCircuit: true }
    return nextResolve(specifier, context)
  },
})
const { default: GovernanceRoute } = await import("../src/features/governance-monitor/route.tsx")
const render = (session) => {
  globalThis.governanceSession = session
  return renderToStaticMarkup(createElement(GovernanceRoute))
}
const evidenceText = ["Monitor de gobernanza", "Reproducción offline", "HNP · accept erróneo contenido", "AUDIT-MERCHANT-CANCEL-01"]

test("pending session shows only the session loader, never evidence", () => {
  const html = render({ data: null, isPending: true })
  assert.ok(html.includes("Cargando sesión"))
  for (const text of evidenceText) assert.ok(!html.includes(text), text)
})
test("anonymous visitor gets the app sign-in form instead of the monitor", () => {
  const html = render({ data: null, isPending: false })
  assert.ok(html.includes("<form"))
  assert.ok(html.includes("Inicia sesión"))
  for (const text of evidenceText) assert.ok(!html.includes(text), text)
})
test("authenticated session renders the read-only monitor", () => {
  const html = render({ data: { user: { id: "unit-test-user" } }, isPending: false })
  for (const text of [...evidenceText, "Solo lectura"]) assert.ok(html.includes(text), text)
  assert.ok(!html.includes("<form"))
})
test("App and the monitor share one session gate", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8")
  assert.match(app, /<SessionGate>/)
  assert.doesNotMatch(app, /useSession|function SignIn/)
})
