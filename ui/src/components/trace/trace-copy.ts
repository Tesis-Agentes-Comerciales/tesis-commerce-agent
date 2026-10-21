import {
  formatElapsed,
  formatMoney,
  formatPreciseDateTime,
  formatTime,
} from "@/components/agent/agent-format"
import type { EvidenceRef } from "@/components/trace/evidence-refs"
import type { PurchaseStage } from "@/lib/agent-contract"
import type {
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
}

const OTHER_RULE: RuleCopy = {
  name: "Otra regla de la política",
  observed: "Valor observado",
  expected: "Valor esperado",
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
 * la frescura ni la recuperación: solo existen en el flujo gobernado, y una fila «No se ejecutó»
 * suya mentiría con la gobernanza apagada.
 */
export type ControlKind = Exclude<
  StepKind,
  "order_event" | "canceled" | "failed" | "freshness_check" | "governed_recovery"
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
}

export const CONTROL_ORDER = Object.keys(CONTROLS) as ControlKind[]

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
  return offerExpired(step) ? FRESHNESS_EXPIRED : FRESHNESS_TITLE[step.outcome]
}

function freshnessDetail(step: FreshnessStep) {
  if (step.state === "blocked") {
    return step.outcome === "unavailable" && !offerExpired(step)
      ? FRESHNESS_PRECAUTION
      : FRESHNESS_STOPPED
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

export function stepTitle(step: TraceStep) {
  switch (step.kind) {
    case "freshness_check":
      return freshnessTitle(step)
    case "governed_recovery":
      return RECOVERY_TITLE
    case "policy_decision":
      return policyTitle(step.state, step.decision)
    case "order_event":
      return lookup(ORDER_EVENTS, step.event_type) ?? "Aviso del comercio"
    case "canceled":
      return step.by === "budget" ? "Compra cancelada: superaba tu presupuesto" : "Cancelaste la compra"
    case "failed":
      return "La compra no se pudo completar"
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
export function stepDetail(step: TraceStep, merchantName: string | null = null) {
  switch (step.kind) {
    case "selection":
      return `${step.product} · ${formatMoney(step.total_minor, step.currency)}`
    case "checkout_created":
      return merchantName
    case "policy_decision":
      return POLICY_MODE_LABEL[step.decision.mode]
    case "approval":
      return approvalDetail(step)
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
      return lookup(FAILURE_COPY, step.code)
    case "freshness_check":
      return freshnessDetail(step)
    case "governed_recovery":
      return recoveryDetail(step)
    default:
      return null
  }
}

/** Control que la traza no trae porque la compra no lo alcanzó o la política estaba apagada. */
export type SkippedControl = { kind: ControlKind; reason: "not_reached" | "policy_off" }

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
  if (trace.status === "stale_terms") {
    const check = trace.steps.findLast((step) => step.kind === "freshness_check")
    if (check?.kind !== "freshness_check" || check.outcome !== "unavailable") {
      return PURCHASE_SUMMARY.stale_terms
    }
    return offerExpired(check) ? STALE_EXPIRED_SUMMARY : STALE_UNAVAILABLE_SUMMARY
  }
  if (trace.status === "blocked") {
    const policy = trace.steps.findLast((step) => step.kind === "policy_decision")
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
}

function stepRefs(step: TraceStep): EvidenceRef[] {
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
    ...trace.steps.flatMap(stepRefs),
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

export const NOTHING_CHARGED = "No se creó ningún checkout ni se hizo ningún cobro."

const PRECAUTION = "Ante la duda, la compra se detiene antes de llegar al comercio."

/**
 * Sin reglas fallidas la política no pudo verificar la compra: no respondió (ninguna regla
 * evaluada) o la revisión no terminó. En ambos casos se detuvo por precaución, no por una regla.
 */
export function blockedCardCopy(decision: PolicyDecision, failedCount: number) {
  if (allowNotApplied(decision)) {
    // El mismo relato que el chat (`d1_allow_unapplied`): permitió, pero se detuvo por precaución.
    return {
      title: "La política de compra permitió esta compra",
      subtitle: "Por precaución, la compra se detuvo.",
      explanation: `No se pudo dejar lista para crear el checkout. ${PRECAUTION}`,
    }
  }
  if (failedCount === 0) {
    const cause = policyUnavailable(decision)
      ? "La política no estaba disponible para revisar esta compra."
      : "La revisión de la política no se completó."
    return {
      title: "No se pudo verificar la política de compra",
      subtitle: "Por precaución, la compra se detuvo.",
      explanation: `${cause} ${PRECAUTION}`,
    }
  }
  return {
    title: "La política de compra detuvo esta compra",
    subtitle:
      failedCount === 1 ? "No se cumplió una regla." : `No se cumplieron ${failedCount} reglas.`,
    explanation: null,
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
