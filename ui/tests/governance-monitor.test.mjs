import "./monitor-test-loader.mjs"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"

const { parseEvidence, scenarioBadge } = await import("../src/features/governance-monitor/adapter.ts")
const { GovernanceMonitor, ScenarioDetails } = await import("../src/features/governance-monitor/GovernanceMonitor.tsx")
const raw = JSON.parse(readFileSync(new URL("../src/features/governance-monitor/evidence.json", import.meta.url), "utf8"))
const bundle = parseEvidence(raw)
const copy = () => structuredClone(raw)
const scenario = (id) => bundle.scenarios.find((s) => s.id === id)

test("adapter preserves source classifications and global PARTIAL", () => {
  assert.equal(bundle.status, "PARTIAL")
  assert.equal(bundle.scenarios.length, 4)
  assert.equal(scenario("hnp-false-accept").executionKind, "scripted offline")
  assert.equal(scenario("hp-allowed").executionKind, "replay")
  assert.equal(scenario("hp-allowed").enforce.outcome, null)
})
test("Q allow and applied deny remain independent; no outcome inferred from Q", () => {
  const s = scenario("hp-denied")
  assert.equal(s.shadow.outcome, "allow")
  assert.equal(s.enforce.outcome, "deny")
  assert.equal(s.execution, "blocked")
  assert.equal(s.commercialPosts, 0)
})
test("erroneous HNP accept does not overwrite actual backend denial", () => {
  const s = scenario("hnp-false-accept")
  assert.equal(s.validator.verdict, "accept")
  assert.equal(s.backend.code, "BACKEND_DENIED")
  assert.deepEqual(s.backend.failures, ["budget"])
  assert.equal(s.effectOccurred, false)
  assert.deepEqual(s.backend.executedPolicyIds, [])
})
test("unknown fields, secret-bearing payloads and missing fields fail closed", () => {
  const extra = copy(); extra.scenarios[0].bearer_token = "unit-test-only"
  assert.throws(() => parseEvidence(extra))
  const missing = copy(); delete missing.scenarios[0].recovery
  assert.throws(() => parseEvidence(missing))
})
test("live labels and non-PARTIAL status are not silently promoted", () => {
  const live = copy(); live.scenarios[0].executionKind = "live_llm"
  assert.throws(() => parseEvidence(live))
  const promoted = copy(); promoted.status = "APPROVED"
  assert.throws(() => parseEvidence(promoted))
})
test("unbound artifacts, duplicated scenarios and unsafe counters rejected", () => {
  const hash = copy(); hash.scenarios[0].evidence[0].sha256 = "0".repeat(64)
  assert.throws(() => parseEvidence(hash))
  const duplicate = copy(); duplicate.scenarios.push(duplicate.scenarios[0])
  assert.throws(() => parseEvidence(duplicate))
  const count = copy(); count.scenarios[0].commercialPosts = true
  assert.throws(() => parseEvidence(count))
})
test("contradictory effects and falsely claimed HNP policies rejected", () => {
  const effect = copy(); effect.scenarios[1].effectOccurred = true
  assert.throws(() => parseEvidence(effect))
  const pdp = copy(); pdp.scenarios[2].backend.executedPolicyIds = ["POL-07"]
  assert.throws(() => parseEvidence(pdp))
})
test("rendering is read-only and visibly states replay, limits and unavailable facts", () => {
  const previous = globalThis.fetch
  globalThis.fetch = () => { throw Error("Rendering must not fetch") }
  try {
    const html = renderToStaticMarkup(createElement(GovernanceMonitor, { bundle }))
    for (const text of ["Solo lectura", "PARTIAL", "Sin LLM en vivo", "replay", "Q · shadow", "D1 · enforce", "No disponible en la fuente", "AUDIT-MERCHANT-CANCEL-01", "I10/D08/D09"]) assert.ok(html.includes(text), text)
    assert.ok(!html.includes("<form"))
    assert.equal((html.match(/aria-pressed=/g) ?? []).length, 4)
  } finally { globalThis.fetch = previous }
})
test("HNP rendering exposes accept versus BACKEND_DENIED and no fictitious effect", () => {
  const html = renderToStaticMarkup(createElement(ScenarioDetails, { scenario: scenario("hnp-false-accept") }))
  for (const text of ["scripted offline", "accept", "BACKEND_DENIED", "budget", "Ausencia de efectos observada en la fuente", "executed_policy_ids=[]"]) assert.ok(html.includes(text), text)
})
test("lost response renders initial uncertainty and later confirmed recovery separately", () => {
  const s = scenario("hp-uncertain")
  assert.equal(s.creation, "uncertain")
  assert.equal(s.recovery, "confirmed")
  const html = renderToStaticMarkup(createElement(ScenarioDetails, { scenario: s }))
  assert.ok(html.includes("Incierta"))
  assert.ok(html.includes("Confirmada · found"))
  assert.ok(!html.includes("→"))
})
test("evidence strings are rendered as text and cannot inject markup", () => {
  const s = structuredClone(scenario("hp-allowed")); s.intent = '<script>alert("unit")</script>'
  const html = renderToStaticMarkup(createElement(ScenarioDetails, { scenario: s }))
  assert.ok(html.includes("&lt;script&gt;"))
  assert.ok(!html.includes("<script>"))
})
test("observer entry loads App lazily and does not import commerce actions", () => {
  const entry = readFileSync(new URL("../src/main.tsx", import.meta.url), "utf8")
  assert.ok(entry.includes("'/governance'"))
  assert.ok(entry.includes("lazy(() => import('./App.tsx'))"))
  const component = readFileSync(new URL("../src/features/governance-monitor/GovernanceMonitor.tsx", import.meta.url), "utf8")
  assert.doesNotMatch(component, /fetch\(|EventSource\(|commerce-api|commerce-runtime|auth-client/)
})

// MON-02A synthetic mutations below exist in memory only, never in the evidence bundle.
const strictCopy = () => structuredClone(bundle)
const htmlFor = (s) => renderToStaticMarkup(createElement(ScenarioDetails, { scenario: s }))

test("MON-02A confirmed historical creation has separate creation and recovery facts", () => {
  const s = scenario("hp-allowed")
  assert.equal(s.execution, "started")
  assert.equal(s.creation, "confirmed")
  assert.equal(s.recovery, "confirmed")
  assert.equal(s.effectOccurred, true)
  assert.ok(htmlFor(s).includes("Creación · observación inicial"))
  assert.ok(htmlFor(s).includes("Estado de recuperación"))
})
test("MON-02A D1 deny has no creation or recovery", () => {
  const s = scenario("hp-denied")
  assert.equal(s.execution, "blocked")
  assert.equal(s.creation, "not_applicable")
  assert.equal(s.recovery, "not_applicable")
  assert.equal(s.commercialPosts, 0)
  assert.equal(s.effectOccurred, false)
  assert.ok(htmlFor(s).includes("No aplicable"))
  assert.ok(!htmlFor(s).includes("Confirmado → Confirmado"))
  assert.ok(!scenarioBadge(s).includes("confirmada"))
})
test("MON-02A HNP accept and deny never become confirmed creation", () => {
  const s = scenario("hnp-false-accept")
  assert.equal(s.validator.verdict, "accept")
  assert.equal(s.backend.code, "BACKEND_DENIED")
  assert.equal(s.creation, "not_applicable")
  assert.equal(s.recovery, "not_applicable")
  assert.ok(!scenarioBadge(s).includes("confirmada"))
})
test("MON-02A denied execution and effect contradictions reject the entire snapshot", () => {
  for (const patch of [{ execution: "started" }, { effectOccurred: true }, { commercialPosts: 1 }, { creation: "confirmed" }, { recovery: "confirmed" }]) {
    const x = strictCopy(); Object.assign(x.scenarios[2], patch)
    assert.throws(() => parseEvidence(x), /Evidencia inconsistente/)
  }
})
test("MON-02A HNP without validator rejects instead of omitting the conflict", () => {
  const x = strictCopy(); x.scenarios[2].validator = null
  assert.throws(() => parseEvidence(x), /HNP sin evaluación/)
})
test("MON-02A not-started confirmed creation is rejected in exported and parsed inputs", () => {
  const raw = copy(); Object.assign(raw.scenarios[0], { execution: "not_started", commercialPosts: 0, effectOccurred: false, recovery: "not_applicable" })
  assert.throws(() => parseEvidence(raw), /Evidencia inconsistente/)
  const x = strictCopy(); Object.assign(x.scenarios[0], { execution: "not_started", commercialPosts: 0, effectOccurred: false, recovery: "not_applicable" })
  assert.throws(() => parseEvidence(x), /Evidencia inconsistente/)
})
test("MON-02A not-started and unknown never get a favorable badge", () => {
  const x = strictCopy()
  Object.assign(x.scenarios[0], { execution: "not_started", creation: "not_applicable", recovery: "not_applicable", commercialPosts: 0, effectOccurred: false })
  let s = parseEvidence(x).scenarios[0]
  assert.equal(scenarioBadge(s), "Operación no iniciada")
  Object.assign(x.scenarios[0], { execution: "unknown", creation: "uncertain", commercialPosts: null, effectOccurred: null, sourceKind: "unavailable", totalMinor: null, currency: null, shadow: null, enforce: null })
  s = parseEvidence(x).scenarios[0]
  assert.ok(scenarioBadge(s).includes("desconocida"))
  assert.ok(htmlFor(s).includes("No disponible en la fuente"))
  assert.ok(!htmlFor(s).includes("Creación histórica confirmada"))
})
test("MON-02A pending creation and explicitly inapplicable recovery stay distinct", () => {
  const x = strictCopy()
  Object.assign(x.scenarios[0], { creation: "uncertain", recovery: "not_applicable", effectOccurred: null })
  const s = parseEvidence(x).scenarios[0]
  assert.equal(scenarioBadge(s), "Creación incierta")
  assert.ok(htmlFor(s).includes("No aplicable"))
  const missing = strictCopy(); delete missing.scenarios[0].recovery
  assert.throws(() => parseEvidence(missing))
})
test("MON-02A not_found preserves uncertainty and cannot authorize retry or imply absence", () => {
  const x = strictCopy()
  Object.assign(x.scenarios[0], { creation: "uncertain", recovery: "not_found", effectOccurred: null })
  const s = parseEvidence(x).scenarios[0]
  assert.ok(htmlFor(s).includes("no autoriza reintento"))
  assert.equal(s.effectOccurred, null)
  x.scenarios[0].effectOccurred = true
  assert.throws(() => parseEvidence(x), /Evidencia inconsistente/)
})
test("MON-02A rejected creation is distinct from a pre-send block", () => {
  const x = strictCopy()
  Object.assign(x.scenarios[0], { creation: "rejected", recovery: "not_applicable", effectOccurred: false })
  const s = parseEvidence(x).scenarios[0]
  assert.equal(s.execution, "started")
  assert.equal(scenarioBadge(s), "Creación rechazada")
})
test("MON-02A direct inconsistent render fails visibly instead of bypassing parser", () => {
  const s = structuredClone(scenario("hnp-false-accept"))
  Object.assign(s, { execution: "started", creation: "confirmed", effectOccurred: true })
  const html = htmlFor(s)
  assert.ok(html.includes('role="alert"'))
  assert.ok(html.includes("Evidencia inconsistente"))
  assert.ok(!html.includes("Creación histórica confirmada"))
})
test("MON-02A budget pass belongs to erroneous validator, source budget denial is preserved", () => {
  const original = JSON.parse(readFileSync(new URL("../../docs/governance-monitor/sources/hnp-false-accept-selected.json", import.meta.url), "utf8"))
  const s = scenario("hnp-false-accept")
  const check = original.record.assessment.checks.find((c) => c.check_id === "budget")
  assert.equal(check.result, "pass")
  assert.equal(original.record.assessment.verdict, "accept")
  assert.equal(original.effective_admission.code, "BACKEND_DENIED")
  assert.deepEqual(original.effective_admission.backend_failures, ["budget"])
  assert.equal(original.record.original_context.terms.total_minor, 13000)
  assert.equal(original.record.original_context.intent.hard.max_total_amount_minor, 12500)
  assert.equal(s.totalMinor, 13000)
  assert.equal(s.budgetLimitMinor, 12500)
  assert.equal(s.validator.checks.find((c) => c.id === "budget").result, "pass")
  const html = htmlFor(s)
  for (const text of ["budget: pass (validador)", "denegado por el backend", "13.000", "12.500", "Discrepancia documentada", "declaración errónea del guion"]) assert.ok(html.includes(text), text)
})
test("MON-02A no verdict is reconstructed from existing decision/evidence IDs", () => {
  const s = scenario("hp-allowed")
  assert.ok(s.enforce.decisionId && s.enforce.evidenceId && s.enforce.digest)
  assert.equal(s.enforce.outcome, null)
  assert.ok(htmlFor(s).includes("No disponible en la fuente"))
})
test("MON-02A real historical source and scripted source have explicit distinct labels", () => {
  assert.ok(htmlFor(scenario("hp-allowed")).includes("HTTP/PostgreSQL real histórico · replay"))
  assert.ok(htmlFor(scenario("hnp-false-accept")).includes("Scripted offline · sin LLM"))
})
test("MON-02A contradictory recovery found versus rejected creation is rejected", () => {
  const x = strictCopy(); x.scenarios[0].creation = "rejected"
  assert.throws(() => parseEvidence(x), /Evidencia inconsistente/)
  const y = strictCopy(); y.scenarios[0].recovery = "not_found"
  assert.throws(() => parseEvidence(y), /Evidencia inconsistente/)
})
test("MON-02A unobserved creation and recovery stay uncertain, never confirmed", () => {
  const x = copy()
  Object.assign(x.scenarios[0], { creation: "uncertain", recovery: "uncertain", effectOccurred: false })
  const s = parseEvidence(x).scenarios[0]
  assert.equal(s.creation, "uncertain")
  assert.equal(s.recovery, "uncertain")
  assert.equal(scenarioBadge(s), "Recuperación incierta")
})
test("MON-02A uncertain recovery does not receive favorable confirmation badge", () => {
  const x = strictCopy(); x.scenarios[0].recovery = "uncertain"
  const s = parseEvidence(x).scenarios[0]
  assert.equal(s.creation, "confirmed")
  assert.equal(scenarioBadge(s), "Recuperación incierta")
  assert.ok(htmlFor(s).includes("bg-amber-100"))
})
test("MON-02A an observed HP POST cannot make creation not applicable", () => {
  const x = strictCopy(); Object.assign(x.scenarios[0], { creation: "not_applicable", recovery: "not_applicable", effectOccurred: false })
  assert.throws(() => parseEvidence(x), /Evidencia inconsistente/)
})
