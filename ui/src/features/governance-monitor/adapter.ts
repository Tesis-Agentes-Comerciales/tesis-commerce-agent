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

export type EvidenceBundle = {
  schema: "governance-monitor/2"
  status: "PARTIAL"
  sourcePins: Record<string, string>
  hnpLedgerEntriesVerified: number
  scenarios: Scenario[]
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
export function parseEvidence(value: unknown): EvidenceBundle {
  const bundle = object(value, ["schema", "status", "sourcePins", "hnpLedgerEntriesVerified", "scenarios"])
  choice(bundle.schema, ["governance-monitor/2"])
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
    if (result.mode === "HNP" && (result.validator === null || result.sourceKind !== "scripted_offline" || result.executionKind !== "scripted offline" || result.shadow !== null || result.enforce !== null || result.commercialPosts !== 0 || result.backend.executedPolicyIds?.length !== 0)) throw Error("Capacidad HNP no acreditada")
    return result
  })
  if (!scenarios.length || new Set(scenarios.map((s) => s.id)).size !== scenarios.length) throw Error("Escenarios vacíos o duplicados")
  return { schema: "governance-monitor/2", status: "PARTIAL", sourcePins: pins, hnpLedgerEntriesVerified: integer(bundle.hnpLedgerEntriesVerified), scenarios }
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
  if (s.mode === "HP" && s.budgetLimitMinor !== null) return "límite HNP atribuido a HP"
  if (s.mode === "HNP" && s.totalMinor !== null && s.budgetLimitMinor !== null
    && s.totalMinor > s.budgetLimitMinor && (s.execution !== "blocked" || !s.backend.failures.includes("budget"))) return "presupuesto excedido sin bloqueo backend documentado"
  return null
}

export const executionText = { not_started: "No iniciada", blocked: "Bloqueada antes del envío", started: "Iniciada (registro histórico)", unknown: "Desconocida" } as const
export const creationText = { confirmed: "Confirmada históricamente", rejected: "Rechazada", uncertain: "Incierta", not_applicable: "No aplicable" } as const
export const recoveryText = { confirmed: "Confirmada · found", not_found: "not_found · no autoriza reintento", uncertain: "Incierta", not_applicable: "No aplicable" } as const

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
