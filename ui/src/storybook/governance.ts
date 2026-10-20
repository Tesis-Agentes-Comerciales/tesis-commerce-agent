import { parseEvidence, type Scenario } from "@/features/governance-monitor/adapter"
import evidence from "@/features/governance-monitor/evidence.json"
import { DEMO_SNAPSHOT_ID, parseDemoSnapshot } from "@/features/governance-monitor/snapshot-contract"
import { DEMO_SNAPSHOT_URL } from "@/features/governance-monitor/snapshot-reader"

// Las historias del monitor leen la misma proyección versionada que la app y `npm run test`
// (`evidence.json`, contrato governance-monitor/3). Nada se copia: los estados inconsistentes se
// derivan en memoria con las mismas mutaciones que los tests y el parser los rechaza igual.

export const EVIDENCE = evidence

export const MONITOR_BUNDLE = parseEvidence(evidence)

export const HNP02_DRY_RUN = MONITOR_BUNDLE.experiments[0]

export type ScenarioId = "hp-created" | "hp-denied" | "hnp-false-accept" | "hp-uncertain"

export function scenario(id: ScenarioId): Scenario {
  const found = MONITOR_BUNDLE.scenarios.find((s) => s.id === id)
  if (!found) throw Error(`La evidencia versionada no tiene el escenario ${id}`)
  return found
}

/** El snapshot de demostración tal como lo sirve `scripts/governance_snapshot_server.mjs`. */
export const DEMO_SNAPSHOT = parseDemoSnapshot({
  schema: "governance-demo-snapshot/1",
  snapshotId: DEMO_SNAPSHOT_ID,
  classification: "sanitized_demo_fixture",
  temporalScope: "historical_closed_artifacts",
  liveEvents: false,
  authenticatedUserEvidence: false,
  bundle: evidence,
})

// Mismos bytes que `scripts/prepare_governance_snapshot.mjs`: el lector los compara con el pin.
const SNAPSHOT_BYTES = `${JSON.stringify(DEMO_SNAPSHOT, null, 2)}\n`

/** Hora de lectura fija para que las historias sean deterministas; no es la hora del evento. */
export const STORY_READ_AT = "2026-10-20T12:00:00.000Z"

/**
 * Respuesta del servidor local de snapshots para cada lectura de la historia:
 * - `snapshot`: los bytes fijados, como el servidor de demo;
 * - `altered`: un byte de más, así que el hash no coincide (inconsistente);
 * - `rejected`: el 422 con que el servidor rechaza una fixture inválida (inconsistente);
 * - `unreachable`: no hay servidor (desconectado).
 */
export type SnapshotReply = "snapshot" | "altered" | "rejected" | "unreachable"

function snapshotResponse(reply: SnapshotReply) {
  const json = { "Content-Type": "application/json" }
  if (reply === "unreachable") throw new TypeError("Failed to fetch")
  if (reply === "rejected") {
    return new Response(JSON.stringify({ code: "DEMO_SNAPSHOT_INVALID" }), { status: 422, headers: json })
  }
  return new Response(reply === "altered" ? `${SNAPSHOT_BYTES} ` : SNAPSHOT_BYTES, { headers: json })
}

/**
 * Simula el servidor local de snapshots para la historia actual: cada lectura toma la siguiente
 * respuesta y la última se repite. Ninguna lectura sale del navegador.
 */
export function mockSnapshotSource(replies: readonly SnapshotReply[] = ["unreachable"]) {
  const previous = globalThis.fetch
  let reads = 0
  globalThis.fetch = async (input, init) => {
    const url = input instanceof Request ? input.url : String(input)
    if (url !== DEMO_SNAPSHOT_URL) return previous(input, init)
    const reply = replies[Math.min(reads, replies.length - 1)] ?? "unreachable"
    reads += 1
    return snapshotResponse(reply)
  }
  return () => {
    globalThis.fetch = previous
  }
}
