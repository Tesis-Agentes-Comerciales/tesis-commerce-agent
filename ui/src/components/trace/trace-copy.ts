import { formatElapsed, formatMoney, formatTime } from "@/components/agent/agent-format"
import type { EvidenceRef } from "@/components/trace/evidence-refs"
import type {
  PolicyDecision,
  PolicyRule,
  PurchaseStatus,
  PurchaseTrace,
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
  blocked: "Detenido por la política",
  error: "Falló",
  skipped: "No se ejecutó",
}

export const PURCHASE_STATUS_LABEL: Record<PurchaseStatus, string> = {
  in_progress: "En curso",
  awaiting_approval: "Esperando tu aprobación",
  completed: "Completada",
  blocked: "Bloqueada por la política",
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

/** Controles del recorrido normal de una compra, en orden; los eventos sueltos no cuentan. */
export type ControlKind = Exclude<StepKind, "order_event" | "canceled" | "failed">

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

const FAILURES: Record<string, string> = {
  payment_declined: "El comercio rechazó el pago.",
  out_of_stock: "El producto se agotó antes de completar la compra.",
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

/** La política no llegó a evaluar ninguna regla: se detuvo por precaución (fail closed). */
export function policyUnavailable(decision: PolicyDecision) {
  return decision.rules.every((rule) => rule.result === "not_evaluated")
}

function policyTitle(state: StepState, decision: PolicyDecision) {
  if (state === "running" || state === "error" || state === "skipped") {
    return controlTitle("policy_decision", state)
  }
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

export function stepTitle(step: TraceStep) {
  switch (step.kind) {
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

/** Línea corta bajo el título; los identificadores van aparte, en el detalle técnico. */
export function stepDetail(step: TraceStep) {
  switch (step.kind) {
    case "selection":
      return `${step.product} · ${formatMoney(step.total_minor, step.currency)}`
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
      return lookup(FAILURES, step.code)
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

const PURCHASE_SUMMARY: Record<Exclude<PurchaseStatus, "completed">, string> = {
  in_progress: "Comprando…",
  awaiting_approval: "Esperando tu aprobación",
  blocked: "Bloqueada por la política de compra",
  canceled: "Compra cancelada",
  failed: "La compra no se pudo completar",
  uncertain: "Compra por confirmar con el comercio",
}

/** Línea de la fila de actividad en el chat. */
export function purchaseSummary(trace: PurchaseTrace) {
  if (trace.status !== "completed") return PURCHASE_SUMMARY[trace.status]
  // Hasta la orden confirmada: un aviso del comercio que llega después mueve `updated_at`.
  const confirmedAt = trace.steps.find((step) => step.kind === "order_confirmed")?.at
  const elapsed = confirmedAt
    ? formatElapsed(Date.parse(confirmedAt) - Date.parse(trace.started_at))
    : null
  return elapsed ? `Compra completada en ${elapsed}` : "Compra completada"
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

function stepRefs(step: TraceStep): EvidenceRef[] {
  switch (step.kind) {
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
