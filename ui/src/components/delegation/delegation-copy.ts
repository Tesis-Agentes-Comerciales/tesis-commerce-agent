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
/** Hasta cuándo vale la propuesta: después, «Autorizar» responde 409 (HNP-10). */
export const authorizeBy = (dateTime: string) => `Puedes autorizarla hasta el ${dateTime}`

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

// Textos de «Delegaciones» en el chat (HNP-10): el formulario de la propuesta y la lista.

export const FORM_COPY = {
  title: "Delegar una compra",
  subtitle: "El agente compra una sola vez, dentro de lo que autorices.",
  offers: "Opciones que puede comprar",
  noOffers:
    "Primero busca en el chat lo que quieres comprar: aquí aparecen las opciones que te mostré.",
  catalogPrice: (amount: string) => `Catálogo: ${amount}`,
  total: "Total máximo con envío (pesos)",
  validity: "Vigencia",
  destination: "Entrega",
  destinationHint: "Por ahora el comercio solo entrega en Bogotá D.C.",
  addressLine: "Dirección",
  city: "Ciudad",
  state: "Departamento",
  preference: "Tu indicación (opcional)",
  submit: "Preparar la autorización",
  cancel: "Cancelar",
} as const

/** Vigencias que ofrece el formulario; el servidor admite hasta `AGENT_HNP_MAX_VALIDITY_SECONDS`. */
export const VALIDITY_OPTIONS = [
  { seconds: 3_600, label: "1 hora" },
  { seconds: 21_600, label: "6 horas" },
  { seconds: 86_400, label: "24 horas" },
] as const

/** Encabezado de la traza en la tarjeta: la misma fila de actividad que la compra en el chat. */
export const GOVERNANCE_LABEL = "Gobernanza de la compra"

export const DELEGATIONS_COPY = {
  title: "Delegaciones",
  description:
    "Autoriza al agente a comprar sin ti, con un tope y por un tiempo. Puedes revocarlo cuando quieras.",
  newDelegation: "Nueva delegación",
  mine: "Mis delegaciones",
  none: "Todavía no tienes delegaciones.",
  loading: "Cargando tus delegaciones…",
  revokeAll: "Revocar todo",
} as const

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
