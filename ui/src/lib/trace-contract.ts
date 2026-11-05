// Traza de una compra (docs/design/trace-panel.md). Los campos van en snake_case porque llegan tal
// cual de `GET /purchases/{run_id}/trace`; el modelo Pydantic del backend valida las mismas fixtures
// de src/storybook/trace-fixtures/. La traza nunca trae JWT, mandatos ni tokens de pago.

/** `operator`: el kill switch de las compras delegadas (HNP). */
export type TraceActor = "user" | "agent" | "policy" | "merchant" | "payment" | "operator"

/** Compra con la persona presente (HP) o delegada (HNP, docs/design/hnp-governed.md §10). */
export type PurchaseMode = "HP" | "HNP"

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

/**
 * Solo en el historial: la traza de esa compra no se pudo armar sin inventar datos (evidencia que no
 * verifica o un dato guardado fuera del contrato). La fila trae lo que guarda la compra; la traza
 * misma responde 503.
 */
export type SummaryStatus = PurchaseStatus | "trace_unavailable"

export type StepState = "done" | "running" | "waiting" | "blocked" | "error" | "skipped"

export interface PurchaseTrace {
  run_id: string
  status: PurchaseStatus
  policy_mode: "off" | "shadow" | "enforce"
  request: string
  /** Origen del comercio: lo que comparan la política y los términos. */
  merchant: string | null
  /** Nombre que declara el feed del comercio (`seller.name`); `null` si no se conoce. */
  merchant_name: string | null
  total: { minor: number; currency: string } | null
  /** ISO-8601 UTC. */
  started_at: string
  updated_at: string
  /** En orden cronológico. */
  steps: TraceStep[]
  /** Solo en una compra delegada (`"HNP"`); una compra HP no lo trae. */
  purchase_mode?: PurchaseMode
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
  /**
   * D2: al aprobar, la política revisa la compra (POL-03..06) antes de firmar. `state: "blocked"`
   * si la detuvo: no se registró la aprobación ni se firmó nada. En una compra delegada revisa
   * además el grant (POL-07..09) antes de reservar el uso único.
   */
  | { kind: "authorization_decision"; decision: PolicyDecision }
  | { kind: "authorization_signed"; mandates: ("checkout" | "payment")[] }
  | { kind: "payment_authorized"; max_minor: number; currency: string }
  | { kind: "order_confirmed"; order_id: string; receipts: ("checkout" | "payment")[] }
  | { kind: "order_event"; event_type: string }
  /** `expiry`: la delegación venció antes de comprar. */
  | { kind: "canceled"; by: "user" | "budget" | "expiry" }
  /**
   * `merchant_refusal`: solo en el fallo de una tarea delegada `checkout_failed` cuyo checkout el
   * comercio se negó a crear con un código tipado, sin que quedara ninguno. Si no, no viene.
   */
  | { kind: "failed"; code: string; merchant_refusal?: MerchantRefusal }
  | ({ kind: "freshness_check" } & FreshnessCheck)
  | ({ kind: "governed_recovery" } & GovernedRecovery)
  // Compra delegada (HNP): el consentimiento reemplaza al pedido y nadie aprueba ni firma al final.
  /** `total_max` incluye el envío. */
  | { kind: "delegation_consented"; grant_id: string; total_max: Money; expires_at: string }
  /** Uno por cada vez que el servicio toma la tarea: el segundo la reanuda. */
  | { kind: "task_activated"; trigger: "service" }
  /**
   * El revisor de intención comparó la elección con lo que pediste, antes de la política
   * (docs/design/hnp-intent-review.md). `state: "blocked"` si no la confirmó: la tarea escaló.
   */
  | { kind: "choice_reviewed"; decision: ReviewDecision; reason: ReviewReason }
  | { kind: "reservation"; outcome: "reserved" | "released" | "consumed" | "uncertain"; amount: Money }
  | { kind: "mandates_closed" }
  /** D3: la política revisa la autoridad justo antes de comprometer el despacho (POL-09, POL-10). */
  | { kind: "dispatch_decision"; decision: PolicyDecision }
  | { kind: "revoked"; by: "owner" | "operator"; effect: "no_effect" | "released" | "in_flight" }
  /** La decisión volvió a la persona: no se firmó ni se cobró nada. */
  | { kind: "escalated"; reason: EscalationReason }
)

/** Por qué el comercio se negó a crear el checkout: respondió antes de crear nada. */
export type MerchantRefusal = "out_of_stock" | "offer_expired" | "invalid_item"

/** Códigos de escalamiento del ledger de delegación (hnp-governed.md §8.1). */
export type EscalationReason =
  | "budget_not_satisfiable"
  | "no_candidate_in_scope"
  | "choice_not_confirmed"
  | "selection_unavailable"
  | "offer_changed"
  | "terms_out_of_scope"
  | "authorization_profile_unresolved"
  | "verifier_unresolved_constraint"
  | "verifier_mandates_not_supported"

export type ReviewDecision = "confirmed" | "not_confirmed"

/** `review_unavailable`: el revisor falló o no respondió, y la compra se detuvo por precaución. */
export type ReviewReason =
  | "matches_request"
  | "contradicts_preference"
  | "outside_request"
  | "unclear"
  | "review_unavailable"

export type StepKind = TraceStep["kind"]

// Controles de la aplicación del flujo HP gobernado (docs/design/b2-governed-pilot.md). Salen de
// `hp_evidence`, no de la política de compra: la UI nunca los presenta como reglas.

/** Frescura de la oferta: `state` es `blocked` cuando la comprobación detuvo la compra. */
export interface FreshnessCheck {
  /** `close`: antes de que una compra delegada reserve su uso único. */
  stage: "present" | "approve" | "close"
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

/** Monto en unidades menores (centavos). */
export interface Money {
  minor: number
  currency: string
}

export interface PolicyDecision {
  mode: "shadow" | "enforce"
  outcome: "allow" | "deny"
  effect: "observed" | "blocked" | "continued"
  policy: { set_id: string; revision: number }
  rules: {
    /** "POL-01", "POL-02", "POL-11"; al aprobar, "POL-03" a "POL-06"; al delegar, hasta "POL-10". */
    id: string
    result: "pass" | "fail" | "not_evaluated"
    /**
     * Un texto, como el origen del comercio, o un monto, como el precio del catálogo (POL-11) o el
     * total del comercio (POL-05).
     */
    observed: string | Money | null
    /** Un texto, como la lista de comercios permitidos, o un monto, como tu presupuesto total. */
    expected: string | Money | null
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
  /** `null` solo en una fila `trace_unavailable` cuya propia fila de compra no cumple el contrato. */
  started_at: string | null
  request: string | null
  merchant: string | null
  merchant_name: string | null
  total: { minor: number; currency: string } | null
  status: SummaryStatus
  policy: { outcome: "allow" | "deny"; failed_rules: string[] } | null
  approved_at: string | null
  order_id: string | null
  duration_ms: number | null
  purchase_mode?: PurchaseMode
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
/** Campo que el backend omite cuando no aplica, como `purchase_mode` en una compra HP. */
const optional =
  (check: Check): Check =>
  (value, path) =>
    value === undefined ? null : check(value, path)

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

const money = shape({ minor: count, currency } satisfies Fields<Money>)
/** Valor de una regla: texto o monto, nunca un número suelto. */
const ruleValue: Check = (value, path) => (isRecord(value) ? money(value, path) : text(value, path))
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
      observed: nullable(ruleValue),
      expected: nullable(ruleValue),
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
  actor: oneOf<TraceActor>("user", "agent", "policy", "merchant", "payment", "operator"),
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
  authorization_decision: { decision: shape(POLICY_DECISION) },
  authorization_signed: { mandates: listOf(receiptKind) },
  payment_authorized: { max_minor: count, currency },
  order_confirmed: { order_id: text, receipts: listOf(receiptKind) },
  order_event: { event_type: text },
  canceled: { by: oneOf("user", "budget", "expiry") },
  failed: {
    code: text,
    merchant_refusal: optional(oneOf<MerchantRefusal>("out_of_stock", "offer_expired", "invalid_item")),
  },
  freshness_check: {
    stage: oneOf<FreshnessCheck["stage"]>("present", "approve", "close"),
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
  delegation_consented: { grant_id: text, total_max: money, expires_at: utc },
  task_activated: { trigger: oneOf("service") },
  choice_reviewed: {
    decision: oneOf<ReviewDecision>("confirmed", "not_confirmed"),
    reason: oneOf<ReviewReason>(
      "matches_request",
      "contradicts_preference",
      "outside_request",
      "unclear",
      "review_unavailable",
    ),
  },
  reservation: {
    outcome: oneOf("reserved", "released", "consumed", "uncertain"),
    amount: money,
  },
  mandates_closed: {},
  dispatch_decision: { decision: shape(POLICY_DECISION) },
  revoked: {
    by: oneOf("owner", "operator"),
    effect: oneOf("no_effect", "released", "in_flight"),
  },
  escalated: {
    reason: oneOf<EscalationReason>(
      "budget_not_satisfiable",
      "no_candidate_in_scope",
      "choice_not_confirmed",
      "selection_unavailable",
      "offer_changed",
      "terms_out_of_scope",
      "authorization_profile_unresolved",
      "verifier_unresolved_constraint",
      "verifier_mandates_not_supported",
    ),
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
  merchant_name: nullable(text),
  total: nullable(money),
  started_at: utc,
  updated_at: utc,
  steps: listOf(step),
  purchase_mode: optional(oneOf<PurchaseMode>("HP", "HNP")),
} satisfies Fields<PurchaseTrace>)

/** Valida una traza de la API o de una fixture; el error nombra la ruta del primer campo inválido. */
export function parsePurchaseTrace(value: unknown): PurchaseTrace {
  const invalid = purchaseTrace(value, "trace")
  if (invalid !== null) throw new TypeError(`Invalid purchase trace at ${invalid}`)
  return value as PurchaseTrace
}
