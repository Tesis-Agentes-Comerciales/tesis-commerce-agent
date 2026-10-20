import type { ApprovalTerms } from "@/lib/agent-contract"
import type { AgentApiMock } from "@/storybook/agent-api-mock"
import {
  fixtureState,
  purchaseOutcome,
  recordPurchaseOutcome,
  scriptedAssistant,
} from "@/storybook/assistant-transport"
import { approvalRecord, COMPLETED } from "@/storybook/fixtures"
import { TRACES, withRunId } from "@/storybook/trace-fixtures"

/** Términos que mostró `request_approval` en `purchase.sse`; el backend los repite al aprobar. */
function streamedTerms(): ApprovalTerms {
  for (const message of fixtureState("purchase").messages) {
    if (message.role !== "assistant") continue
    for (const part of message.content) {
      if (part.type !== "tool-call" || part.toolName !== "request_approval" || part.isError) continue
      if (part.result && "terms" in part.result) return part.result.terms
    }
  }
  throw Error("purchase.sse no trae los términos de request_approval")
}

/** Traza que acompaña a los streams: espera la aprobación hasta que la historia complete o cancele. */
function liveTrace(runId: string) {
  const outcome = purchaseOutcome(runId)
  const trace =
    outcome === "completed"
      ? TRACES.completedShadow
      : outcome === "canceled"
        ? TRACES.canceled
        : TRACES.awaitingApproval
  return withRunId(trace, runId)
}

export const DEFAULT_AGENT_API: AgentApiMock = {
  "GET /health": { body: { status: "ok" } },
  // Sin compra pendiente el backend responde `null`; la app arranca con el thread vacío.
  "GET /me/pending-purchase": { body: null },
  // Streams reales del backend (fixtures/assistant-transport), elegidos por el texto del mensaje.
  "POST /assistant": (request) => scriptedAssistant(request),
  // La app relee los términos antes de aprobar; el mismo fingerprint deja seguir la aprobación.
  "GET /purchases/:runId/approval": { body: streamedTerms() },
  "POST /purchases/:runId/approval": ({ params }) => ({
    delayMs: 600,
    body: approvalRecord(params.runId ?? ""),
  }),
  "POST /purchases/:runId/cancel": ({ params }) => {
    recordPurchaseOutcome(params.runId ?? "", "canceled")
    return { delayMs: 400, body: { run_id: params.runId, stage: "canceled" } }
  },
  "GET /purchases/:runId/trace": ({ params }) => ({ body: liveTrace(params.runId ?? "") }),
  "POST /purchases/:runId/complete": ({ params }) => {
    recordPurchaseOutcome(params.runId ?? "", "completed")
    return { delayMs: 900, body: COMPLETED }
  },
}
