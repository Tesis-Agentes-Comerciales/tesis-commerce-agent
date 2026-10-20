import "./monitor-test-loader.mjs"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { schemaValidator } from "./json-schema-subset.mjs"

const { parseEvidence, experimentIssue, experimentNotices, experimentKindText, validatorText, EXPERIMENT_NOTICE, SIMULATED_TRANSPORT_NOTICE } = await import("../src/features/governance-monitor/adapter.ts")
const { ExperimentDetails } = await import("../src/features/governance-monitor/ExperimentDetails.tsx")
const { GovernanceMonitor } = await import("../src/features/governance-monitor/GovernanceMonitor.tsx")
const { parseDemoSnapshot } = await import("../src/features/governance-monitor/snapshot-contract.ts")
const raw = JSON.parse(readFileSync(new URL("../src/features/governance-monitor/evidence.json", import.meta.url), "utf8"))
const schema = JSON.parse(readFileSync(new URL("../../docs/governance-monitor/governance-monitor-3.schema.json", import.meta.url), "utf8"))
const validate = schemaValidator(schema)
const bundle = parseEvidence(raw)
const copy = () => structuredClone(raw)
const sampleOf = (x, id) => x.experiments[0].cases.find((c) => c.caseId === id).sample
const render = (experiment) => renderToStaticMarkup(createElement(ExperimentDetails, { experiment }))
const snapshot = (value) => ({ schema: "governance-demo-snapshot/1", snapshotId: "hp-hnp-demo-v1", classification: "sanitized_demo_fixture", temporalScope: "historical_closed_artifacts", liveEvents: false, authenticatedUserEvidence: false, bundle: value })

// Synthetic provider run, in memory only: the bundled fixture is a dry-run and never changes here.
function asProvider(value) {
  const x = structuredClone(value), e = x.experiments[0]
  Object.assign(e, { executionKind: "llm validator provider", transport: "provider", label: "HNP-02 · validador LLM · proveedor", notes: ["Proveedor real: una corrida, no una medida general del modelo."] })
  e.actors[1].transport = "openai-responses"
  Object.assign(e.cost, { basis: "provider_reported_usage", providerCalls: e.cost.llmCalls, fakeCalls: 0 })
  return x
}

test("bundled v3 projection adds one dry-run HNP-02 experiment beside the four scenarios", () => {
  assert.equal(bundle.schema, "governance-monitor/3")
  assert.equal(bundle.scenarios.length, 4)
  assert.equal(bundle.experiments.length, 1)
  const e = bundle.experiments[0]
  assert.equal(e.executionKind, "llm validator dry-run")
  assert.equal(e.transport, "fake")
  assert.equal(e.sink, "fictitious_in_memory")
  assert.equal(e.commercialPosts, 0)
  assert.equal(e.cost.providerCalls, 0)
  assert.equal(e.cost.basis, "fake_transport_estimate")
  assert.deepEqual(e.actors.map((a) => [a.role, a.kind, a.transport]), [["buyer", "scripted", "none"], ["validator", "llm", "fake"]])
  assert.equal(experimentIssue(e), null)
})

// Structural rules are shared by the schema and the parser; cross-field invariants live only in the parsers.
const structural = {
  "unknown experiment field": (x) => { x.experiments[0].prompt = "unit-test-only" },
  "previous contract version": (x) => { x.schema = "governance-monitor/2" },
  "missing experiments": (x) => { delete x.experiments },
  "kind and transport disagree": (x) => { x.experiments[0].transport = "provider" },
  "cost basis of another transport": (x) => { x.experiments[0].cost.basis = "provider_reported_usage" },
  "provider calls in a dry-run": (x) => { const c = x.experiments[0].cost; c.providerCalls = 1; c.fakeCalls -= 1 },
  "unknown execution kind": (x) => { x.experiments[0].executionKind = "llm validator live" },
  "operational effects claimed": (x) => { x.experiments[0].operationalEffects = true },
  "commercial POST claimed": (x) => { x.experiments[0].commercialPosts = 1 },
  "real sink": (x) => { x.experiments[0].sink = "checkout" },
  "single actor": (x) => { x.experiments[0].actors.pop() },
  "unknown actor transport": (x) => { x.experiments[0].actors[1].transport = "anthropic" },
  "rate above one": (x) => { x.experiments[0].arms.B.falseBlocks.high = 1.5 },
  "p-value above one": (x) => { x.experiments[0].comparison.all.mcnemarExactP = 2 },
  "unknown admission code": (x) => { sampleOf(x, "valid").A.admissionCode = "APPROVED" },
  "repeated backend failure": (x) => { sampleOf(x, "over-total").A.backendFailures = ["budget", "budget"] },
  "declared pass stored as a non-pass check": (x) => { sampleOf(x, "valid").B.validator.nonPassChecks = { budget: "pass" } },
  "unknown check id": (x) => { sampleOf(x, "valid").B.validator.nonPassChecks = { bindings: "fail" } },
  "model reasons in a sample": (x) => { sampleOf(x, "valid").B.validator.reasons = ["unit-test-only"] },
  "empty outcome count": (x) => { x.experiments[0].cases[0].validatorOutcomes.reject = 0 },
  "timestamp without zone": (x) => { x.experiments[0].createdAt = "2026-10-20 04:43:29" },
  "non-decimal price": (x) => { x.experiments[0].cost.pricesUsdPerMtok.input = "0.25 USD" },
  "unknown case kind": (x) => { x.experiments[0].cases[0].caseKind = "safe" },
}
const crossField = {
  "B admitted what A blocked": (x) => { const p = x.experiments[0].comparison.admissible; p.onlyABlocked = 1; p.pairs += 1 },
  "pair counts do not add up": (x) => { x.experiments[0].comparison.unsafe.bothBlocked += 1 },
  "B with more simulated effects than A": (x) => { x.experiments[0].arms.B.simulatedEffects = x.experiments[0].arms.A.simulatedEffects + 1 },
  "rate disagrees with k/n": (x) => { x.experiments[0].validator.captures.rate = 0.5 },
  "effect in B but not in A": (x) => { const s = sampleOf(x, "over-total"); Object.assign(s.B, { effect: true, admissionCode: "SIMULATED_ONLY", backendFailures: [] }) },
  "validator in arm A": (x) => { const s = sampleOf(x, "valid"); s.A.validator = structuredClone(s.B.validator) },
  "validator actor on a provider transport": (x) => { x.experiments[0].actors[1].transport = "openai-responses" },
}

test("schema and parser accept the bundled projection and reject the same structural mutations", () => {
  assert.deepEqual(validate(raw), [])
  assert.deepEqual(validate(asProvider(raw)), [])
  for (const [name, mutate] of Object.entries(structural)) {
    const x = copy(); mutate(x)
    assert.notDeepEqual(validate(x), [], `schema: ${name}`)
    assert.throws(() => parseEvidence(x), undefined, `parser: ${name}`)
  }
})
test("cross-field invariants are enforced by the parser, beyond the structural schema", () => {
  for (const [name, mutate] of Object.entries(crossField)) {
    const x = copy(); mutate(x)
    assert.deepEqual(validate(x), [], `schema: ${name}`)
    assert.throws(() => parseEvidence(x), /Evidencia inconsistente/, name)
  }
})
test("checker refuses schema keywords it does not implement", () => {
  assert.throws(() => schemaValidator({ $defs: { x: { type: "string", format: "date-time" } } }), /Unsupported keyword format/)
  assert.throws(() => schemaValidator({ $ref: "https://example.invalid/schema" }), /Unsupported \$ref/)
})

test("each execution kind owns its copy; dry-run never reads as a provider or a script", () => {
  const dry = bundle.experiments[0], provider = parseEvidence(asProvider(raw)).experiments[0]
  assert.match(experimentKindText(dry), /simulado \(FakeTransport\)/)
  assert.match(experimentKindText(provider), /proveedor real/)
  const titles = new Set([dry, provider, { executionKind: "scripted offline" }].map((x) => validatorText(x).title))
  assert.equal(titles.size, 3)
  assert.match(validatorText(dry).note, /no proceden de ningún modelo/)
  const html = render(dry)
  assert.ok(html.includes("llm validator dry-run"))
  assert.ok(!html.includes("proveedor real"))
  assert.ok(!html.includes("scripted offline"))
})
test("dry-run banner comes first and states both mandatory notices", () => {
  const html = render(bundle.experiments[0])
  assert.deepEqual(experimentNotices(bundle.experiments[0]), [EXPERIMENT_NOTICE, SIMULATED_TRANSPORT_NOTICE])
  assert.ok(html.startsWith('<section aria-label="Detalle del experimento"'))
  const banner = html.indexOf('role="note"')
  assert.ok(banner > 0 && banner < html.indexOf("<h2"))
  for (const notice of [EXPERIMENT_NOTICE, SIMULATED_TRANSPORT_NOTICE]) assert.ok(html.indexOf(notice) < html.indexOf("<h2"), notice)
})
test("only an explicit provider transport drops the simulated-figures notice", () => {
  const provider = parseEvidence(asProvider(raw)).experiments[0]
  const html = render(provider)
  assert.ok(html.includes(EXPERIMENT_NOTICE))
  assert.ok(!html.includes(SIMULATED_TRANSPORT_NOTICE))
  assert.deepEqual(experimentNotices({ transport: "Provider" }), [EXPERIMENT_NOTICE, SIMULATED_TRANSPORT_NOTICE])
})
test("unknown kinds or transports fail closed in parser, invariants and view", () => {
  for (const patch of [{ executionKind: "llm validator live" }, { executionKind: "scripted offline" }, { transport: "openai" }]) {
    const x = copy(); Object.assign(x.experiments[0], patch)
    assert.throws(() => parseEvidence(x), undefined, JSON.stringify(patch))
  }
  const direct = { ...bundle.experiments[0], executionKind: "llm validator live" }
  assert.match(experimentIssue(direct), /tipo de ejecución y transporte/)
  const html = render(direct)
  assert.ok(html.includes('role="alert"'))
  assert.ok(html.includes(SIMULATED_TRANSPORT_NOTICE))
  assert.ok(!html.includes("Brazos A/B"))
})
test("a scenario cannot claim an LLM kind; LLM runs are accepted only as experiments", () => {
  const x = copy(); x.scenarios[2].executionKind = "llm validator dry-run"
  assert.notDeepEqual(validate(x), [])
  assert.throws(() => parseEvidence(x))
  const duplicate = copy(); duplicate.experiments[0].id = duplicate.scenarios[0].id
  assert.throws(() => parseEvidence(duplicate), /duplicados/)
})
test("a declared pass on a check the backend denied is shown as a documented discrepancy", () => {
  const html = render(bundle.experiments[0])
  assert.ok(html.includes("budget: pass (validador)"))
  assert.ok(html.includes("budget: pass declarado por el validador; denegado por el backend del par"))
  assert.ok(html.includes("Sin respuesta analizada del validador"))
})
test("monitor lists experiments in their own navigation and renders read-only", () => {
  const previous = globalThis.fetch
  globalThis.fetch = () => { throw Error("Rendering must not fetch") }
  try {
    const html = renderToStaticMarkup(createElement(GovernanceMonitor, { bundle }))
    assert.ok(html.includes('aria-label="Experimentos HNP-02"'))
    assert.ok(html.includes(bundle.experiments[0].label))
    assert.equal((html.match(/aria-pressed=/g) ?? []).length, bundle.scenarios.length + bundle.experiments.length)
    assert.ok(!html.includes("<form"))
  } finally { globalThis.fetch = previous }
  const view = readFileSync(new URL("../src/features/governance-monitor/ExperimentDetails.tsx", import.meta.url), "utf8")
  assert.doesNotMatch(view, /fetch\(|EventSource\(|commerce-api|commerce-runtime|auth-client/)
})
test("experiment strings are rendered as text and cannot inject markup", () => {
  const e = structuredClone(bundle.experiments[0]); e.scope = '<script>alert("unit")</script>'
  const html = render(e)
  assert.ok(html.includes("&lt;script&gt;"))
  assert.ok(!html.includes("<script>"))
})
test("demo snapshot admits the dry-run experiment and refuses a provider run", () => {
  assert.equal(parseDemoSnapshot(snapshot(raw)).bundle.experiments.length, 1)
  assert.throws(() => parseDemoSnapshot(snapshot(asProvider(raw))), /Experimentos fuera de la fixture autorizada/)
})
