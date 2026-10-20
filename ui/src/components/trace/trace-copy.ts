import { formatMoney, formatTime } from "@/components/agent/agent-format"
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
