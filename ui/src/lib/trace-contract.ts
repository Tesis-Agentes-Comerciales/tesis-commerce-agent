// Traza de una compra (docs/design/trace-panel.md). Los campos van en snake_case porque llegan tal
// cual de `GET /purchases/{run_id}/trace`; el modelo Pydantic del backend valida las mismas fixtures
// de src/storybook/trace-fixtures/. La traza nunca trae JWT, mandatos ni tokens de pago.

export type TraceActor = "user" | "agent" | "policy" | "merchant" | "payment"

export type PurchaseStatus =
  | "in_progress"
  | "awaiting_approval"
  | "completed"
  | "blocked"
  /** La frescura de la oferta bloqueó los términos: solo queda cancelar. */
  | "stale_terms"
  | "canceled"
  | "failed"
  | "uncertain"

export type StepState = "done" | "running" | "waiting" | "blocked" | "error" | "skipped"

export interface PurchaseTrace {
  run_id: string
  status: PurchaseStatus
  policy_mode: "off" | "shadow" | "enforce"
  request: string
  merchant: string | null
  total: { minor: number; currency: string } | null
  /** ISO-8601 UTC. */
  started_at: string
  updated_at: string
  /** En orden cronológico. */
  steps: TraceStep[]
}

export type TraceStep = {
  /** Estable dentro del run. */
  id: string
  /** `null` solo si la fuente no guarda la hora. */
  at: string | null
  duration_ms: number | null
  actor: TraceActor
  state: StepState
} & (
  | { kind: "request" }
  | { kind: "selection"; product: string; total_minor: number; currency: string }
  | { kind: "policy_decision"; decision: PolicyDecision }
  | { kind: "checkout_created"; checkout_id: string }
  | { kind: "approval"; fingerprint: string; expires_at: string; approved_at: string | null }
  | { kind: "authorization_signed"; mandates: ("checkout" | "payment")[] }
  | { kind: "payment_authorized"; max_minor: number; currency: string }
  | { kind: "order_confirmed"; order_id: string; receipts: ("checkout" | "payment")[] }
  | { kind: "order_event"; event_type: string }
  | { kind: "canceled"; by: "user" | "budget" }
  | { kind: "failed"; code: string }
  | ({ kind: "freshness_check" } & FreshnessCheck)
  | ({ kind: "governed_recovery" } & GovernedRecovery)
)

export type StepKind = TraceStep["kind"]

// Controles de la aplicación del flujo HP gobernado (docs/design/b2-governed-pilot.md). Salen de
// `hp_evidence`, no de la política de compra: la UI nunca los presenta como reglas.

/** Frescura de la oferta: `state` es `blocked` cuando la comprobación detuvo la compra. */
export interface FreshnessCheck {
  stage: "present" | "approve"
  mode: "shadow" | "enforce"
  outcome: "fresh" | "changed" | "unavailable"
  /** Solo los campos que cambiaron: el valor del checkout frente al de la oferta viva. */
  changes: FreshnessChange[]
  /** Moneda de los términos del checkout, para los montos. */
  currency: string | null
  offer_revision: number | null
  /** Código si no se pudo leer la oferta (`outcome: "unavailable"`). */
  read_error: string | null
  evidence_digest: string
}

export type FreshnessChange =
  | {
      field: "subtotal_minor" | "shipping_minor" | "total_minor" | "delivery_days" | "stock"
      presented: number | null
      current: number | null
    }
  | { field: "currency"; presented: string | null; current: string | null }
  /** ISO-8601 UTC. */
  | { field: "expires_at"; presented: string | null; current: string | null }

export type FreshnessField = FreshnessChange["field"]

export type RecoveryCause =
  | "budget"
  | "unavailable"
  | "currency"
  | "category"
  | "condition"
  | "option"
  | "term"
  | "not_shortlisted"
  | "not_in_catalog"
  | "no_match"

/** El agente explicó por qué la elección no podía seguir, en vez de un error técnico. */
export interface GovernedRecovery {
  recovery: "no_candidate" | "selection_outside_shortlist" | "budget_exceeded_after_checkout"
  cause: RecoveryCause
  variant_id: string | null
  budget_minor: number | null
  /** Precio del catálogo, no el total del comercio. */
  observed_minor: number | null
  currency: string | null
  evidence_digest: string
}

export interface PolicyDecision {
  mode: "shadow" | "enforce"
  outcome: "allow" | "deny"
  effect: "observed" | "blocked" | "continued"
  policy: { set_id: string; revision: number }
  rules: {
    /** "POL-01", "POL-02". */
    id: string
    result: "pass" | "fail" | "not_evaluated"
    /** Por ejemplo, el origen del comercio. */
    observed: string | null
    /** Por ejemplo, la lista de comercios permitidos. */
    expected: string | null
  }[]
  reason_codes: string[]
  latency_ms: number | null
  decision_id: string
  evidence_id: string
}

export type PolicyRule = PolicyDecision["rules"][number]

/** Fila de `GET /me/purchases`. */
export interface PurchaseSummary {
  run_id: string
  started_at: string
  request: string
  merchant: string | null
  total: { minor: number; currency: string } | null
  status: PurchaseStatus
  policy: { outcome: "allow" | "deny"; failed_rules: string[] } | null
  approved_at: string | null
  order_id: string | null
  duration_ms: number | null
}

// Validación en tiempo de ejecución. La API no es confiable y las fixtures JSON no se pueden
// comprobar con `satisfies`: TypeScript ensancha los literales de un import JSON a `string`.
// `Fields<T>` obliga a que cada validador nombre exactamente los campos del tipo.

/** Devuelve la ruta del primer campo que no cumple, o `null`. */
type Check = (value: unknown, path: string) => string | null
type Fields<T> = { [K in keyof T]-?: Check }

const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/

const when =
  (valid: (value: unknown) => boolean): Check =>
  (value, path) =>
    valid(value) ? null : path
const text = when((value) => typeof value === "string" && value.length > 0)
const count = when((value) => Number.isSafeInteger(value) && (value as number) >= 0)
const currency = when((value) => typeof value === "string" && /^[A-Za-z]{3}$/.test(value))
const utc = when(
  (value) => typeof value === "string" && UTC_TIME.test(value) && !Number.isNaN(Date.parse(value)),
)
const oneOf = <T>(...options: T[]) => when((value) => options.includes(value as T))
const nullable =
  (check: Check): Check =>
  (value, path) =>
    value === null ? null : check(value, path)

function firstFailure(failures: (string | null)[]) {
  return failures.find((failure) => failure !== null) ?? null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

const listOf =
  (check: Check): Check =>
  (value, path) =>
    Array.isArray(value)
      ? firstFailure(value.map((item, index) => check(item, `${path}[${index}]`)))
      : path

/** Objeto con exactamente estos campos: uno de más también es un error, como en Pydantic. */
function shape(fields: Record<string, Check>): Check {
  return (value, path) => {
    if (!isRecord(value)) return path
    const unknownKey = Object.keys(value).find((key) => !Object.hasOwn(fields, key))
    if (unknownKey !== undefined) return `${path}.${unknownKey}`
    return firstFailure(
      Object.entries(fields).map(([key, check]) => check(value[key], `${path}.${key}`)),
    )
  }
}

const money = shape({ minor: count, currency } satisfies Fields<{ minor: number; currency: string }>)
const receiptKind = oneOf("checkout", "payment")

const POLICY_DECISION: Fields<PolicyDecision> = {
  mode: oneOf<PolicyDecision["mode"]>("shadow", "enforce"),
  outcome: oneOf<PolicyDecision["outcome"]>("allow", "deny"),
  effect: oneOf<PolicyDecision["effect"]>("observed", "blocked", "continued"),
  policy: shape({ set_id: text, revision: count } satisfies Fields<PolicyDecision["policy"]>),
  rules: listOf(
    shape({
      id: text,
      result: oneOf<PolicyRule["result"]>("pass", "fail", "not_evaluated"),
      observed: nullable(text),
      expected: nullable(text),
    } satisfies Fields<PolicyRule>),
  ),
  reason_codes: listOf(text),
  latency_ms: nullable(count),
  decision_id: text,
  evidence_id: text,
}

type StepBase = Pick<TraceStep, "id" | "at" | "duration_ms" | "actor" | "state">
type StepFields<K extends StepKind> = Fields<
  Omit<Extract<TraceStep, { kind: K }>, keyof StepBase | "kind">
>

const STEP_BASE: Fields<StepBase> = {
  id: text,
  at: nullable(utc),
  duration_ms: nullable(count),
  actor: oneOf<TraceActor>("user", "agent", "policy", "merchant", "payment"),
  state: oneOf<StepState>("done", "running", "waiting", "blocked", "error", "skipped"),
}

const AMOUNT_FIELDS = ["subtotal_minor", "shipping_minor", "total_minor", "delivery_days", "stock"]

/** Cada campo con su tipo de valor, como la unión discriminada del modelo Pydantic. */
const changeValue = (field: unknown): Check | null => {
  if (AMOUNT_FIELDS.includes(field as string)) return nullable(count)
  if (field === "currency") return nullable(currency)
  if (field === "expires_at") return nullable(utc)
  return null
}

const freshnessChange: Check = (value, path) => {
  const valueCheck = isRecord(value) ? changeValue(value.field) : null
  if (valueCheck === null) return `${path}.field`
  return shape({ field: text, presented: valueCheck, current: valueCheck })(value, path)
}

const STEP_FIELDS: { [K in StepKind]: StepFields<K> } = {
  request: {},
  selection: { product: text, total_minor: count, currency },
  policy_decision: { decision: shape(POLICY_DECISION) },
  checkout_created: { checkout_id: text },
  approval: { fingerprint: text, expires_at: utc, approved_at: nullable(utc) },
  authorization_signed: { mandates: listOf(receiptKind) },
  payment_authorized: { max_minor: count, currency },
  order_confirmed: { order_id: text, receipts: listOf(receiptKind) },
  order_event: { event_type: text },
  canceled: { by: oneOf("user", "budget") },
  failed: { code: text },
  freshness_check: {
    stage: oneOf<FreshnessCheck["stage"]>("present", "approve"),
    mode: oneOf<FreshnessCheck["mode"]>("shadow", "enforce"),
    outcome: oneOf<FreshnessCheck["outcome"]>("fresh", "changed", "unavailable"),
    changes: listOf(freshnessChange),
    currency: nullable(currency),
    offer_revision: nullable(count),
    read_error: nullable(text),
    evidence_digest: text,
  },
  governed_recovery: {
    recovery: oneOf<GovernedRecovery["recovery"]>(
      "no_candidate",
      "selection_outside_shortlist",
      "budget_exceeded_after_checkout",
    ),
    cause: oneOf<RecoveryCause>(
      "budget",
      "unavailable",
      "currency",
      "category",
      "condition",
      "option",
      "term",
      "not_shortlisted",
      "not_in_catalog",
      "no_match",
    ),
    variant_id: nullable(text),
    budget_minor: nullable(count),
    observed_minor: nullable(count),
    currency: nullable(currency),
    evidence_digest: text,
  },
}

const step: Check = (value, path) => {
  const kind = isRecord(value) ? value.kind : undefined
  if (typeof kind !== "string" || !Object.hasOwn(STEP_FIELDS, kind)) return `${path}.kind`
  return shape({ ...STEP_BASE, kind: text, ...STEP_FIELDS[kind as StepKind] })(value, path)
}

const purchaseTrace = shape({
  run_id: text,
  status: oneOf<PurchaseStatus>(
    "in_progress",
    "awaiting_approval",
    "completed",
    "blocked",
    "stale_terms",
    "canceled",
    "failed",
    "uncertain",
  ),
  policy_mode: oneOf<PurchaseTrace["policy_mode"]>("off", "shadow", "enforce"),
  request: text,
  merchant: nullable(text),
  total: nullable(money),
  started_at: utc,
  updated_at: utc,
  steps: listOf(step),
} satisfies Fields<PurchaseTrace>)

/** Valida una traza de la API o de una fixture; el error nombra la ruta del primer campo inválido. */
export function parsePurchaseTrace(value: unknown): PurchaseTrace {
  const invalid = purchaseTrace(value, "trace")
  if (invalid !== null) throw new TypeError(`Invalid purchase trace at ${invalid}`)
  return value as PurchaseTrace
}
