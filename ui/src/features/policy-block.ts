import type { PolicyBlockReason } from "@/components/agent"
import { isRecord } from "@/features/agent-results"
import { runIdOf } from "@/features/purchase-trace"

// Bloqueo de la política de compra (D1) leído del stream, como `staleTermsOf` lee los términos que ya
// no valen: el paso `prepare_checkout` falla con el código del backend y la compra ya está explicada,
// sin esperar a que llegue la traza. Ni aviso rojo ni «Se detuvo por un error».

/** Códigos con los que `prepare_checkout` falla cuando la política detiene la compra. */
const POLICY_BLOCKS = new Map<string, PolicyBlockReason>([
  ["d1_denied", "denied"],
  ["d1_unavailable", "unavailable"],
  ["d1_allow_unapplied", "allow_unapplied"],
])

/** Por qué la política detuvo la compra según un código de error, o `null` si es otro error. */
export function policyBlockOf(code: unknown): PolicyBlockReason | null {
  return typeof code === "string" ? (POLICY_BLOCKS.get(code) ?? null) : null
}

type StepPart = { type: string; toolName?: string; args?: unknown; result?: unknown; isError?: boolean }

/** El código de error de un paso fallido, o `undefined`. */
export function stepErrorCode(part: StepPart | undefined) {
  if (!part?.isError || !isRecord(part.result)) return undefined
  const error = part.result.error
  return isRecord(error) ? error.code : undefined
}

/**
 * La política detuvo la compra de este mensaje: su `prepare_checkout` falló con un código de
 * bloqueo. El `run_id` va en los `args` porque el paso no trae `result`.
 */
export function messagePolicyBlock(parts: readonly StepPart[]) {
  const prepare = parts.find(
    (part) => part.type === "tool-call" && part.toolName === "prepare_checkout",
  )
  const reason = policyBlockOf(stepErrorCode(prepare))
  return reason && prepare ? { reason, runId: runIdOf(prepare.args) } : null
}
