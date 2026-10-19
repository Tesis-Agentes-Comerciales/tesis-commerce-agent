import { parseEvidence, type EvidenceBundle } from "./adapter.ts"

export type PhaseEvidence = {
  outcome: "allow" | "deny" | null
  decisionId: string
  evidenceId: string
  digest: string
  reason: string | null
}

export type LegacyScenario = {
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
  execution: "recorded" | "blocked" | "not_started"
  initialResult: "confirmed" | "uncertain" | "not_available"
  result: "confirmed" | "uncertain" | "not_available"
  commercialPosts: number
  effectOccurred: boolean
  totalMinor: number | null
  currency: string | null
  notes: string[]
  evidence: { artifact: string; sha256: string; pointer: string; codeCommit: string; recordDigest: string | null }[]
}

export type LegacyBundle = {
  schema: "governance-monitor/1"
  status: "PARTIAL"
  sourcePins: Record<string, string>
  hnpLedgerEntriesVerified: number
  scenarios: LegacyScenario[]
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
function phase(value: unknown): PhaseEvidence | null {
  if (value === null) return null
  const p = object(value, ["outcome", "decisionId", "evidenceId", "digest", "reason"])
  return { outcome: p.outcome === null ? null : choice<"allow" | "deny">(p.outcome, ["allow", "deny"]), decisionId: text(p.decisionId), evidenceId: text(p.evidenceId), digest: text(p.digest), reason: nullable(p.reason) }
}

/** Projection only: never grants authority, reconstructs missing facts, or sends commands. */
export function parseHistoricalWire(value: unknown): LegacyBundle {
  const bundle = object(value, ["schema", "status", "sourcePins", "hnpLedgerEntriesVerified", "scenarios"])
  choice(bundle.schema, ["governance-monitor/1"])
  choice(bundle.status, ["PARTIAL"])
  const pins = Object.fromEntries(Object.entries(object(bundle.sourcePins)).map(([key, val]) => [text(key), sha(val)]))
  const scenarios = array(bundle.scenarios).map((value): LegacyScenario => {
    const s = object(value, ["id", "label", "mode", "executionKind", "scope", "intent", "constraints", "proposal", "proposedAction", "runId", "operationId", "requestId", "shadow", "enforce", "validator", "backend", "execution", "initialResult", "result", "commercialPosts", "effectOccurred", "totalMinor", "currency", "notes", "evidence"])
    const backend = object(s.backend, ["code", "failures", "executedPolicyIds"])
    let validator: LegacyScenario["validator"] = null
    if (s.validator !== null) {
      const v = object(s.validator, ["verdict", "adapterId", "checks"])
      validator = { verdict: choice(v.verdict, ["accept", "reject", "abstain"]), adapterId: text(v.adapterId), checks: array(v.checks).map((item) => {
        const c = object(item, ["id", "result"])
        return { id: text(c.id), result: choice(c.result, ["pass", "fail", "unresolved"]) }
      }) }
    }
    if (typeof s.effectOccurred !== "boolean") throw Error("Observación de efecto inválida")
    const result: LegacyScenario = {
      id: text(s.id), label: text(s.label), mode: choice(s.mode, ["HP", "HNP"]), executionKind: choice(s.executionKind, ["replay", "scripted offline"]),
      scope: text(s.scope), intent: nullable(s.intent), constraints: nullable(s.constraints), proposal: nullable(s.proposal), proposedAction: nullable(s.proposedAction),
      runId: nullable(s.runId), operationId: nullable(s.operationId), requestId: nullable(s.requestId), shadow: phase(s.shadow), enforce: phase(s.enforce), validator,
      backend: { code: text(backend.code), failures: array(backend.failures).map(text), executedPolicyIds: backend.executedPolicyIds === null ? null : array(backend.executedPolicyIds).map(text) },
      execution: choice(s.execution, ["recorded", "blocked", "not_started"]), initialResult: choice(s.initialResult, ["confirmed", "uncertain", "not_available"]), result: choice(s.result, ["confirmed", "uncertain", "not_available"]),
      commercialPosts: integer(s.commercialPosts), effectOccurred: s.effectOccurred, totalMinor: s.totalMinor === null ? null : integer(s.totalMinor), currency: nullable(s.currency), notes: array(s.notes).map(text),
      evidence: array(s.evidence).map((item) => {
        const e = object(item, ["artifact", "sha256", "pointer", "codeCommit", "recordDigest"])
        const artifact = text(e.artifact), hash = sha(e.sha256)
        if (pins[artifact] !== hash) throw Error("Referencia de evidencia no vinculada")
        return { artifact, sha256: hash, pointer: text(e.pointer), codeCommit: sha(e.codeCommit, 40), recordDigest: nullable(e.recordDigest) }
      }),
    }
    if (!result.evidence.length) throw Error("Escenario sin fuente")
    if (result.execution === "blocked" && result.effectOccurred) throw Error("Bloqueo contradictorio")
    if (result.enforce?.outcome === "deny" && result.execution !== "blocked") throw Error("Deny contradictorio")
    if (result.mode === "HP" && (result.executionKind !== "replay" || result.validator !== null)) throw Error("Fuente HP mal etiquetada")
    if (result.mode === "HNP" && (result.executionKind !== "scripted offline" || result.shadow !== null || result.enforce !== null || result.commercialPosts !== 0 || result.backend.executedPolicyIds?.length !== 0)) throw Error("Capacidad HNP no acreditada")
    return result
  })
  if (!scenarios.length || new Set(scenarios.map((s) => s.id)).size !== scenarios.length) throw Error("Escenarios vacíos o duplicados")
  return { schema: "governance-monitor/1", status: "PARTIAL", sourcePins: pins, hnpLedgerEntriesVerified: integer(bundle.hnpLedgerEntriesVerified), scenarios }
}

export const unavailable = (value: string | null) => value ?? "No disponible en la fuente"

/** The pinned v1 export conflated a confirmed denial observation with creation.
 * This compatibility reader applies only to the historical offline bundle.
 * It preserves raw JSON and never treats IDs, digests or Q as a verdict.
 */
export function parseHistoricalEvidence(value: unknown): EvidenceBundle {
  const old = parseHistoricalWire(value)
  const scenarios = old.scenarios.map((s) => {
    const { initialResult, result, execution, ...rest } = s
    if (execution === "not_started" && (initialResult === "confirmed" || result === "confirmed")) {
      throw Error("Evidencia inconsistente: creación confirmada sin inicio")
    }
    const inactive = execution === "blocked" || execution === "not_started"
    let budgetLimitMinor: number | null = null
    if (s.mode === "HNP" && s.constraints !== null) {
      const hard: unknown = JSON.parse(s.constraints)
      const limit = object(hard).max_total_amount_minor
      budgetLimitMinor = limit == null ? null : integer(limit)
    }
    return {
      ...rest,
      execution: execution === "recorded" ? "started" : execution,
      creation: inactive ? "not_applicable" : initialResult === "not_available" ? "uncertain" : initialResult,
      recovery: inactive ? "not_applicable" : result === "not_available" ? "uncertain" : result,
      sourceKind: s.mode === "HP" ? "historical_http_postgresql" : "scripted_offline",
      budgetLimitMinor,
    }
  })
  return parseEvidence({ ...old, schema: "governance-monitor/2", scenarios })
}
