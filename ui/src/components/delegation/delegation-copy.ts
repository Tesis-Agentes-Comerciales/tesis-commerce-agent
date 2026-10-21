import type { DelegationView } from "@/lib/delegation-contract"

// Textos fijos de las tarjetas de delegación (docs/design/hnp-governed.md §8.3, HNP-07). Los
// mensajes de la tarea, del escalamiento y de la revocación no están aquí: llegan escritos del
// backend en `message` (`HNP_MESSAGES`) y la UI los muestra tal cual.

export const CONSENT_COPY = {
  title: "Autorizar una compra sin ti",
  totalIncludesShipping: "El total máximo incluye envío",
  singleUse: "Un solo uso",
  revocable: "Puedes revocarla antes de que el agente comprometa el pago",
  authorize: "Autorizar",
  cancel: "Cancelar",
} as const

/** «Vence el <fecha y hora>». */
export const expiresOn = (dateTime: string) => `Vence el ${dateTime}`

export const SCOPE_LABEL = {
  total: "Total máximo",
  expires: "Vence",
  destination: "Entrega",
  instrument: "Pago",
  lastActivity: "Última actividad",
  preference: "Tu indicación",
} as const

/** §2.1, «Mis delegaciones»: Revocar. */
export const REVOKE_LABEL = "Revocar"
/** §8.2: el CTA del escalamiento prellena la compra HP en el chat. */
export const BUY_FROM_CHAT_LABEL = "Comprar desde el chat"

/**
 * Lo que la tarjeta dice de una delegación. Manda el resultado de la tarea; mientras la tarea no
 * termina, manda el grant (revocada o vencida aunque la tarea siga en curso). El escalamiento no
 * tiene título propio: usa `escalation.title` del backend.
 */
export type DelegationStatus =
  | "scheduled"
  | "running"
  | "purchased"
  | "escalated"
  | "revoked"
  | "kill_switch"
  | "expired"
  | "failed"
  | "uncertain"

const DELEGATION_STATUS_TITLE: Record<Exclude<DelegationStatus, "escalated">, string> = {
  scheduled: "Delegación activa",
  running: "Compra en curso",
  purchased: "Compra completada",
  revoked: "Delegación revocada",
  kill_switch: "Compra cancelada",
  expired: "Delegación vencida",
  failed: "Compra no completada",
  uncertain: "Compra por confirmar",
}

export function delegationStatus({
  state,
  revoked_reason,
  task,
  escalation,
}: DelegationView): DelegationStatus {
  switch (task.state) {
    case "canceled":
      if (task.outcome_code === "kill_switch") return "kill_switch"
      return task.outcome_code === "grant_expired" ? "expired" : "revoked"
    case "escalated":
      // El contrato exige la vista con la tarea escalada; sin ella no hay qué decidir.
      return escalation ? "escalated" : "failed"
    case "scheduled":
    case "running":
      if (state === "expired") return "expired"
      if (state === "revoked") return revoked_reason === "operator" ? "kill_switch" : "revoked"
      return task.state
    default:
      return task.state
  }
}

export function delegationTitle(status: DelegationStatus, { escalation }: DelegationView) {
  if (status !== "escalated") return DELEGATION_STATUS_TITLE[status]
  return escalation?.title ?? DELEGATION_STATUS_TITLE.failed
}
