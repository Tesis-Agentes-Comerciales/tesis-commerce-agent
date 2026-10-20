export type PhaseEvidence = {
  outcome: "allow" | "deny" | null
  decisionId: string
  evidenceId: string
  digest: string
  reason: string | null
}

export type Scenario = {
  id: string
  label: string
  mode: "HP" | "HNP"
  executionKind: "replay" | "scripted offline"
  scope: string
  intent: string | null
  constraints: string | null
  proposal: string | null
  proposedAction: string | null
  runId: string | null
  operationId: string | null
  requestId: string | null
  shadow: PhaseEvidence | null
  enforce: PhaseEvidence | null
  validator: { verdict: "accept" | "reject" | "abstain"; adapterId: string; checks: { id: string; result: "pass" | "fail" | "unresolved" }[] } | null
  backend: { code: string; failures: string[]; executedPolicyIds: string[] | null }
  execution: "not_started" | "blocked" | "started" | "unknown"
  creation: "confirmed" | "rejected" | "uncertain" | "not_applicable"
  recovery: "confirmed" | "not_found" | "uncertain" | "not_applicable"
  commercialPosts: number | null
  effectOccurred: boolean | null
  sourceKind: "historical_http_postgresql" | "scripted_offline" | "unavailable"
  budgetLimitMinor: number | null
  totalMinor: number | null
  currency: string | null
  notes: string[]
  evidence: { artifact: string; sha256: string; pointer: string; codeCommit: string; recordDigest: string | null }[]
}

export const BUSINESS_CHECKS = ["selection", "merchant", "budget", "offer", "consent", "validity", "revocation", "usage", "constraints", "scope"] as const
const CHECK_IDS = [...BUSINESS_CHECKS, "binding"] as const
const CASE_KINDS = ["admissible", "changed_after_review", "invalid", "not_reviewable"] as const
const ADMISSION_CODES = ["SIMULATED_ONLY", "BUYER_INVALID", "BUYER_UNAVAILABLE", "CONTEXT_UNAVAILABLE", "VALIDATOR_UNAVAILABLE", "VALIDATOR_INVALID", "VALIDATOR_REJECTED", "BACKEND_DENIED", "BACKEND_UNAVAILABLE", "CONTEXT_CHANGED", "EVIDENCE_UNAVAILABLE", "SINK_UNAVAILABLE", "RECEIPT_UNAVAILABLE"] as const
const VERDICTS = ["accept", "reject", "abstain"] as const
const VALIDATOR_OUTCOMES = [...VERDICTS, "invalid", "timeout", "unavailable", "not_called"] as const
const REVIEWED_OUTCOMES = [...VERDICTS, "invalid", "timeout"] as const
const CALL_STATUSES = ["ok", "refusal", "incomplete", "schema_invalid", "inconsistent", "timeout", "transport_error"] as const
// HNP-02 kinds, each bound to one transport, validator actor transport and cost basis.
const EXPERIMENT_KINDS = {
  "llm validator dry-run": { transport: "fake", actor: "fake", basis: "fake_transport_estimate" },
  "llm validator provider": { transport: "provider", actor: "openai-responses", basis: "provider_reported_usage" },
} as const

export type CaseKind = (typeof CASE_KINDS)[number]
export type Wilson = { k: number; n: number; rate: number | null; low: number | null; high: number | null }
export type Latency = { n: number; medianMs: number; p95Ms: number; maxMs: number } | null
export type ArmMetrics = { episodes: number; scoredEpisodes: number; infrastructureErrors: number; caseMix: Record<CaseKind, number>; simulatedEffects: number; escapes: Wilson; falseBlocks: Wilson; backendBlocksOnInvalid: number; episodeLatencyMs: Latency }
export type PairTable = { pairs: number; bothAdmitted: number; bothBlocked: number; onlyBBlocked: number; onlyABlocked: number; mcnemarExactP: number }
export type ExperimentActor = {
  role: "buyer" | "validator"; kind: "scripted" | "llm"; adapterId: string; transport: "none" | "fake" | "openai-responses"
  model: string | null; promptVersion: string | null; promptSha256: string | null; reasoningEffort: "minimal" | "low" | "medium" | "high" | null
  temperature: number | null; maxOutputTokens: number | null; timeoutSeconds: number | null
}
export type ExperimentEpisode = {
  episodeId: string; admissionCode: (typeof ADMISSION_CODES)[number]; effect: boolean; outcomeClass: "experimental" | "infrastructure"; backendFailures: (typeof CHECK_IDS)[number][]; latencyMs: number
  validator: {
    outcome: (typeof VALIDATOR_OUTCOMES)[number]; callStatus: (typeof CALL_STATUSES)[number] | null; errorKind: string | null; declaredDecision: (typeof VERDICTS)[number] | null
    // Only the checks the answer did not pass; the rest of the ten were declared pass. null: no parsed answer.
    nonPassChecks: Partial<Record<(typeof BUSINESS_CHECKS)[number], "fail" | "unresolved">> | null
    reasonCount: number | null; latencyMs: number | null; inputTokens: number | null; outputTokens: number | null; costUsd: number | null
  } | null
}
export type ExperimentCase = {
  caseId: string; caseKind: CaseKind
  arms: Record<"A" | "B", { episodes: number; infrastructure: number; effects: number }>
  validatorOutcomes: Partial<Record<(typeof VALIDATOR_OUTCOMES)[number], number>>
  sample: { pairId: string; A: ExperimentEpisode; B: ExperimentEpisode }
}
/** One HNP-02 run over a fictitious sink: arm A (scripted buyer + backend) against arm B (+ LLM validator). Never an operation. */
export type Experiment = {
  id: string; label: string; mode: "HNP"; executionKind: keyof typeof EXPERIMENT_KINDS; transport: "fake" | "provider"; scope: string; phase: string
  sink: "fictitious_in_memory"; operationalEffects: false; commercialPosts: 0; createdAt: string; seed: number; repetitions: number; excludedCases: string[]
  actors: ExperimentActor[]
  arms: Record<"A" | "B", ArmMetrics>
  validator: { reviewed: number; outcomes: Record<(typeof REVIEWED_OUTCOMES)[number], number>; captures: Wilson; capturesWithExpectedChecks: Wilson; uniqueCaptures: number; missesContainedByBackend: number; falseRejections: Wilson; abstentions: Wilson; invalidOutputs: Wilson; timeouts: Wilson }
  comparison: { excludedPairs: { incomplete: number; infrastructure: number }; all: PairTable; admissible: PairTable; unsafe: PairTable }
  cost: {
    basis: "fake_transport_estimate" | "provider_reported_usage"; llmCalls: number; providerCalls: number; fakeCalls: number; callsByStatus: Record<(typeof CALL_STATUSES)[number], number>
    callsWithUnknownUsage: number; pendingCalls: number; tokens: { input: number; output: number; cachedInput: number; reasoning: number }; costUsd: number; callLatencyMs: Latency
    estimateUsd: { min: string; max: string }; pricesUsdPerMtok: { input: string; output: string }
  }
  deviations: { harness: number; infrastructure: number; validator: number }
  sampleRule: string
  cases: ExperimentCase[]
  implementationSha256: string
  corpusSha256: { cases: string; oracle: string }
  notes: string[]
  evidence: { artifact: string; sha256: string; pointer: string }[]
}

export type EvidenceBundle = {
  schema: "governance-monitor/3"
  status: "PARTIAL"
  sourcePins: Record<string, string>
  hnpLedgerEntriesVerified: number
  scenarios: Scenario[]
  experiments: Experiment[]
}

function object(value: unknown, keys?: string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw Error("Objeto de evidencia inválido")
  const record = value as Record<string, unknown>
  if (keys && (Object.keys(record).length !== keys.length || keys.some((key) => !Object.hasOwn(record, key)))) throw Error("Campos de evidencia desconocidos o ausentes")
  return record
}
function text(value: unknown): string {
  if (typeof value !== "string" || value.length === 0 || value.length > 5000) throw Error("Texto de evidencia inválido")
  return value
}
function nullable(value: unknown): string | null { return value === null ? null : text(value) }
function array(value: unknown): unknown[] {
  if (!Array.isArray(value) || value.length > 200) throw Error("Lista de evidencia inválida")
  return value
}
function choice<T extends string>(value: unknown, choices: readonly T[]): T {
  if (!choices.includes(value as T)) throw Error("Estado de evidencia desconocido")
  return value as T
}
function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) throw Error("Contador inválido")
  return value
}
function sha(value: unknown, length = 64): string {
  const result = text(value)
  if (!new RegExp(`^[a-f0-9]{${length}}$`).test(result)) throw Error("Huella inválida")
  return result
}
function measure(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw Error("Medida inválida")
  return value
}
function ratio(value: unknown): number {
  const result = measure(value)
  if (result > 1) throw Error("Proporción inválida")
  return result
}
function bool(value: unknown): boolean {
  if (typeof value !== "boolean") throw Error("Observación booleana inválida")
  return value
}
function matches(value: unknown, pattern: RegExp): string {
  const result = text(value)
  if (!pattern.test(result)) throw Error("Identificador inválido")
  return result
}
function maybe<T>(value: unknown, read: (value: unknown) => T): T | null { return value === null ? null : read(value) }
/** Every key present, each a count. */
function counts<K extends string>(value: unknown, keys: readonly K[]): Record<K, number> {
  const record = object(value, [...keys])
  return Object.fromEntries(keys.map((key) => [key, integer(record[key])])) as Record<K, number>
}
/** Only outcomes that occurred: known keys, positive counts. */
function occurred<K extends string>(value: unknown, keys: readonly K[]): Partial<Record<K, number>> {
  return Object.fromEntries(Object.entries(object(value)).map(([key, n]) => {
    if (integer(n) === 0) throw Error("Conteo vacío")
    return [choice(key, keys), n]
  })) as Partial<Record<K, number>>
}
function phase(value: unknown): PhaseEvidence | null {
  if (value === null) return null
  const p = object(value, ["outcome", "decisionId", "evidenceId", "digest", "reason"])
  return { outcome: p.outcome === null ? null : choice<"allow" | "deny">(p.outcome, ["allow", "deny"]), decisionId: text(p.decisionId), evidenceId: text(p.evidenceId), digest: text(p.digest), reason: nullable(p.reason) }
}

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/
const MODEL = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/
const PROMPT = /^[a-z]+-v[0-9]+$/
const ERROR_KIND = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
const DECIMAL = /^[0-9]{1,12}(\.[0-9]{1,12})?$/
const TIMESTAMP = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})$/
function wilson(value: unknown): Wilson {
  const w = object(value, ["k", "n", "rate", "low", "high"])
  return { k: integer(w.k), n: integer(w.n), rate: maybe(w.rate, ratio), low: maybe(w.low, ratio), high: maybe(w.high, ratio) }
}
function latency(value: unknown): Latency {
  if (value === null) return null
  const l = object(value, ["n", "medianMs", "p95Ms", "maxMs"])
  return { n: integer(l.n), medianMs: measure(l.medianMs), p95Ms: measure(l.p95Ms), maxMs: measure(l.maxMs) }
}
function arm(value: unknown): ArmMetrics {
  const a = object(value, ["episodes", "scoredEpisodes", "infrastructureErrors", "caseMix", "simulatedEffects", "escapes", "falseBlocks", "backendBlocksOnInvalid", "episodeLatencyMs"])
  return { episodes: integer(a.episodes), scoredEpisodes: integer(a.scoredEpisodes), infrastructureErrors: integer(a.infrastructureErrors), caseMix: counts(a.caseMix, CASE_KINDS), simulatedEffects: integer(a.simulatedEffects), escapes: wilson(a.escapes), falseBlocks: wilson(a.falseBlocks), backendBlocksOnInvalid: integer(a.backendBlocksOnInvalid), episodeLatencyMs: latency(a.episodeLatencyMs) }
}
function pairs(value: unknown): PairTable {
  const p = object(value, ["pairs", "bothAdmitted", "bothBlocked", "onlyBBlocked", "onlyABlocked", "mcnemarExactP"])
  return { pairs: integer(p.pairs), bothAdmitted: integer(p.bothAdmitted), bothBlocked: integer(p.bothBlocked), onlyBBlocked: integer(p.onlyBBlocked), onlyABlocked: integer(p.onlyABlocked), mcnemarExactP: ratio(p.mcnemarExactP) }
}
function actor(value: unknown): ExperimentActor {
  const a = object(value, ["role", "kind", "adapterId", "transport", "model", "promptVersion", "promptSha256", "reasoningEffort", "temperature", "maxOutputTokens", "timeoutSeconds"])
  const temperature = maybe(a.temperature, measure)
  if (temperature !== null && temperature > 2) throw Error("Temperatura inválida")
  return {
    role: choice(a.role, ["buyer", "validator"]), kind: choice(a.kind, ["scripted", "llm"]), adapterId: matches(a.adapterId, IDENTIFIER), transport: choice(a.transport, ["none", "fake", "openai-responses"]),
    model: maybe(a.model, (x) => matches(x, MODEL)), promptVersion: maybe(a.promptVersion, (x) => matches(x, PROMPT)), promptSha256: maybe(a.promptSha256, (x) => sha(x)),
    reasoningEffort: maybe(a.reasoningEffort, (x) => choice(x, ["minimal", "low", "medium", "high"])), temperature, maxOutputTokens: maybe(a.maxOutputTokens, integer), timeoutSeconds: maybe(a.timeoutSeconds, measure),
  }
}
function episode(value: unknown): ExperimentEpisode {
  const e = object(value, ["episodeId", "admissionCode", "effect", "outcomeClass", "backendFailures", "latencyMs", "validator"])
  let validator: ExperimentEpisode["validator"] = null
  if (e.validator !== null) {
    const v = object(e.validator, ["outcome", "callStatus", "errorKind", "declaredDecision", "nonPassChecks", "reasonCount", "latencyMs", "inputTokens", "outputTokens", "costUsd"])
    validator = {
      outcome: choice(v.outcome, VALIDATOR_OUTCOMES), callStatus: maybe(v.callStatus, (x) => choice(x, CALL_STATUSES)), errorKind: maybe(v.errorKind, (x) => matches(x, ERROR_KIND)), declaredDecision: maybe(v.declaredDecision, (x) => choice(x, VERDICTS)),
      nonPassChecks: maybe(v.nonPassChecks, (x) => Object.fromEntries(Object.entries(object(x)).map(([id, result]) => [choice(id, BUSINESS_CHECKS), choice(result, ["fail", "unresolved"])]))),
      reasonCount: maybe(v.reasonCount, integer), latencyMs: maybe(v.latencyMs, measure), inputTokens: maybe(v.inputTokens, integer), outputTokens: maybe(v.outputTokens, integer), costUsd: maybe(v.costUsd, measure),
    }
  }
  const backendFailures = array(e.backendFailures).map((x) => choice(x, CHECK_IDS))
  if (new Set(backendFailures).size !== backendFailures.length) throw Error("Control backend repetido")
  return { episodeId: matches(e.episodeId, IDENTIFIER), admissionCode: choice(e.admissionCode, ADMISSION_CODES), effect: bool(e.effect), outcomeClass: choice(e.outcomeClass, ["experimental", "infrastructure"]), backendFailures, latencyMs: measure(e.latencyMs), validator }
}
function experimentCase(value: unknown): ExperimentCase {
  const c = object(value, ["caseId", "caseKind", "arms", "validatorOutcomes", "sample"])
  const arms = object(c.arms, ["A", "B"]), sample = object(c.sample, ["pairId", "A", "B"])
  const tally = (x: unknown) => counts(x, ["episodes", "infrastructure", "effects"])
  return { caseId: matches(c.caseId, IDENTIFIER), caseKind: choice(c.caseKind, CASE_KINDS), arms: { A: tally(arms.A), B: tally(arms.B) }, validatorOutcomes: occurred(c.validatorOutcomes, VALIDATOR_OUTCOMES), sample: { pairId: matches(sample.pairId, IDENTIFIER), A: episode(sample.A), B: episode(sample.B) } }
}

/** HNP-02 runs. The scripted-only HNP rule for scenarios is relaxed here, and only for the LLM-validator kinds:
 * arm A has no validator and an invalid or missing answer leaves none, so validator presence is checked per episode. */
function parseExperiment(value: unknown, pins: Record<string, string>): Experiment {
  const e = object(value, ["id", "label", "mode", "executionKind", "transport", "scope", "phase", "sink", "operationalEffects", "commercialPosts", "createdAt", "seed", "repetitions", "excludedCases", "actors", "arms", "validator", "comparison", "cost", "deviations", "sampleRule", "cases", "implementationSha256", "corpusSha256", "notes", "evidence"])
  if (e.mode !== "HNP" || e.sink !== "fictitious_in_memory" || e.operationalEffects !== false || e.commercialPosts !== 0) throw Error("Experimento fuera de un sink ficticio")
  const arms = object(e.arms, ["A", "B"]), corpus = object(e.corpusSha256, ["cases", "oracle"])
  const v = object(e.validator, ["reviewed", "outcomes", "captures", "capturesWithExpectedChecks", "uniqueCaptures", "missesContainedByBackend", "falseRejections", "abstentions", "invalidOutputs", "timeouts"])
  const c = object(e.comparison, ["excludedPairs", "all", "admissible", "unsafe"]), excluded = object(c.excludedPairs, ["incomplete", "infrastructure"])
  const cost = object(e.cost, ["basis", "llmCalls", "providerCalls", "fakeCalls", "callsByStatus", "callsWithUnknownUsage", "pendingCalls", "tokens", "costUsd", "callLatencyMs", "estimateUsd", "pricesUsdPerMtok"])
  const estimate = object(cost.estimateUsd, ["min", "max"]), prices = object(cost.pricesUsdPerMtok, ["input", "output"])
  const repetitions = integer(e.repetitions)
  if (repetitions < 1 || repetitions > 100) throw Error("Repeticiones inválidas")
  const result: Experiment = {
    id: matches(e.id, IDENTIFIER), label: text(e.label), mode: "HNP", executionKind: choice(e.executionKind, Object.keys(EXPERIMENT_KINDS) as (keyof typeof EXPERIMENT_KINDS)[]), transport: choice(e.transport, ["fake", "provider"]),
    scope: text(e.scope), phase: text(e.phase), sink: "fictitious_in_memory", operationalEffects: false, commercialPosts: 0, createdAt: matches(e.createdAt, TIMESTAMP), seed: integer(e.seed), repetitions,
    excludedCases: array(e.excludedCases).map((x) => matches(x, IDENTIFIER)), actors: array(e.actors).map(actor), arms: { A: arm(arms.A), B: arm(arms.B) },
    validator: {
      reviewed: integer(v.reviewed), outcomes: counts(v.outcomes, REVIEWED_OUTCOMES), captures: wilson(v.captures), capturesWithExpectedChecks: wilson(v.capturesWithExpectedChecks), uniqueCaptures: integer(v.uniqueCaptures),
      missesContainedByBackend: integer(v.missesContainedByBackend), falseRejections: wilson(v.falseRejections), abstentions: wilson(v.abstentions), invalidOutputs: wilson(v.invalidOutputs), timeouts: wilson(v.timeouts),
    },
    comparison: { excludedPairs: { incomplete: integer(excluded.incomplete), infrastructure: integer(excluded.infrastructure) }, all: pairs(c.all), admissible: pairs(c.admissible), unsafe: pairs(c.unsafe) },
    cost: {
      basis: choice(cost.basis, ["fake_transport_estimate", "provider_reported_usage"]), llmCalls: integer(cost.llmCalls), providerCalls: integer(cost.providerCalls), fakeCalls: integer(cost.fakeCalls), callsByStatus: counts(cost.callsByStatus, CALL_STATUSES),
      callsWithUnknownUsage: integer(cost.callsWithUnknownUsage), pendingCalls: integer(cost.pendingCalls), tokens: counts(cost.tokens, ["input", "output", "cachedInput", "reasoning"]), costUsd: measure(cost.costUsd), callLatencyMs: latency(cost.callLatencyMs),
      estimateUsd: { min: matches(estimate.min, DECIMAL), max: matches(estimate.max, DECIMAL) }, pricesUsdPerMtok: { input: matches(prices.input, DECIMAL), output: matches(prices.output, DECIMAL) },
    },
    deviations: counts(e.deviations, ["harness", "infrastructure", "validator"]), sampleRule: text(e.sampleRule), cases: array(e.cases).map(experimentCase),
    implementationSha256: sha(e.implementationSha256), corpusSha256: { cases: sha(corpus.cases), oracle: sha(corpus.oracle) }, notes: array(e.notes).map(text),
    evidence: array(e.evidence).map((item) => {
      const x = object(item, ["artifact", "sha256", "pointer"])
      const artifact = text(x.artifact), hash = sha(x.sha256)
      if (pins[artifact] !== hash) throw Error("Referencia de evidencia no vinculada")
      return { artifact, sha256: hash, pointer: text(x.pointer) }
    }),
  }
  if (!result.evidence.length) throw Error("Experimento sin fuente")
  const issue = experimentIssue(result)
  if (issue) throw Error(`Evidencia inconsistente: ${issue}`)
  return result
}

/** Projection only: never grants authority, reconstructs missing facts, or sends commands. */
export function parseEvidence(value: unknown): EvidenceBundle {
  const bundle = object(value, ["schema", "status", "sourcePins", "hnpLedgerEntriesVerified", "scenarios", "experiments"])
  choice(bundle.schema, ["governance-monitor/3"])
  choice(bundle.status, ["PARTIAL"])
  const pins = Object.fromEntries(Object.entries(object(bundle.sourcePins)).map(([key, val]) => [text(key), sha(val)]))
  const scenarios = array(bundle.scenarios).map((value): Scenario => {
    const s = object(value, ["id", "label", "mode", "executionKind", "scope", "intent", "constraints", "proposal", "proposedAction", "runId", "operationId", "requestId", "shadow", "enforce", "validator", "backend", "execution", "creation", "recovery", "sourceKind", "budgetLimitMinor", "commercialPosts", "effectOccurred", "totalMinor", "currency", "notes", "evidence"])
    const backend = object(s.backend, ["code", "failures", "executedPolicyIds"])
    let validator: Scenario["validator"] = null
    if (s.validator !== null) {
      const v = object(s.validator, ["verdict", "adapterId", "checks"])
      validator = { verdict: choice(v.verdict, ["accept", "reject", "abstain"]), adapterId: text(v.adapterId), checks: array(v.checks).map((item) => {
        const c = object(item, ["id", "result"])
        return { id: text(c.id), result: choice(c.result, ["pass", "fail", "unresolved"]) }
      }) }
    }
    if (s.effectOccurred !== null && typeof s.effectOccurred !== "boolean") throw Error("Observación de efecto inválida")
    const result: Scenario = {
      id: text(s.id), label: text(s.label), mode: choice(s.mode, ["HP", "HNP"]), executionKind: choice(s.executionKind, ["replay", "scripted offline"]),
      scope: text(s.scope), intent: nullable(s.intent), constraints: nullable(s.constraints), proposal: nullable(s.proposal), proposedAction: nullable(s.proposedAction),
      runId: nullable(s.runId), operationId: nullable(s.operationId), requestId: nullable(s.requestId), shadow: phase(s.shadow), enforce: phase(s.enforce), validator,
      backend: { code: text(backend.code), failures: array(backend.failures).map(text), executedPolicyIds: backend.executedPolicyIds === null ? null : array(backend.executedPolicyIds).map(text) },
      execution: choice(s.execution, ["not_started", "blocked", "started", "unknown"]), creation: choice(s.creation, ["confirmed", "rejected", "uncertain", "not_applicable"]), recovery: choice(s.recovery, ["confirmed", "not_found", "uncertain", "not_applicable"]),
      sourceKind: choice(s.sourceKind, ["historical_http_postgresql", "scripted_offline", "unavailable"]), budgetLimitMinor: s.budgetLimitMinor === null ? null : integer(s.budgetLimitMinor),
      commercialPosts: s.commercialPosts === null ? null : integer(s.commercialPosts), effectOccurred: s.effectOccurred, totalMinor: s.totalMinor === null ? null : integer(s.totalMinor), currency: nullable(s.currency), notes: array(s.notes).map(text),
      evidence: array(s.evidence).map((item) => {
        const e = object(item, ["artifact", "sha256", "pointer", "codeCommit", "recordDigest"])
        const artifact = text(e.artifact), hash = sha(e.sha256)
        if (pins[artifact] !== hash) throw Error("Referencia de evidencia no vinculada")
        return { artifact, sha256: hash, pointer: text(e.pointer), codeCommit: sha(e.codeCommit, 40), recordDigest: nullable(e.recordDigest) }
      }),
    }
    if (!result.evidence.length) throw Error("Escenario sin fuente")
    const issue = scenarioIssue(result)
    if (issue) throw Error(`Evidencia inconsistente: ${issue}`)
    if (result.mode === "HP" && (result.executionKind !== "replay" || result.sourceKind === "scripted_offline" || result.validator !== null)) throw Error("Fuente HP mal etiquetada")
    // A scenario is one scripted HNP-01 record; LLM-validator runs are accepted only as experiments.
    if (result.mode === "HNP" && (result.validator === null || result.sourceKind !== "scripted_offline" || result.executionKind !== "scripted offline" || result.shadow !== null || result.enforce !== null || result.commercialPosts !== 0 || result.backend.executedPolicyIds?.length !== 0)) throw Error("Capacidad HNP no acreditada")
    return result
  })
  const experiments = array(bundle.experiments).map((item) => parseExperiment(item, pins))
  const ids = [...scenarios, ...experiments].map((x) => x.id)
  if (!scenarios.length || new Set(ids).size !== ids.length) throw Error("Escenarios vacíos o duplicados")
  return { schema: "governance-monitor/3", status: "PARTIAL", sourcePins: pins, hnpLedgerEntriesVerified: integer(bundle.hnpLedgerEntriesVerified), scenarios, experiments }
}

export const unavailable = (value: string | null) => value ?? "No disponible en la fuente"

/** Cross-field invariants. A validator opinion is never a backend permission. */
export function scenarioIssue(s: Scenario): string | null {
  if (!["not_started", "blocked", "started", "unknown"].includes(s.execution)
    || !["confirmed", "rejected", "uncertain", "not_applicable"].includes(s.creation)
    || !["confirmed", "not_found", "uncertain", "not_applicable"].includes(s.recovery)) return "estados ausentes o desconocidos"
  const denied = s.enforce?.outcome === "deny" || s.backend.code === "BACKEND_DENIED"
  if (denied && s.execution !== "blocked") return "denegación backend con ejecución no bloqueada"
  if (s.execution === "blocked" || s.execution === "not_started") {
    if (s.commercialPosts !== 0 || s.effectOccurred !== false) return "sin inicio, pero POST/efectos no ausentes"
    if (s.creation !== "not_applicable" || s.recovery !== "not_applicable") return "sin envío, pero creación o recuperación aplicables"
  }
  if (s.mode === "HP" && s.execution === "started" && s.commercialPosts === 0) return "inicio con cero envíos observados"
  if (s.mode === "HP" && s.execution === "started" && s.creation === "not_applicable") return "envío HP con creación no aplicable"
  if (s.commercialPosts !== null && s.commercialPosts > 0 && s.execution !== "started") return "POST observado sin inicio"
  if ((s.creation === "confirmed" || s.creation === "rejected" || s.recovery === "confirmed" || s.recovery === "not_found") && s.execution !== "started") return "resultado concreto sin inicio acreditado"
  if (s.creation === "rejected" && (s.effectOccurred === true || s.recovery === "confirmed")) return "creación rechazada con efecto o recuperación found"
  if (s.creation === "confirmed" && s.recovery === "not_found") return "creación confirmada frente a recuperación not_found"
  if ((s.creation === "confirmed" || s.recovery === "confirmed") && s.effectOccurred !== true) return "confirmación sin efecto observado"
  if (s.effectOccurred === true && (s.execution !== "started" || (s.creation !== "confirmed" && s.recovery !== "confirmed"))) return "efecto sin creación o recuperación confirmada"
  if (s.recovery === "not_found" && s.effectOccurred === true) return "not_found con efecto confirmado"
  if (s.mode === "HNP" && s.validator === null) return "HNP sin evaluación del validador"
  if (/permitid/i.test(s.label) && s.enforce?.outcome !== "allow") return "etiqueta de permiso sin decisión allow aplicada"
  if (s.mode === "HP" && s.budgetLimitMinor !== null) return "límite HNP atribuido a HP"
  if (s.mode === "HNP" && s.totalMinor !== null && s.budgetLimitMinor !== null
    && s.totalMinor > s.budgetLimitMinor && (s.execution !== "blocked" || !s.backend.failures.includes("budget"))) return "presupuesto excedido sin bloqueo backend documentado"
  return null
}

const wilsonIssue = (w: Wilson) => w.k > w.n || (w.n === 0
  ? w.rate !== null || w.low !== null || w.high !== null
  : w.rate === null || w.low === null || w.high === null || Math.abs(w.rate - w.k / w.n) > 1e-9 || w.low > w.rate || w.rate > w.high)
const pairIssue = (p: PairTable) => p.pairs !== p.bothAdmitted + p.bothBlocked + p.onlyBBlocked + p.onlyABlocked || p.onlyABlocked !== 0

/** Cross-field invariants of an experiment. The validator can only block: arm B never admits what arm A blocked. */
export function experimentIssue(e: Experiment): string | null {
  const kind = Object.hasOwn(EXPERIMENT_KINDS, e.executionKind) ? EXPERIMENT_KINDS[e.executionKind] : null
  if (!kind || kind.transport !== e.transport) return "tipo de ejecución y transporte no coinciden"
  if (e.mode !== "HNP" || e.sink !== "fictitious_in_memory" || e.operationalEffects !== false || e.commercialPosts !== 0) return "experimento fuera de un sink ficticio"
  const [buyer, validator] = e.actors
  if (e.actors.length !== 2 || buyer.role !== "buyer" || buyer.kind !== "scripted" || buyer.transport !== "none" || buyer.model !== null
    || validator.role !== "validator" || validator.kind !== "llm" || validator.model === null || validator.promptVersion === null || validator.promptSha256 === null || validator.transport !== kind.actor) return "actores incompatibles con el transporte declarado"
  const cost = e.cost
  if (cost.basis !== kind.basis || cost.llmCalls !== cost.providerCalls + cost.fakeCalls || (e.transport === "fake" ? cost.providerCalls : cost.fakeCalls) !== 0) return "costo incompatible con el transporte declarado"
  const v = e.validator
  if ([e.arms.A.escapes, e.arms.A.falseBlocks, e.arms.B.escapes, e.arms.B.falseBlocks, v.captures, v.capturesWithExpectedChecks, v.falseRejections, v.abstentions, v.invalidOutputs, v.timeouts].some(wilsonIssue)) return "intervalo de Wilson inconsistente"
  if ([e.comparison.all, e.comparison.admissible, e.comparison.unsafe].some(pairIssue)) return "pares A/B inconsistentes o B admite lo que A bloqueó"
  if (e.arms.B.simulatedEffects > e.arms.A.simulatedEffects) return "brazo B con más efectos simulados que A"
  if (!e.cases.length || new Set(e.cases.map((c) => c.caseId)).size !== e.cases.length) return "casos vacíos o duplicados"
  for (const c of e.cases) {
    const { A, B } = c.sample
    const reviewed = Object.values(c.validatorOutcomes).reduce((sum, n) => sum + n, 0)
    if (c.arms.B.effects > c.arms.A.effects || reviewed !== c.arms.B.episodes || [c.arms.A, c.arms.B].some((x) => x.effects > x.episodes || x.infrastructure > x.episodes)) return `caso ${c.caseId} con conteos inconsistentes`
    if (A.episodeId !== `${c.sample.pairId}:A` || B.episodeId !== `${c.sample.pairId}:B` || A.validator !== null || B.validator === null) return `muestra ${c.caseId} sin par A/B válido`
    if ((B.effect && !A.effect) || (B.effect && B.validator.outcome !== "accept") || [A, B].some((x) => x.effect && (x.admissionCode !== "SIMULATED_ONLY" || x.backendFailures.length > 0))) return `muestra ${c.caseId} con efecto sin admisión`
  }
  return null
}

// Mandatory copy of every experiment view. Only an explicit provider transport drops the simulated-figures notice.
export const EXPERIMENT_NOTICE = "Experimento en entorno ficticio, sink ficticio, no es operación real."
export const SIMULATED_TRANSPORT_NOTICE = "Cifras de transporte simulado, no miden ningún modelo."
export const experimentNotices = (e: { transport: string }) => e.transport === "provider" ? [EXPERIMENT_NOTICE] : [EXPERIMENT_NOTICE, SIMULATED_TRANSPORT_NOTICE]

export const executionText = { not_started: "No iniciada", blocked: "Bloqueada antes del envío", started: "Iniciada (registro histórico)", unknown: "Desconocida" } as const
export const creationText = { confirmed: "Confirmada históricamente", rejected: "Rechazada", uncertain: "Incierta", not_applicable: "No aplicable" } as const
export const recoveryText = { confirmed: "Confirmada · found", not_found: "not_found · no autoriza reintento", uncertain: "Incierta", not_applicable: "No aplicable" } as const
const sourceKindText = { historical_http_postgresql: "HTTP/PostgreSQL real histórico · replay", scripted_offline: "Scripted offline", unavailable: null } as const

type ValidatorCopy = { title: string; note: string; checks: string; falsePass: string }
type ExecutionKind = Scenario["executionKind"] | Experiment["executionKind"]
/** Copy owned by each execution kind. LLM kinds never inherit scripted wording, and dry-run never reads as a provider run. */
const executionKindText: Record<ExecutionKind, { llm: string | null; validator: ValidatorCopy | null }> = {
  replay: { llm: null, validator: null },
  "scripted offline": { llm: "sin LLM", validator: {
    title: "Validador HNP · scripted offline", note: "Respuesta programada; cero LLM reales. El veredicto no concede autoridad.",
    checks: "Declaraciones del validador guionado, no controles backend aprobados:", falsePass: "El pass es una declaración errónea del guion; no evidencia de autorización ni control superado.",
  } },
  "llm validator dry-run": { llm: "validador LLM sobre transporte simulado (FakeTransport) · sin proveedor", validator: {
    title: "Validador HNP-02 · LLM en dry-run", note: "Respuestas pseudoaleatorias del transporte simulado: no proceden de ningún modelo. El veredicto no concede autoridad.",
    checks: "Declaraciones simuladas del validador, no controles backend aprobados:", falsePass: "El pass procede del transporte simulado; no evidencia autorización, control superado ni capacidad de un modelo.",
  } },
  "llm validator provider": { llm: "validador LLM con proveedor real", validator: {
    title: "Validador HNP-02 · LLM con proveedor", note: "Respuesta de un modelo real en una corrida experimental sobre un sink ficticio. El veredicto no concede autoridad.",
    checks: "Declaraciones del modelo validador, no controles backend aprobados:", falsePass: "El pass es una declaración errónea del modelo; no evidencia autorización ni control superado.",
  } },
}
// Only reachable when a kind bypassed the parser: states the facts without claiming how the verdict was produced.
const neutralValidator: ValidatorCopy = {
  title: "Validador HNP", note: "Tipo de ejecución sin texto revisado. El veredicto no concede autoridad.",
  checks: "Declaraciones del validador, no controles backend aprobados:", falsePass: "El pass declarado por el validador no evidencia autorización ni control superado.",
}
const kindCopy = (s: { executionKind: string }) => Object.hasOwn(executionKindText, s.executionKind) ? executionKindText[s.executionKind as ExecutionKind] : { llm: null, validator: null }
export const validatorText = (s: { executionKind: string }): ValidatorCopy => kindCopy(s).validator ?? neutralValidator
export const experimentKindText = (e: Experiment): string | null => kindCopy(e).llm
export function sourceText(s: Scenario): string | null {
  const source = Object.hasOwn(sourceKindText, s.sourceKind) ? sourceKindText[s.sourceKind] : null
  const llm = kindCopy(s).llm
  return source && llm ? `${source} · ${llm}` : source
}

export function scenarioBadge(s: Scenario): string {
  if (scenarioIssue(s)) return "Evidencia inconsistente"
  if (s.execution === "blocked") return "Operación bloqueada · sin creación"
  if (s.execution === "not_started") return "Operación no iniciada"
  if (s.recovery === "uncertain") return "Recuperación incierta"
  if (s.recovery === "confirmed") return "Creación histórica confirmada por recuperación"
  if (s.creation === "confirmed") return "Creación histórica confirmada"
  if (s.creation === "rejected") return "Creación rechazada"
  if (s.recovery === "not_found") return "Recuperación not_found · sin reintento"
  if (s.execution === "unknown") return "Ejecución desconocida · creación no confirmada"
  return "Creación incierta"
}
