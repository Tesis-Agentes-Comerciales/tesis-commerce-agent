// Delegaciones Human Not Present (docs/design/hnp-governed.md §5.3). Los campos van en snake_case
// porque llegan tal cual de la API de delegaciones; el modelo Pydantic del backend
// (src/commerce_agent/delegation/contract.py) valida las mismas fixtures de
// src/storybook/fixtures/delegation-*.json. Las vistas nunca traen mandatos, tokens, la dirección
// completa ni la identidad del dueño, y solo la propuesta trae `scope_digest`. Los textos llegan
// escritos del backend en `message` (§8.3): la UI no los duplica.

import type { Money } from "@/lib/trace-contract"

export type GrantState = "active" | "consumed" | "revoked" | "expired"
export type RevokedReason = "owner" | "operator" | "escalated" | "failed"
export type TaskState =
  | "scheduled"
  | "running"
  | "purchased"
  | "escalated"
  | "failed"
  | "canceled"
  | "uncertain"

const ESCALATION_CODES = [
  "budget_not_satisfiable",
  "no_candidate_in_scope",
  "choice_not_confirmed",
  "selection_unavailable",
  "offer_changed",
  "terms_out_of_scope",
  "authorization_profile_unresolved",
  "verifier_unresolved_constraint",
  "verifier_mandates_not_supported",
] as const
const CANCEL_CODES = ["revoked_by_owner", "kill_switch", "grant_expired"] as const
const FAILURE_CODES = [
  "selection_not_eligible",
  "merchant_not_allowed",
  "mandate_proof_invalid",
  "policy_denied",
  "governance_unavailable",
  "state_conflict",
  "verifier_invalid_mandate",
  "verifier_invalid_credential",
  "checkout_failed",
  "budget_guard",
  "close_expired_before_dispatch",
] as const
const UNCERTAIN_CODES = ["dispatch_timeout", "partial_verifier_failure", "receipt_mismatch"] as const

/** Por qué la delegación volvió al comprador (§8.1). */
export type EscalationCode = (typeof ESCALATION_CODES)[number]
/** Código de cómo terminó la tarea (§8.1); `null` mientras no termina o si compró. */
export type OutcomeCode =
  | EscalationCode
  | (typeof CANCEL_CODES)[number]
  | (typeof FAILURE_CODES)[number]
  | (typeof UNCERTAIN_CODES)[number]
export type RevokeEffect = "no_effect" | "released" | "in_flight"

export interface ScopeVariant {
  variant_id: string
  title: string
}

/** Alcance tal como lo muestra la tarjeta de consentimiento. */
export interface ScopeView {
  merchant_name: string
  /** De 1 a 5 variantes. */
  variants: ScopeVariant[]
  /** Incluye envío. */
  total_max: Money
  /** Vencimiento de la delegación (ISO-8601 UTC). */
  expires_at: string
  /** «Ciudad, Departamento», nunca la dirección completa. */
  destination_label: string
  instrument_label: string
  uses: 1
  /** Texto del propio comprador, de 1 a 280 caracteres. */
  preference_text: string | null
}

export interface DelegationProposal {
  proposal_id: string
  scope: ScopeView
  /** Lo que el clic «Autorizar» devuelve con `proposal_id`. */
  scope_digest: string
  presented_at: string
  /** Vencimiento de la propuesta, no el de la delegación. */
  expires_at: string
}

/** Lo que `POST /delegations` recibe: la propuesta y el digest del alcance que se mostró. */
export type DelegationConsent = Pick<DelegationProposal, "proposal_id" | "scope_digest">

export interface TaskView {
  task_id: string
  state: TaskState
  outcome_code: OutcomeCode | null
  run_id: string | null
  order_id: string | null
  message: string | null
  updated_at: string
}

/** «Necesito tu decisión»: el CTA «Comprar desde el chat» prellena `prefill_request`. */
export interface EscalationView {
  reason: EscalationCode
  title: string
  message: string
  prefill_request: string
}

export interface DelegationView {
  grant_id: string
  state: GrantState
  /** Solo en `revoked`. */
  revoked_reason: RevokedReason | null
  scope: ScopeView
  consented_at: string
  expires_at: string
  task: TaskView
  /** Solo si la tarea escaló. */
  escalation: EscalationView | null
  last_activity_at: string
}

/** `GET /me/delegations`, más recientes primero. */
export interface DelegationList {
  delegations: DelegationView[]
}

export interface RevokeReceipt {
  grant_id: string
  state: "revoked"
  /** Otra causa que `owner`: la delegación ya estaba cerrada y la respuesta es `no_effect`. */
  revoked_reason: RevokedReason
  revoked_at: string
  effect: RevokeEffect
  message: string
}

/** `POST /me/delegations/revoke-all`: un recibo por delegación que estaba activa. */
export interface RevokeAllReceipt {
  receipts: RevokeReceipt[]
}

/**
 * Hasta cuándo se puede autorizar la propuesta. Su `expires_at` es el del TTL, pero el servidor
 * también la da por vencida cuando pasa el de la delegación, que se fijó al proponer y puede llegar
 * antes (`valid_for_seconds` corto): manda el primero (§4.2, §5.1).
 */
export function proposalDeadline({ expires_at, scope }: DelegationProposal): string {
  return Date.parse(scope.expires_at) < Date.parse(expires_at) ? scope.expires_at : expires_at
}

// Validación en tiempo de ejecución, con las mismas reglas que el modelo Pydantic. Los ayudantes
// son los de trace-contract.ts; se repiten aquí porque ese archivo es de la traza (HNP-11).
// `Fields<T>` obliga a que cada validador nombre exactamente los campos del tipo.

/** Devuelve la ruta del primer campo que no cumple, o `null`. */
type Check = (value: unknown, path: string) => string | null
type Fields<T> = { [K in keyof T]-?: Check }
/** Regla entre campos de un objeto que ya cumple su forma. */
type Rule = (value: Record<string, unknown>, path: string) => string | null

const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/
const ID = /^[A-Za-z0-9._:-]{1,128}$/

const when =
  (valid: (value: unknown) => boolean): Check =>
  (value, path) =>
    valid(value) ? null : path
/** Longitud en puntos de código, como `min_length`/`max_length` de Pydantic. */
const textOf = (min: number, max: number) =>
  when((value) => typeof value === "string" && [...value].length >= min && [...value].length <= max)
const text = textOf(1, Number.POSITIVE_INFINITY)
const count = when((value) => Number.isSafeInteger(value) && (value as number) >= 0)
const currency = when((value) => typeof value === "string" && /^[A-Za-z]{3}$/.test(value))
const utc = when(
  (value) => typeof value === "string" && UTC_TIME.test(value) && !Number.isNaN(Date.parse(value)),
)
const id = when((value) => typeof value === "string" && ID.test(value))
/** Id con su prefijo (§4.1): `dpr_` propuesta, `dg_` delegación, `ht_` tarea. */
const idWith = (prefix: "dpr_" | "dg_" | "ht_") => {
  const pattern = new RegExp(`^${prefix}[A-Za-z0-9._:-]+$`)
  return when((value) => typeof value === "string" && value.length <= 128 && pattern.test(value))
}
const digest = when((value) => typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value))
const oneOf = <T>(...options: readonly T[]) => when((value) => options.includes(value as T))
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
  (check: Check, min = 0, max = Number.POSITIVE_INFINITY): Check =>
  (value, path) =>
    Array.isArray(value) && value.length >= min && value.length <= max
      ? firstFailure(value.map((item, index) => check(item, `${path}[${index}]`)))
      : path

/** Objeto con exactamente estos campos: uno de más también es un error, como en Pydantic. */
function shape(fields: Record<string, Check>, rule?: Rule): Check {
  return (value, path) => {
    if (!isRecord(value)) return path
    const unknownKey = Object.keys(value).find((key) => !Object.hasOwn(fields, key))
    if (unknownKey !== undefined) return `${path}.${unknownKey}`
    const failure = firstFailure(
      Object.entries(fields).map(([key, check]) => check(value[key], `${path}.${key}`)),
    )
    return failure ?? rule?.(value, path) ?? null
  }
}

const REVOKED_REASONS: RevokedReason[] = ["owner", "operator", "escalated", "failed"]
const EFFECTS: RevokeEffect[] = ["no_effect", "released", "in_flight"]

/**
 * Códigos que admite cada estado de la tarea (§8.1); los demás estados no llevan código. B0-HNP
 * nunca escala: la causa que en B2 escala allí falla con el mismo código.
 */
const OUTCOMES: Partial<Record<TaskState, readonly OutcomeCode[]>> = {
  escalated: ESCALATION_CODES,
  canceled: CANCEL_CODES,
  failed: [...FAILURE_CODES, ...ESCALATION_CODES],
  uncertain: UNCERTAIN_CODES,
}

const outcomeBelongsToState: Rule = (task, path) => {
  const allowed = OUTCOMES[task.state as TaskState] ?? []
  const valid =
    allowed.length > 0
      ? allowed.includes(task.outcome_code as OutcomeCode)
      : task.outcome_code === null
  return valid ? null : `${path}.outcome_code`
}

const statesAgree: Rule = (delegation, path) => {
  const task = delegation.task as TaskView
  const escalation = delegation.escalation as EscalationView | null
  if ((delegation.state === "revoked") !== (delegation.revoked_reason !== null)) {
    return `${path}.revoked_reason`
  }
  if ((task.state === "escalated") !== (escalation !== null)) return `${path}.escalation`
  if (escalation !== null && escalation.reason !== task.outcome_code) {
    return `${path}.escalation.reason`
  }
  return null
}

/** §5.1: una delegación cerrada por otra causa no se toca. */
const onlyOwnerRevocationHasEffect: Rule = (receipt, path) =>
  receipt.revoked_reason !== "owner" && receipt.effect !== "no_effect" ? `${path}.effect` : null

const money = shape({ minor: count, currency } satisfies Fields<Money>)

const scopeView = shape({
  merchant_name: text,
  variants: listOf(shape({ variant_id: id, title: text } satisfies Fields<ScopeVariant>), 1, 5),
  total_max: money,
  expires_at: utc,
  destination_label: text,
  instrument_label: text,
  uses: oneOf(1),
  preference_text: nullable(textOf(1, 280)),
} satisfies Fields<ScopeView>)

const delegationProposal = shape({
  proposal_id: idWith("dpr_"),
  scope: scopeView,
  scope_digest: digest,
  presented_at: utc,
  expires_at: utc,
} satisfies Fields<DelegationProposal>)

const taskView = shape(
  {
    task_id: idWith("ht_"),
    state: oneOf<TaskState>(
      "scheduled",
      "running",
      "purchased",
      "escalated",
      "failed",
      "canceled",
      "uncertain",
    ),
    outcome_code: nullable(
      oneOf<OutcomeCode>(...ESCALATION_CODES, ...CANCEL_CODES, ...FAILURE_CODES, ...UNCERTAIN_CODES),
    ),
    run_id: nullable(text),
    order_id: nullable(text),
    message: nullable(text),
    updated_at: utc,
  } satisfies Fields<TaskView>,
  outcomeBelongsToState,
)

const escalationView = shape({
  reason: oneOf<EscalationCode>(...ESCALATION_CODES),
  title: text,
  message: text,
  prefill_request: text,
} satisfies Fields<EscalationView>)

const delegationView = shape(
  {
    grant_id: idWith("dg_"),
    state: oneOf<GrantState>("active", "consumed", "revoked", "expired"),
    revoked_reason: nullable(oneOf(...REVOKED_REASONS)),
    scope: scopeView,
    consented_at: utc,
    expires_at: utc,
    task: taskView,
    escalation: nullable(escalationView),
    last_activity_at: utc,
  } satisfies Fields<DelegationView>,
  statesAgree,
)

const revokeReceipt = shape(
  {
    grant_id: idWith("dg_"),
    state: oneOf("revoked"),
    revoked_reason: oneOf(...REVOKED_REASONS),
    revoked_at: utc,
    effect: oneOf(...EFFECTS),
    message: text,
  } satisfies Fields<RevokeReceipt>,
  onlyOwnerRevocationHasEffect,
)

function parse<T>(check: Check, root: string, value: unknown): T {
  const invalid = check(value, root)
  if (invalid !== null) throw new TypeError(`Invalid delegation contract at ${invalid}`)
  return value as T
}

// Cada función valida una respuesta de la API o una fixture; el error nombra la ruta del primer
// campo inválido.

export function parseDelegationProposal(value: unknown): DelegationProposal {
  return parse(delegationProposal, "proposal", value)
}

export function parseDelegationView(value: unknown): DelegationView {
  return parse(delegationView, "delegation", value)
}

export function parseDelegationList(value: unknown): DelegationList {
  return parse(
    shape({ delegations: listOf(delegationView) } satisfies Fields<DelegationList>),
    "delegations",
    value,
  )
}

export function parseDelegationRevokeReceipt(value: unknown): RevokeReceipt {
  return parse(revokeReceipt, "receipt", value)
}

export function parseDelegationRevokeAllReceipt(value: unknown): RevokeAllReceipt {
  return parse(
    shape({ receipts: listOf(revokeReceipt) } satisfies Fields<RevokeAllReceipt>),
    "receipts",
    value,
  )
}
