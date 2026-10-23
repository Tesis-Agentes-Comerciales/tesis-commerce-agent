import type { DelegationDraftView } from "@/lib/agent-contract"
import { checkoutDetails } from "@/lib/commerce-api"
import type { BuyerProfile } from "@/lib/commerce-types"

// Del formulario «Delegar una compra» al cuerpo de `POST /delegations/proposals`
// (docs/design/hnp-governed.md §5.1 y §6). Sin red: el cliente está en `delegation-api.ts`.

/** `POST /delegations/proposals`. */
export type DelegationProposalRequest = {
  /** De 1 a 5, distintas y en orden: el servidor guarda el alcance así. */
  variant_ids: string[]
  /** Incluye envío. */
  total_max_minor: number
  currency: "COP"
  valid_for_seconds: number
  /** Los datos de entrega de la compra HP; el destino va tal como lo escribió el comprador. */
  details: ReturnType<typeof checkoutDetails>
  preference_text: string | null
}

/** El formulario tal como se llenó; `total` son pesos enteros escritos por el comprador. */
export type DelegationDraft = {
  variantIds: string[]
  total: string
  validForSeconds: number
  addressLine: string
  city: string
  state: string
  preference: string
}

/** §6: 5 variantes por alcance y 280 caracteres de indicación. */
export const MAX_DELEGATION_VARIANTS = 5
export const MAX_PREFERENCE_CHARS = 280

/** Pesos con o sin separadores de miles («2.000.000»), sin centavos. */
function pesos(total: string) {
  const digits = total.replace(/[.\s]/g, "")
  return /^\d{1,13}$/.test(digits) ? Number(digits) : null
}

export type DraftCheck =
  | { ok: true; request: DelegationProposalRequest }
  | { ok: false; problem: string }

/**
 * El cuerpo de la propuesta, o qué falta. La dirección viaja tal como se escribió: el comercio
 * normaliza el destino por su lado y la UI no adivina cómo.
 */
export function proposalRequest(draft: DelegationDraft, profile: BuyerProfile): DraftCheck {
  const variantIds = [...new Set(draft.variantIds)].sort()
  if (variantIds.length === 0) return { ok: false, problem: "Elige al menos una opción." }
  if (variantIds.length > MAX_DELEGATION_VARIANTS) {
    return { ok: false, problem: `Elige como máximo ${MAX_DELEGATION_VARIANTS} opciones.` }
  }
  const total = pesos(draft.total)
  if (!total) {
    return { ok: false, problem: "Escribe el total máximo en pesos, sin centavos." }
  }
  if (![draft.addressLine, draft.city, draft.state].every((field) => field.trim())) {
    return { ok: false, problem: "Completa la dirección, la ciudad y el departamento." }
  }
  const preference = draft.preference.trim() ? draft.preference : null
  if (preference && [...preference].length > MAX_PREFERENCE_CHARS) {
    return { ok: false, problem: `Tu indicación admite ${MAX_PREFERENCE_CHARS} caracteres.` }
  }
  const destination = { addressLine: draft.addressLine, city: draft.city, state: draft.state }
  return {
    ok: true,
    request: {
      variant_ids: variantIds,
      total_max_minor: total * 100,
      currency: "COP",
      valid_for_seconds: draft.validForSeconds,
      details: checkoutDetails({ ...profile, ...destination }),
      preference_text: preference,
    },
  }
}

/** Vigencia con la que abre el formulario: la más corta que ofrece. */
const DEFAULT_VALIDITY_SECONDS = 3_600

/** Centavos a pesos enteros con puntos de miles, como los escribe el comprador: «2.000.000». */
function pesosText(amountMinor: number) {
  return String(Math.floor(amountMinor / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ".")
}

/**
 * El formulario del hilo prellenado con lo que encontró el turno (hnp-chat-delegate.md): las
 * opciones marcadas, el total con el presupuesto que escribió el comprador (o vacío), su mensaje
 * como indicación y la entrega del perfil. Todo sigue editable.
 */
export function chatDraft(view: DelegationDraftView, profile: BuyerProfile): DelegationDraft {
  return {
    variantIds: view.offers.slice(0, MAX_DELEGATION_VARIANTS).map((offer) => offer.variant_id),
    total: view.budget_minor ? pesosText(view.budget_minor) : "",
    validForSeconds: DEFAULT_VALIDITY_SECONDS,
    addressLine: profile.addressLine,
    city: profile.city,
    state: profile.state,
    preference: [...view.preference_text.trim()].slice(0, MAX_PREFERENCE_CHARS).join(""),
  }
}
