import { parseEvidence, type EvidenceBundle } from "./adapter.ts"

export const MAX_SNAPSHOT_BYTES = 65_536
export const DEMO_SNAPSHOT_ID = "hp-hnp-demo-v1"
export const DEMO_SNAPSHOT_URL = `http://127.0.0.1:5183/snapshots/${DEMO_SNAPSHOT_ID}`

export type DemoSnapshot = {
  schema: "governance-demo-snapshot/1"
  snapshotId: typeof DEMO_SNAPSHOT_ID
  classification: "sanitized_demo_fixture"
  temporalScope: "historical_closed_artifacts"
  liveEvents: false
  authenticatedUserEvidence: false
  bundle: EvidenceBundle
}

/** Deny unexpected fields and secret-like data before serving or displaying fixtures. */
export function parseDemoSnapshot(value: unknown): DemoSnapshot {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw Error("Snapshot incompatible")
  const s = value as Record<string, unknown>
  const keys = ["schema", "snapshotId", "classification", "temporalScope", "liveEvents", "authenticatedUserEvidence", "bundle"]
  if (Object.keys(s).length !== keys.length || keys.some((k) => !Object.hasOwn(s, k))) throw Error("Contrato de snapshot inválido")
  if (s.schema !== "governance-demo-snapshot/1" || s.snapshotId !== DEMO_SNAPSHOT_ID
    || s.classification !== "sanitized_demo_fixture" || s.temporalScope !== "historical_closed_artifacts"
    || s.liveEvents !== false || s.authenticatedUserEvidence !== false) throw Error("No es una fixture de demo autorizada")
  const text = JSON.stringify(value)
  if (/-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:ghp_|github_pat_|AIza|sk-proj-)[A-Za-z0-9_-]{20,}|Bearer\s+\S+|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i.test(text)) throw Error("Redacción de fixture inválida")
  const bundle = parseEvidence(s.bundle)
  const expected = ["hp-created", "hp-denied", "hnp-false-accept", "hp-uncertain"]
  if (bundle.scenarios.length !== expected.length || expected.some((id, i) => bundle.scenarios[i].id !== id)) throw Error("Escenarios fuera de la fixture autorizada")
  return { schema: "governance-demo-snapshot/1", snapshotId: DEMO_SNAPSHOT_ID, classification: "sanitized_demo_fixture", temporalScope: "historical_closed_artifacts", liveEvents: false, authenticatedUserEvidence: false, bundle }
}
