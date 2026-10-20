import { useEffect } from "react"
import { SessionGate } from "@/components/session-gate"
import evidence from "./evidence.json"
import { parseEvidence } from "./adapter.ts"
import { MONITOR_TITLE, OPEN_GATES } from "./gates.ts"
import { SourceMonitor } from "./SourceMonitor.tsx"

function readEvidence(raw: unknown) {
  try {
    return parseEvidence(raw)
  } catch {
    return null
  }
}

/** `raw` is the bundled projection; any other value only exists to show the contract error. */
export function EvidenceRoute({ raw = evidence }: { raw?: unknown }) {
  const bundle = readEvidence(raw)
  if (!bundle) return <main className="p-8"><h1>{MONITOR_TITLE}</h1><p role="alert">Evidencia inconsistente, no disponible o incompatible. No se habilita ninguna acción comercial.</p><p>{OPEN_GATES}</p></main>
  return <SourceMonitor offlineBundle={bundle} />
}

/** Same session gate as App: no evidence renders without a Better Auth session. */
export default function GovernanceRoute() {
  useEffect(() => { document.title = MONITOR_TITLE }, [])
  return <SessionGate>{() => <EvidenceRoute />}</SessionGate>
}
