import {
  parsePurchaseTrace,
  type PurchaseTrace,
  type StepKind,
  type TraceStep,
} from "@/lib/trace-contract"

import awaitingApproval from "./awaiting-approval.json"
import blockedMerchant from "./blocked-merchant.json"
import blockedProduct from "./blocked-product.json"
import canceled from "./canceled.json"
import completedEnforce from "./completed-enforce.json"
import completedShadow from "./completed-shadow.json"
import failed from "./failed.json"
import governedRecovery from "./governed-recovery.json"
import inProgress from "./in-progress.json"
import policyOff from "./policy-off.json"
import policyUnavailable from "./policy-unavailable.json"
import staleTerms from "./stale-terms.json"
import staleTermsReread from "./stale-terms-reread.json"
import uncertain from "./uncertain.json"

// Trazas sintéticas, una por escenario. El backend valida estos mismos archivos con Pydantic.
// `satisfies PurchaseTrace` no sirve con JSON (TypeScript ensancha sus literales a `string`), así que
// cada una pasa por `parsePurchaseTrace` al cargarse: si deja de cumplir el contrato, fallan todas
// las historias que la usan. Las horas están en UTC; en Bogotá la aprobación cae a las 10:42.

export const TRACES = {
  completedShadow: parsePurchaseTrace(completedShadow),
  completedEnforce: parsePurchaseTrace(completedEnforce),
  blockedMerchant: parsePurchaseTrace(blockedMerchant),
  blockedProduct: parsePurchaseTrace(blockedProduct),
  policyOff: parsePurchaseTrace(policyOff),
  inProgress: parsePurchaseTrace(inProgress),
  awaitingApproval: parsePurchaseTrace(awaitingApproval),
  policyUnavailable: parsePurchaseTrace(policyUnavailable),
  failed: parsePurchaseTrace(failed),
  canceled: parsePurchaseTrace(canceled),
  uncertain: parsePurchaseTrace(uncertain),
  // Controles de la aplicación del flujo HP gobernado (b2-governed-pilot), no de la política.
  /**
   * S04 como en el piloto: el comercio cambió el precio después de mostrar los términos y la frescura
   * detuvo el POST de la aprobación (etapa `approve`).
   */
  staleTerms: parsePurchaseTrace(staleTerms),
  /**
   * S04 como en el chat de la demo: al pulsar «Aprobar», la UI relee los términos antes del POST y la
   * frescura los rechaza ahí (etapa `present`, después de un `present` confirmado).
   */
  staleTermsReread: parsePurchaseTrace(staleTermsReread),
  /** S02: el total del comercio superó el presupuesto; el agente canceló el checkout y lo explicó. */
  governedRecovery: parsePurchaseTrace(governedRecovery),
} satisfies Record<string, PurchaseTrace>

/** Último paso de un tipo: en S04, la comprobación de frescura que bloqueó. */
export function lastStepOf<Kind extends StepKind>(trace: PurchaseTrace, kind: Kind) {
  const step = trace.steps.findLast((candidate) => candidate.kind === kind)
  if (!step) throw new Error(`Fixture ${trace.run_id} has no ${kind} step`)
  return step as Extract<TraceStep, { kind: Kind }>
}

/** Primer paso de un tipo dentro de una fixture; falla si no lo tiene. */
export function stepOf<Kind extends StepKind>(trace: PurchaseTrace, kind: Kind) {
  const step = trace.steps.find((candidate) => candidate.kind === kind)
  if (!step) throw new Error(`Fixture ${trace.run_id} has no ${kind} step`)
  return step as Extract<TraceStep, { kind: Kind }>
}

/** La misma fixture con el `run_id` de un stream: la UI descarta una traza de otra compra. */
export function withRunId(trace: PurchaseTrace, runId: string): PurchaseTrace {
  return { ...trace, run_id: runId }
}

/** Decisión de política de una fixture, para las tarjetas que la reciben suelta. */
export function decisionOf(trace: PurchaseTrace) {
  return stepOf(trace, "policy_decision").decision
}
