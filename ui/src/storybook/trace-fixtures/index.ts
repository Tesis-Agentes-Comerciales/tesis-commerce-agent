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
import inProgress from "./in-progress.json"
import policyOff from "./policy-off.json"
import policyUnavailable from "./policy-unavailable.json"
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
} satisfies Record<string, PurchaseTrace>

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
