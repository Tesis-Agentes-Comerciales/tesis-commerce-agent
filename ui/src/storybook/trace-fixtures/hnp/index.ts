import { parsePurchaseTrace, type PurchaseTrace } from "@/lib/trace-contract"

import checkoutRefused from "./checkout-refused.json"
import d2Denied from "./d2-denied.json"
import enforceCompleted from "./enforce-completed.json"
import enforceReviewNotConfirmed from "./enforce-review-not-confirmed.json"
import escalated from "./escalated.json"
import killSwitch from "./kill-switch.json"
import offCompleted from "./off-completed.json"
import offerChanged from "./offer-changed.json"
import revokedInFlight from "./revoked-in-flight.json"
import waitingForPolicy from "./waiting-for-policy.json"

// Compras delegadas (HNP, docs/design/hnp-governed.md §10): el comprador consintió antes de irse y
// el agente compra solo. Sintéticas como las de HP y en su propia carpeta; el backend las valida
// con Pydantic (tests/test_hnp_trace_contract.py). Las completadas traen lo que escribe la UoW-3:
// `payment_authorized` (del recibo de pago `success`), `order_confirmed` y el uso consumido, con la
// misma hora y en ese orden.

export const HNP_TRACES = {
  /** B0-HNP: sin decisiones de política; los controles del ledger siguen. */
  offCompleted: parsePurchaseTrace(offCompleted),
  /** B2-HNP: D1, frescura al cerrar, D2 antes de reservar y D3 antes de despachar. */
  enforceCompleted: parsePurchaseTrace(enforceCompleted),
  /**
   * El revisor de intención no confirmó la elección (contradice la indicación del comprador): escala
   * antes de la política, sin checkout ni cobro (docs/design/hnp-intent-review.md).
   */
  enforceReviewNotConfirmed: parsePurchaseTrace(enforceReviewNotConfirmed),
  /** D2 niega por POL-08 (el destino no es el consentido): la decisión vuelve al comprador. */
  d2Denied: parsePurchaseTrace(d2Denied),
  /** La prueba del mandato abierto quedó sin resolver: D2 se detiene sin evaluar reglas y escala. */
  escalated: parsePurchaseTrace(escalated),
  /** El envío subió justo antes de cerrar: la frescura detiene la compra antes de D2. */
  offerChanged: parsePurchaseTrace(offerChanged),
  /** El dueño revocó con el despacho ya comprometido: la compra sigue y se concilia. */
  revokedInFlight: parsePurchaseTrace(revokedInFlight),
  /** Kill switch después de reservar: D3 permitió sin aplicarse y el uso único se liberó. */
  killSwitch: parsePurchaseTrace(killSwitch),
  /** K15: `start` dejó la compra elegida sin el resultado de D1; la tarea falló y la compra espera. */
  waitingForPolicy: parsePurchaseTrace(waitingForPolicy),
  /** #93: el comercio rechazó el checkout (`out_of_stock`) y la tarea terminó `checkout_failed`. */
  checkoutRefused: parsePurchaseTrace(checkoutRefused),
} satisfies Record<string, PurchaseTrace>
