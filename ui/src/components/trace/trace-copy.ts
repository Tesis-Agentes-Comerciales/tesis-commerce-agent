import {
  formatDateTime,
  formatElapsed,
  formatMoney,
  formatPreciseDateTime,
  formatTime,
} from "@/components/agent/agent-format"
import type { EvidenceRef } from "@/components/trace/evidence-refs"
import type { PurchaseStage } from "@/lib/agent-contract"
import type {
  EscalationReason,
  FreshnessChange,
  FreshnessField,
  GovernedRecovery,
  PolicyDecision,
  PolicyRule,
  PurchaseStatus,
  PurchaseTrace,
  RecoveryCause,
  StepKind,
  StepState,
  TraceStep,
} from "@/lib/trace-contract"

// Único origen de los textos de la traza, según la tabla «Lenguaje» de docs/design/trace-panel.md.
// La UI no muestra códigos internos (D1, POL-01, shadow): esos solo van en el detalle técnico.

export const STEP_STATE_LABEL: Record<StepState, string> = {
  done: "Hecho",
  running: "En curso",
  waiting: "Esperando",
  // Sin atribuir: lo detiene la política o un control de la aplicación; el título dice cuál.
  blocked: "Detenido",
  error: "Falló",
  skipped: "No se ejecutó",
}

export const PURCHASE_STATUS_LABEL: Record<PurchaseStatus, string> = {
  in_progress: "En curso",
  awaiting_approval: "Esperando tu aprobación",
  completed: "Completada",
  blocked: "Bloqueada por la política",
  stale_terms: "Detenida: condiciones del comercio",
  canceled: "Cancelada",
  failed: "No se completó",
  uncertain: "Por confirmar",
}

export const POLICY_MODE_LABEL: Record<PurchaseTrace["policy_mode"], string> = {
  off: "Política desactivada",
  shadow: "Solo observando: registra, no detiene",
  enforce: "Activa: detiene la compra si no cumple",
}

export const RULE_RESULT_LABEL: Record<PolicyRule["result"], string> = {
  pass: "Cumple",
  fail: "No cumple",
  not_evaluated: "Sin evaluar",
}

type RuleCopy = { name: string; observed: string; expected: string }

const OTHER_RULE: RuleCopy = {
  name: "Otra regla de la política",
  observed: "Valor observado",
  expected: "Valor esperado",
}

const RULES: Record<string, RuleCopy> = {
  "POL-01": {
    name: "Producto dentro de lo que pediste",
    observed: "Producto elegido",
    expected: "Productos que cumplen tu pedido",
  },
  "POL-02": {
    name: "Comercio permitido",
    observed: "Comercio",
    expected: "Comercios permitidos",
  },
  // Al aprobar (D2). POL-03 revisa la identidad del dueño: nunca trae valores.
  "POL-03": {
    name: "Tu cuenta puede aprobar esta compra",
    observed: "Valor observado",
    expected: "Valor esperado",
  },
  // La traza solo trae sus valores si difieren: cubren el producto, no la presentación ni el clic.
  "POL-04": {
    name: "Es la misma compra que revisó la política",
    observed: "Producto aprobado",
    expected: "Producto que revisó la política",
  },
  // El total autoritativo del comercio, con envío, frente al presupuesto total.
  "POL-05": {
    name: "Total y condiciones dentro de lo que pediste",
    observed: "Total del comercio",
    expected: "Tu presupuesto total",
  },
  "POL-06": {
    name: "La política no cambió desde que eligió el producto",
    observed: "Política al elegir",
    expected: "Política vigente",
  },
  // Compra delegada (D2 y D3): comparan la delegación y su uso único, sin valores en la traza.
  "POL-07": { ...OTHER_RULE, name: "Tu delegación sigue vigente" },
  "POL-08": { ...OTHER_RULE, name: "Compra dentro de lo que autorizaste" },
  "POL-09": { ...OTHER_RULE, name: "El uso único está disponible para esta compra" },
  "POL-10": { ...OTHER_RULE, name: "La autorización sigue vigente al despachar" },
  // Compara el precio del catálogo con el presupuesto total: el total del comercio nunca es menor.
  "POL-11": {
    name: "Precio dentro de tu presupuesto",
    observed: "Precio en el catálogo",
    expected: "Tu presupuesto total",
  },
}

export function ruleCopy(ruleId: string) {
  return Object.hasOwn(RULES, ruleId) ? RULES[ruleId] : OTHER_RULE
}

/** Comercio de la compra: su origen, que es lo que evalúa la política, y su nombre legible. */
export type MerchantLabel = { origin: string; name: string }

/** Solo si la traza trae el nombre; sin él la UI no lo reemplaza por el origen. */
export function traceMerchant(
  trace: Pick<PurchaseTrace, "merchant" | "merchant_name">,
): MerchantLabel | undefined {
  return trace.merchant && trace.merchant_name
    ? { origin: trace.merchant, name: trace.merchant_name }
    : undefined
}

/**
 * Controles del recorrido normal de una compra, en orden; los eventos sueltos no cuentan. Tampoco
 * la frescura, la recuperación ni las decisiones al aprobar y al despachar: solo existen en el flujo
 * gobernado (y con una política que las trae), y una fila «No se ejecutó» suya mentiría con la
 * gobernanza apagada.
 */
export type ControlKind = Exclude<
  StepKind,
  | "order_event"
  | "canceled"
  | "failed"
  | "freshness_check"
  | "governed_recovery"
  | "authorization_decision"
  | "dispatch_decision"
  | "revoked"
  | "escalated"
>

type ControlCopy = { todo: string; running: string; done: string; error: string }

const CONTROLS: Record<ControlKind, ControlCopy> = {
  request: {
    todo: "Recibir tu pedido",
    running: "Recibiendo tu pedido…",
    done: "Pedido recibido",
    error: "No pude leer tu pedido",
  },
  selection: {
    todo: "Elegir el producto",
    running: "Eligiendo el producto…",
    done: "Producto elegido",
    error: "No pude elegir un producto",
  },
  policy_decision: {
    todo: "Revisar la política de compra",
    running: "Revisando la política de compra…",
    done: "Política de compra: permitida",
    error: "No se pudo revisar la política de compra",
  },
  checkout_created: {
    todo: "Crear el checkout",
    running: "Creando el checkout…",
    done: "Checkout creado en el comercio",
    error: "No se pudo crear el checkout",
  },
  approval: {
    todo: "Tu aprobación",
    running: "Esperando tu aprobación",
    done: "Aprobaste la compra",
    error: "No se pudo registrar tu aprobación",
  },
  authorization_signed: {
    todo: "Firmar la autorización",
    running: "Firmando la autorización…",
    done: "Autorización firmada",
    error: "No se pudo firmar la autorización",
  },
  payment_authorized: {
    todo: "Autorizar el pago",
    running: "Autorizando el pago…",
    done: "Pago autorizado",
    error: "No se pudo autorizar el pago",
  },
  order_confirmed: {
    todo: "Confirmar la orden",
    running: "Confirmando la orden…",
    done: "Orden confirmada",
    error: "No se pudo confirmar la orden",
  },
  delegation_consented: {
    todo: "Autorizar la delegación",
    running: "Esperando tu autorización",
    done: "Delegación autorizada",
    error: "No se pudo registrar la delegación",
  },
  task_activated: {
    todo: "Iniciar la compra delegada",
    running: "Iniciando la compra delegada…",
    done: "Compra delegada en marcha",
    error: "No se pudo iniciar la compra delegada",
  },
  reservation: {
    todo: "Reservar el uso único",
    running: "Reservando el uso único…",
    done: "Uso único reservado",
    error: "No se pudo reservar el uso único",
  },
  mandates_closed: {
    todo: "Cerrar la autorización",
    running: "Cerrando la autorización…",
    done: "Autorización cerrada por el agente",
    error: "No se pudo cerrar la autorización",
  },
}

export const CONTROL_ORDER: ControlKind[] = [
  "request",
  "selection",
  "policy_decision",
  "checkout_created",
  "approval",
  "authorization_signed",
  "payment_authorized",
  "order_confirmed",
]

/** Compra delegada: el consentimiento reemplaza al pedido y nadie aprueba ni firma al final. */
export const DELEGATED_CONTROL_ORDER: ControlKind[] = [
  "delegation_consented",
  "task_activated",
  "policy_decision",
  "checkout_created",
  "reservation",
  "mandates_closed",
  "payment_authorized",
  "order_confirmed",
]

/** Lo que una fila necesita de su compra: si es delegada (HNP) y su estado. */
export type StepContext = Pick<PurchaseTrace, "status" | "purchase_mode">

function delegated(context?: StepContext) {
  return context?.purchase_mode === "HNP"
}

export function controlOrder(context: StepContext) {
  return delegated(context) ? DELEGATED_CONTROL_ORDER : CONTROL_ORDER
}

const ORDER_EVENTS: Record<string, string> = {
  order_create: "El comercio registró la orden",
  order_update: "El comercio actualizó la orden",
}

/**
 * Por qué no se completó; la tarjeta de aprobación añade qué hacer después. `offer_expired` e
 * `invalid_item` solo llegan cuando el comercio rechazó crear el checkout: no se creó nada.
 */
export const FAILURE_COPY = {
  payment_declined: "El pago sandbox fue rechazado.",
  out_of_stock: "El producto se agotó antes de completar la compra.",
  offer_expired: "La oferta del comercio venció.",
  invalid_item: "El producto ya no está disponible en el comercio.",
} satisfies Record<string, string>

/** Lo que no pasó en una compra delegada que falló: el pago nunca salió (§8.1). */
const NO_CHARGE = "No se hizo ningún cobro."

/**
 * Causa de un fallo de la tarea delegada, solo si es cierta en todos los caminos que dan ese código
 * (`application/hnp_execution.py`). `checkout_failed` también cubre un update rechazado o un cierre
 * fallido con el checkout ya creado, y `governance_unavailable`, la frescura sin leer al cerrar:
 * ninguno nombra una causa. `policy_denied` solo sale de un deny de D2 o D3 por sus reglas.
 */
const DELEGATED_FAILURE: Record<string, string> = {
  policy_denied: "La política de compra no permitió la compra.",
}

function failureDetail(step: Extract<TraceStep, { kind: "failed" }>, context?: StepContext) {
  if (waitingForPolicy(step, context)) return WAITING_FOR_POLICY.detail
  const cause = lookup(FAILURE_COPY, step.code)
  if (!delegated(context)) return cause
  const reason = cause ?? lookup(DELEGATED_FAILURE, step.code) ?? "Por precaución, la compra se detuvo."
  return `${reason} ${NO_CHARGE}`
}

/** K15: D1 permitió sin su resultado de despacho; la tarea cerró la delegación sin cobrar. */
function waitingForPolicy(step: TraceStep, context?: StepContext) {
  return (
    delegated(context) &&
    context?.status === "uncertain" &&
    step.kind === "failed" &&
    step.code === "governance_unavailable"
  )
}

/** #93: la tarea delegada terminó con otro código que la compra (`failed:task`); van los dos. */
function failureTitle(step: Extract<TraceStep, { kind: "failed" }>, context?: StepContext) {
  if (waitingForPolicy(step, context)) return WAITING_FOR_POLICY.title
  return step.id === "failed:task" ? "La compra delegada se detuvo" : "La compra no se pudo completar"
}

const WAITING_FOR_POLICY = {
  title: "La compra espera la recuperación de la política de compra",
  detail: `La delegación quedó cerrada. ${NO_CHARGE}`,
  summary: "Por confirmar: recuperación pendiente",
}

const SCOPES = { checkout: "la compra", payment: "el pago" } as const

function scopeList(scopes: (keyof typeof SCOPES)[]) {
  if (scopes.length === 0) return null
  return new Intl.ListFormat("es", { type: "conjunction" }).format(scopes.map((s) => SCOPES[s]))
}

function controlTitle(kind: ControlKind, state: StepState) {
  const copy = CONTROLS[kind]
  if (state === "done") return copy.done
  if (state === "error") return copy.error
  if (state === "skipped") return copy.todo
  return copy.running
}

/**
 * La política respondió sin evaluar ninguna regla: se detuvo por precaución (fail closed). Sin
 * reglas no hay de dónde saberlo, así que no cuenta como «no disponible».
 */
export function policyUnavailable(decision: PolicyDecision) {
  return (
    decision.rules.length > 0 && decision.rules.every((rule) => rule.result === "not_evaluated")
  )
}

/**
 * La política permitió, pero el allow no se pudo dejar listo antes de crear el checkout (por ejemplo,
 * falló al guardarlo): el backend lo registra como bloqueado. Lo detuvo la precaución, no una regla.
 */
function allowNotApplied(decision: PolicyDecision) {
  return decision.outcome === "allow" && decision.effect === "blocked"
}

function policyTitle(state: StepState, decision: PolicyDecision) {
  if (state === "running" || state === "error" || state === "skipped") {
    return controlTitle("policy_decision", state)
  }
  if (allowNotApplied(decision)) return "La política permitió la compra; se detuvo por precaución"
  if (decision.effect === "blocked") {
    return policyUnavailable(decision)
      ? "Política no disponible: compra detenida por precaución"
      : "La política detuvo la compra"
  }
  return decision.outcome === "allow" ? CONTROLS.policy_decision.done : "Política de compra: no se cumplió"
}

// Autorización al aprobar (D2): la política revisa la compra otra vez antes de firmar nada.

type AuthorizationStep = Extract<TraceStep, { kind: "authorization_decision" }>

/** Qué no pasó cuando la autorización detuvo la compra: no hay aprobación registrada. */
export const NOTHING_SIGNED = "No se firmó ninguna autorización ni se hizo ningún cobro."

const AUTHORIZATION_TITLE = {
  running: "Verificando la autorización…",
  done: "Autorización verificada",
  denied: "La política no autorizó la compra",
  unverified: "No se pudo verificar la autorización",
  unapplied: "La política autorizó la compra; se detuvo por precaución",
}

/** Se detuvo sin una regla que falle: el motor no respondió, faltaron datos o no se aplicó. */
function authorizationUnverified(decision: PolicyDecision) {
  return decision.effect === "blocked" && !decision.rules.some((rule) => rule.result === "fail")
}

/** D2 en una compra delegada: la política revisa la delegación antes de reservar el uso único. */
const DELEGATED_AUTHORIZATION_TITLE = { ...AUTHORIZATION_TITLE, done: "Autorización delegada verificada" }

/** D3: la última revisión antes de enviar el pago. */
const DISPATCH_TITLE = {
  running: "Verificando el despacho…",
  done: "Despacho autorizado",
  denied: "La política no autorizó el despacho",
  unverified: "No se pudo verificar el despacho",
  unapplied: "La política autorizó el despacho; se detuvo por precaución",
}

type DecisionStep = Extract<TraceStep, { kind: "authorization_decision" | "dispatch_decision" }>

function decisionTitle(step: DecisionStep, titles: typeof AUTHORIZATION_TITLE) {
  if (step.state === "running") return titles.running
  const { decision } = step
  if (allowNotApplied(decision)) return titles.unapplied
  if (decision.effect === "blocked") {
    return authorizationUnverified(decision) ? titles.unverified : titles.denied
  }
  return decision.outcome === "allow" ? titles.done : titles.denied
}

function authorizationTitle(step: AuthorizationStep, context?: StepContext) {
  return decisionTitle(step, delegated(context) ? DELEGATED_AUTHORIZATION_TITLE : AUTHORIZATION_TITLE)
}

/** Qué no pasó cuando la política detuvo una compra delegada antes de reservar. */
const NOTHING_RESERVED = `No se reservó el uso único. ${NO_CHARGE}`

function authorizationDetail(step: AuthorizationStep, context?: StepContext) {
  if (delegated(context)) {
    return step.state === "blocked" ? NOTHING_RESERVED : "Antes de reservar el uso único"
  }
  return step.state === "blocked" ? NOTHING_SIGNED : "Antes de firmar la autorización"
}

function dispatchDetail(step: Extract<TraceStep, { kind: "dispatch_decision" }>) {
  return step.state === "blocked" ? `No se envió el pago. ${NO_CHARGE}` : "Antes de enviar el pago"
}

function lookup(table: Record<string, string>, key: string) {
  return Object.hasOwn(table, key) ? table[key] : null
}

// Controles de la aplicación del flujo HP gobernado. Nunca se nombran como política ni como regla.

type FreshnessStep = Extract<TraceStep, { kind: "freshness_check" }>

const FRESHNESS_TITLE: Record<FreshnessStep["outcome"], string> = {
  fresh: "Condiciones confirmadas con el comercio",
  changed: "Las condiciones del comercio cambiaron",
  unavailable: "No se pudieron confirmar las condiciones con el comercio",
}

const FRESHNESS_RUNNING = "Confirmando las condiciones con el comercio…"

/** El comercio respondió que la oferta venció: no es una lectura fallida ni una precaución. */
const FRESHNESS_EXPIRED = "La oferta del comercio venció"

function offerExpired(step: FreshnessStep) {
  return step.outcome === "unavailable" && step.read_error === "offer_expired"
}

const FRESHNESS_WHEN: Record<FreshnessStep["stage"], string> = {
  present: "Antes de mostrarte los términos",
  approve: "Antes de registrar tu aprobación",
  close: "Antes de cerrar la compra delegada",
}

/**
 * Qué no pasó cuando un cambio detuvo la compra. Un solo texto para las dos etapas: al pulsar
 * «Aprobar», el chat relee los términos (etapa `present`) antes de enviar la aprobación, así que una
 * parada en `present` puede llegar después de mostrarlos. Lo que sí vale en las dos es esto.
 */
const FRESHNESS_STOPPED = "No se registró ninguna aprobación ni se firmó ninguna autorización."

const FRESHNESS_PRECAUTION = "Por precaución, la compra se detuvo."

function freshnessTitle(step: FreshnessStep) {
  if (step.state === "running") return FRESHNESS_RUNNING
  if (offerExpired(step)) return FRESHNESS_EXPIRED
  return step.stage === "close" && step.outcome === "fresh"
    ? "Oferta vigente al cerrar"
    : FRESHNESS_TITLE[step.outcome]
}

function freshnessDetail(step: FreshnessStep) {
  if (step.state === "blocked") {
    if (step.outcome === "unavailable" && !offerExpired(step)) return FRESHNESS_PRECAUTION
    return step.stage === "close" ? NOTHING_RESERVED : FRESHNESS_STOPPED
  }
  // En modo observando un cambio no detiene nada: se dice, igual que en la política.
  if (step.mode === "shadow" && step.outcome !== "fresh") return POLICY_MODE_LABEL.shadow
  return step.state === "done" ? FRESHNESS_WHEN[step.stage] : null
}

export const FRESHNESS_CHANGES_LABEL = "Qué cambió"

type ValueLabels = { presented: string; current: string }

/** Rótulos de los dos valores: los términos del checkout frente a la oferta que lee el agente. */
const FRESHNESS_VALUE_LABEL: ValueLabels = {
  presented: "En el checkout",
  current: "Oferta actual",
}

const FRESHNESS_FIELD: Record<FreshnessField, string> = {
  subtotal_minor: "Precio del producto",
  shipping_minor: "Envío",
  total_minor: "Total",
  currency: "Moneda",
  expires_at: "Vigencia de la oferta",
  delivery_days: "Días de entrega",
  stock: "Disponibilidad",
}

/** El checkout no guarda existencias: guarda la cantidad pedida, que se compara con las disponibles. */
const FIELD_VALUE_LABEL: Partial<Record<FreshnessField, ValueLabels>> = {
  stock: { presented: "Unidades pedidas", current: "Unidades disponibles" },
}

const MONEY_FIELDS: ReadonlySet<FreshnessField> = new Set([
  "subtotal_minor",
  "shipping_minor",
  "total_minor",
])

const UNKNOWN_VALUE = "Sin dato"

function changeValue(change: FreshnessChange, side: "presented" | "current", currency: string | null) {
  const value = change[side]
  if (value === null) return UNKNOWN_VALUE
  if (typeof value === "string") return value
  if (MONEY_FIELDS.has(change.field) && currency) return formatMoney(value, currency)
  if (change.field === "delivery_days") return value === 1 ? "1 día" : `${value} días`
  return String(value)
}

/** Vigencias con segundos; si aun así se leen iguales, también con milisegundos. */
function expiryValues(change: Extract<FreshnessChange, { field: "expires_at" }>) {
  const write = (value: string | null, milliseconds: boolean) =>
    value === null ? UNKNOWN_VALUE : (formatPreciseDateTime(value, { milliseconds }) ?? value)
  const presented = write(change.presented, false)
  const current = write(change.current, false)
  if (presented !== current) return { presented, current }
  return { presented: write(change.presented, true), current: write(change.current, true) }
}

/** Cada cambio con su nombre, sus rótulos y los dos valores ya escritos, para la lista de la fila. */
export function freshnessChanges(step: FreshnessStep) {
  // Los montos de la oferta actual van en su moneda si también cambió.
  const currencyChange = step.changes.find((change) => change.field === "currency")
  const current =
    typeof currencyChange?.current === "string" ? currencyChange.current : step.currency
  return step.changes.map((change) => ({
    field: change.field,
    name: FRESHNESS_FIELD[change.field],
    labels: FIELD_VALUE_LABEL[change.field] ?? FRESHNESS_VALUE_LABEL,
    ...(change.field === "expires_at"
      ? expiryValues(change)
      : {
          presented: changeValue(change, "presented", step.currency),
          current: changeValue(change, "current", current),
        }),
  }))
}

type RecoveryStep = Extract<TraceStep, { kind: "governed_recovery" }>

const RECOVERY_TITLE = "El agente explicó por qué no siguió"

const RECOVERY_CAUSE: Record<RecoveryCause, string> = {
  budget: "Superaba tu presupuesto",
  unavailable: "El producto no estaba disponible",
  currency: "El producto no se vende en la moneda que pediste",
  category: "El producto no es de la categoría que pediste",
  condition: "El producto no está en la condición que pediste",
  option: "El producto no tiene las características que pediste",
  term: "El producto no coincide con lo que buscas",
  not_shortlisted: "La opción elegida no estaba entre las verificadas para tu pedido",
  // La lista corta sale del catálogo: la causa es la elección del modelo, no el catálogo.
  not_in_catalog: "El modelo propuso una opción que no estaba entre las verificadas para tu pedido",
  no_match: "Ningún producto del catálogo cumple todo lo que pediste",
}

/** Lo que no ocurrió: con el checkout ya creado, el agente lo canceló. */
const RECOVERY_OUTCOME: Record<GovernedRecovery["recovery"], string> = {
  no_candidate: "No se creó ningún checkout.",
  selection_outside_shortlist: "No se creó ningún checkout.",
  budget_exceeded_after_checkout: "Se canceló el checkout antes de cobrar.",
}

function recoveryDetail(step: RecoveryStep) {
  const budget =
    step.cause === "budget" && step.budget_minor !== null && step.currency
      ? ` de ${formatMoney(step.budget_minor, step.currency)}`
      : ""
  return `${RECOVERY_CAUSE[step.cause]}${budget}. ${RECOVERY_OUTCOME[step.recovery]}`
}

// Compra delegada (HNP). Las causas del escalamiento son las de §8.3, como en el chat.

const ESCALATION_CAUSE: Record<EscalationReason, string> = {
  budget_not_satisfiable:
    "El precio de catálogo de la opción disponible ya supera el total máximo que autorizaste.",
  no_candidate_in_scope: "Ninguna de las opciones que autorizaste está disponible ahora.",
  offer_changed: "La oferta del comercio cambió antes de que cerrara la compra.",
  terms_out_of_scope:
    "El total del comercio, con envío, o algún término quedó fuera de lo que autorizaste.",
  authorization_profile_unresolved: "No pude comprobar que la compra encaje en tu autorización.",
  verifier_unresolved_constraint:
    "El proveedor de pagos no pudo comprobar una condición de tu autorización.",
  verifier_mandates_not_supported: "El proveedor de pagos no acepta compras delegadas en este momento.",
}

type RevokedStep = Extract<TraceStep, { kind: "revoked" }>

const REVOKED_BY: Record<RevokedStep["by"], string> = {
  owner: "Revocaste la delegación",
  operator: "El operador detuvo las compras delegadas",
}

const REVOKED_EFFECT: Record<RevokedStep["effect"], string> = {
  no_effect: `. ${NO_CHARGE}`,
  released: ` antes de que el agente comprometiera el pago. ${NO_CHARGE}`,
  in_flight: ", pero el agente ya había comprometido el pago: esa compra sigue en curso.",
}

type ReservationStep = Extract<TraceStep, { kind: "reservation" }>

const RESERVATION_TITLE: Record<ReservationStep["outcome"], string> = {
  reserved: CONTROLS.reservation.done,
  released: "Uso único liberado",
  consumed: "Uso único consumido",
  uncertain: "Uso único por confirmar",
}

function reservationDetail(step: ReservationStep) {
  const amount = formatMoney(step.amount.minor, step.amount.currency)
  return step.outcome === "released" ? `Se liberaron ${amount}. ${NO_CHARGE}` : `Por ${amount}`
}

function consentDetail(step: Extract<TraceStep, { kind: "delegation_consented" }>) {
  const total = `Hasta ${formatMoney(step.total_max.minor, step.total_max.currency)}`
  const expires = formatDateTime(step.expires_at)
  return expires ? `${total} · vence ${expires}` : total
}

function canceledTitle(step: Extract<TraceStep, { kind: "canceled" }>) {
  if (step.by === "expiry") return "La delegación venció"
  return step.by === "budget" ? "Compra cancelada: superaba tu presupuesto" : "Cancelaste la compra"
}

/** Un segundo `task_activated` es el servicio retomando la misma tarea. */
function activationTitle(step: Extract<TraceStep, { kind: "task_activated" }>) {
  const resumed = step.state === "done" && step.id !== "task_activated"
  return resumed ? "Compra delegada reanudada" : controlTitle(step.kind, step.state)
}

export function stepTitle(step: TraceStep, context?: StepContext) {
  switch (step.kind) {
    case "freshness_check":
      return freshnessTitle(step)
    case "governed_recovery":
      return RECOVERY_TITLE
    case "policy_decision":
      return policyTitle(step.state, step.decision)
    case "authorization_decision":
      return authorizationTitle(step, context)
    case "dispatch_decision":
      return decisionTitle(step, DISPATCH_TITLE)
    case "order_event":
      return lookup(ORDER_EVENTS, step.event_type) ?? "Aviso del comercio"
    case "canceled":
      return canceledTitle(step)
    case "failed":
      return failureTitle(step, context)
    case "task_activated":
      return activationTitle(step)
    case "reservation":
      return step.state === "done" ? RESERVATION_TITLE[step.outcome] : controlTitle(step.kind, step.state)
    case "revoked":
      return "Delegación revocada"
    case "escalated":
      return "Necesito tu decisión"
    default:
      return controlTitle(step.kind, step.state)
  }
}

function approvalDetail(step: Extract<TraceStep, { kind: "approval" }>) {
  if (step.state === "waiting") {
    const expires = formatTime(step.expires_at)
    return expires ? `Vence a las ${expires}` : null
  }
  const approved = step.approved_at ? formatTime(step.approved_at) : null
  return approved ? `A las ${approved}` : null
}

/**
 * Línea corta bajo el título; los identificadores van aparte, en el detalle técnico. El checkout
 * dice en qué comercio se creó, por su nombre.
 */
export function stepDetail(
  step: TraceStep,
  merchantName: string | null = null,
  context?: StepContext,
) {
  switch (step.kind) {
    case "selection":
      return `${step.product} · ${formatMoney(step.total_minor, step.currency)}`
    case "checkout_created":
      return merchantName
    case "policy_decision":
      return POLICY_MODE_LABEL[step.decision.mode]
    case "approval":
      return approvalDetail(step)
    case "authorization_decision":
      return authorizationDetail(step, context)
    case "dispatch_decision":
      return dispatchDetail(step)
    case "authorization_signed": {
      const scopes = scopeList(step.mandates)
      return scopes && `Para ${scopes}`
    }
    case "payment_authorized":
      return `Hasta ${formatMoney(step.max_minor, step.currency)}`
    case "order_confirmed": {
      const scopes = scopeList(step.receipts)
      return scopes && `Recibos de ${scopes}`
    }
    case "failed":
      return failureDetail(step, context)
    case "freshness_check":
      return freshnessDetail(step)
    case "governed_recovery":
      return recoveryDetail(step)
    case "delegation_consented":
      return consentDetail(step)
    case "task_activated":
      return "Sin tu intervención"
    case "reservation":
      return reservationDetail(step)
    case "mandates_closed":
      return "Dentro de lo que autorizaste"
    case "revoked":
      return `${REVOKED_BY[step.by]}${REVOKED_EFFECT[step.effect]}`
    case "escalated":
      return `${ESCALATION_CAUSE[step.reason]} ${NO_CHARGE}`
    case "canceled":
      return step.by === "expiry" ? `Venció antes de que el agente comprara. ${NO_CHARGE}` : null
    default:
      return null
  }
}

/** Control que la traza no trae porque la compra no lo alcanzó o la política estaba apagada. */
export type SkippedControl = { kind: ControlKind; reason: "not_reached" | "policy_off" }

/** Un paso de la traza siempre trae `id`; `reason` no basta, porque `escalated` también lo trae. */
export function isSkipped(row: TraceStep | SkippedControl): row is SkippedControl {
  return !("id" in row)
}

export function skippedTitle(control: SkippedControl) {
  return control.reason === "policy_off" ? POLICY_MODE_LABEL.off : CONTROLS[control.kind].todo
}

export function skippedDetail(control: SkippedControl) {
  return control.reason === "policy_off" ? "No se revisó esta compra" : null
}

/** Cómo se nombra cada estado de la compra en el chat: la fila de actividad y el paso de seguimiento. */
const PURCHASE_SUMMARY: Record<PurchaseStatus, string> = {
  in_progress: "Comprando…",
  awaiting_approval: "Esperando tu aprobación",
  completed: "Compra completada",
  blocked: "Bloqueada por la política de compra",
  stale_terms: "Detenida: cambiaron las condiciones",
  canceled: "Compra cancelada",
  failed: "La compra no se pudo completar",
  uncertain: "Compra por confirmar con el comercio",
}

// Cortas: en un teléfono de 375 px el resumen no se recorta (historia Phone375).
const STALE_UNAVAILABLE_SUMMARY = "Detenida: condiciones sin confirmar"
const STALE_EXPIRED_SUMMARY = "Detenida: la oferta venció"
const ALLOW_NOT_APPLIED_SUMMARY = "Detenida por precaución"

/** Línea de la fila de actividad en el chat. */
export function purchaseSummary(trace: PurchaseTrace) {
  if (trace.steps.some((step) => waitingForPolicy(step, trace))) return WAITING_FOR_POLICY.summary
  if (trace.status === "stale_terms") {
    const check = trace.steps.findLast((step) => step.kind === "freshness_check")
    if (check?.kind !== "freshness_check" || check.outcome !== "unavailable") {
      return PURCHASE_SUMMARY.stale_terms
    }
    return offerExpired(check) ? STALE_EXPIRED_SUMMARY : STALE_UNAVAILABLE_SUMMARY
  }
  if (trace.status === "blocked") {
    const policy = trace.steps.findLast(
      (step) =>
        step.kind === "policy_decision" ||
        step.kind === "authorization_decision" ||
        step.kind === "dispatch_decision",
    )
    if (policy && policy.kind !== "policy_decision" && authorizationUnverified(policy.decision)) {
      return ALLOW_NOT_APPLIED_SUMMARY
    }
    return policy?.kind === "policy_decision" && allowNotApplied(policy.decision)
      ? ALLOW_NOT_APPLIED_SUMMARY
      : PURCHASE_SUMMARY.blocked
  }
  if (trace.status !== "completed") return PURCHASE_SUMMARY[trace.status]
  // Hasta la orden confirmada: un aviso del comercio que llega después mueve `updated_at`.
  const confirmedAt = trace.steps.find((step) => step.kind === "order_confirmed")?.at
  const elapsed = confirmedAt
    ? formatElapsed(Date.parse(confirmedAt) - Date.parse(trace.started_at))
    : null
  return elapsed ? `${PURCHASE_SUMMARY.completed} en ${elapsed}` : PURCHASE_SUMMARY.completed
}

// Etapas de SQLite (`state.purchase`, paso `purchase_update`). Las que coinciden con un estado de la
// traza usan sus mismas palabras; las intermedias dicen qué paso de la compra ya ocurrió.
const STAGE_LABEL: Record<PurchaseStage, string> = {
  received: CONTROLS.request.done,
  selected: CONTROLS.selection.done,
  checkout_prepared: CONTROLS.checkout_created.done,
  awaiting_confirmation: PURCHASE_SUMMARY.awaiting_approval,
  approved: "Aprobación registrada",
  completing: "Completando la compra",
  uncertain: PURCHASE_SUMMARY.uncertain,
  completed: PURCHASE_SUMMARY.completed,
  canceled: PURCHASE_SUMMARY.canceled,
  failed: PURCHASE_SUMMARY.failed,
}

export function purchaseStageLabel(stage: string) {
  return Object.hasOwn(STAGE_LABEL, stage) ? STAGE_LABEL[stage as PurchaseStage] : stage
}

/** Identificadores de una decisión para el detalle técnico: aquí sí van los códigos internos. */
export function decisionRefs(decision: PolicyDecision): EvidenceRef[] {
  const failedRules = decision.rules.filter((rule) => rule.result === "fail").map((rule) => rule.id)
  return [
    { label: "Decisión", value: decision.decision_id },
    { label: "Evidencia", value: decision.evidence_id },
    { label: "Política", value: `${decision.policy.set_id} · revisión ${decision.policy.revision}` },
    ...listRef("Reglas que fallaron", failedRules),
    ...listRef("Códigos", decision.reason_codes),
  ]
}

function listRef(label: string, values: string[]): EvidenceRef[] {
  return values.length > 0 ? [{ label, value: values.join(", ") }] : []
}

const FRESHNESS_STAGE_REF: Record<FreshnessStep["stage"], string> = {
  present: "al presentar",
  approve: "al aprobar",
  close: "al cerrar",
}

/** Decisión al aprobar (D2) o, en una compra delegada, antes de reservar (D2) y de despachar (D3). */
function laterDecisionRefs(decision: PolicyDecision, moment: string): EvidenceRef[] {
  const failed = decision.rules.filter((rule) => rule.result === "fail").map((rule) => rule.id)
  return [
    { label: `Decisión ${moment}`, value: decision.decision_id },
    { label: `Evidencia ${moment}`, value: decision.evidence_id },
    ...listRef(`Reglas que fallaron ${moment}`, failed),
    ...listRef(`Códigos ${moment}`, decision.reason_codes),
  ]
}

function stepRefs(step: TraceStep, context: StepContext): EvidenceRef[] {
  switch (step.kind) {
    case "freshness_check": {
      // Aquí sí van los códigos: etapa, resultado, modo y, si la hay, la revisión de la oferta.
      const codes = [step.stage, step.outcome, step.mode, step.read_error]
      const revision = step.offer_revision === null ? [] : [`revisión ${step.offer_revision}`]
      const label = `Frescura ${FRESHNESS_STAGE_REF[step.stage]}`
      return [
        { label, value: [...codes.filter(Boolean), ...revision].join(" · ") },
        { label: `Evidencia de frescura ${FRESHNESS_STAGE_REF[step.stage]}`, value: step.evidence_digest },
      ]
    }
    case "governed_recovery":
      return [
        { label: "Recuperación", value: `${step.recovery} · ${step.cause}` },
        ...(step.variant_id ? [{ label: "Variante", value: step.variant_id }] : []),
        { label: "Evidencia de recuperación", value: step.evidence_digest },
      ]
    case "authorization_decision":
      // Aquí sí van los códigos de la decisión D2.
      return laterDecisionRefs(step.decision, delegated(context) ? "antes de reservar" : "al aprobar")
    case "dispatch_decision":
      return laterDecisionRefs(step.decision, "antes de despachar")
    case "delegation_consented":
      return [{ label: "Delegación", value: step.grant_id }]
    case "escalated":
      return [{ label: "Escalamiento", value: step.reason }]
    case "checkout_created":
      return [{ label: "Checkout", value: step.checkout_id }]
    case "order_confirmed":
      return [{ label: "Orden", value: step.order_id }]
    case "failed":
      return [{ label: "Código de error", value: step.code }]
    default:
      return []
  }
}

export function traceRefs(trace: PurchaseTrace): EvidenceRef[] {
  // Si la compra pasó más de una vez por la política, la que cuenta es la última decisión.
  const last = trace.steps.findLast((step) => step.kind === "policy_decision")
  return [
    { label: "Compra", value: trace.run_id },
    ...(last?.kind === "policy_decision" ? decisionRefs(last.decision) : []),
    ...trace.steps.flatMap((step) => stepRefs(step, trace)),
  ]
}

export const TECHNICAL_DETAILS_TITLE = "Detalle técnico"

export const EVIDENCE_EMPTY = "Esta compra todavía no tiene identificadores."

export function copyEvidenceLabel(label: string) {
  return `Copiar ${label.toLocaleLowerCase("es")}`
}

export const COPY_FEEDBACK = {
  idle: "",
  copied: "Copiado",
  failed: "No se pudo copiar",
}

// Tarjeta de bloqueo. Las decisiones de política ocurren antes de crear el checkout (D1).

/** Cómo la política detuvo la compra según el código del stream, antes de que llegue la traza. */
export type PolicyBlockReason = "denied" | "unavailable" | "allow_unapplied"

export const NOTHING_CHARGED = "No se creó ningún checkout ni se hizo ningún cobro."

const PRECAUTION = "Ante la duda, la compra se detiene antes de llegar al comercio."

const BLOCKED_TITLE = {
  denied: "La política de compra detuvo esta compra",
  unavailable: "No se pudo verificar la política de compra",
  allow_unapplied: "La política de compra permitió esta compra",
} as const
const STOPPED_BY_PRECAUTION = "Por precaución, la compra se detuvo."
const ALLOW_UNAPPLIED = `No se pudo dejar lista para crear el checkout. ${PRECAUTION}`

/**
 * Sin reglas fallidas la política no pudo verificar la compra: no respondió (ninguna regla
 * evaluada) o la revisión no terminó. En ambos casos se detuvo por precaución, no por una regla.
 */
export function blockedCardCopy(decision: PolicyDecision, failedCount: number) {
  if (allowNotApplied(decision)) {
    // El mismo relato que el chat (`d1_allow_unapplied`): permitió, pero se detuvo por precaución.
    return {
      title: BLOCKED_TITLE.allow_unapplied,
      subtitle: STOPPED_BY_PRECAUTION,
      explanation: ALLOW_UNAPPLIED,
    }
  }
  if (failedCount === 0) {
    const cause = policyUnavailable(decision)
      ? "La política no estaba disponible para revisar esta compra."
      : "La revisión de la política no se completó."
    return {
      title: BLOCKED_TITLE.unavailable,
      subtitle: STOPPED_BY_PRECAUTION,
      explanation: `${cause} ${PRECAUTION}`,
    }
  }
  return {
    title: BLOCKED_TITLE.denied,
    subtitle:
      failedCount === 1 ? "No se cumplió una regla." : `No se cumplieron ${failedCount} reglas.`,
    explanation: null,
  }
}

/**
 * La misma tarjeta antes de la traza, con lo que dice el código del stream (`d1_denied`,
 * `d1_unavailable`, `d1_allow_unapplied`). El título es el que tendrá con la traza, así no cambia
 * al llegar. Lo que solo sabe la traza (qué regla, por qué no respondió) queda en `null`: la tarjeta
 * lo muestra en esqueleto mientras la espera y, si no llega, lo dice con `missing`.
 */
export function streamBlockCopy(reason: PolicyBlockReason) {
  switch (reason) {
    case "denied":
      return {
        title: BLOCKED_TITLE.denied,
        subtitle: null,
        explanation: null,
        missing: "No pude mostrar qué regla no se cumplió.",
      }
    case "unavailable":
      return {
        title: BLOCKED_TITLE.unavailable,
        subtitle: STOPPED_BY_PRECAUTION,
        explanation: null,
        missing: PRECAUTION,
      }
    case "allow_unapplied":
      return {
        title: BLOCKED_TITLE.allow_unapplied,
        subtitle: STOPPED_BY_PRECAUTION,
        explanation: ALLOW_UNAPPLIED,
        missing: ALLOW_UNAPPLIED,
      }
  }
}

export function policyModeNote(mode: PolicyDecision["mode"]) {
  return `Política de compra · ${POLICY_MODE_LABEL[mode]}`
}

/** «Aprobado por ti · 10:42 · autorización firmada». */
export function approvalReceiptText(approvedAt: string, authorizationSigned: boolean) {
  const signed = CONTROLS.authorization_signed.done.toLocaleLowerCase("es")
  return ["Aprobado por ti", formatTime(approvedAt), authorizationSigned ? signed : null]
    .filter(Boolean)
    .join(" · ")
}
