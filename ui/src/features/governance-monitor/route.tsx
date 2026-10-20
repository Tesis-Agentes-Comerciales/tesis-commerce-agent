import evidence from "./evidence.json"
import { parseEvidence } from "./adapter.ts"
import { MONITOR_TITLE, OPEN_GATES } from "./gates.ts"
import { SourceMonitor } from "./SourceMonitor.tsx"

function readEvidence() {
  try {
    return parseEvidence(evidence)
  } catch {
    return null
  }
}

export default function GovernanceRoute() {
  const bundle = readEvidence()
  if (!bundle) return <main className="p-8"><h1>{MONITOR_TITLE}</h1><p role="alert">Evidencia inconsistente, no disponible o incompatible. No se habilita ninguna acción comercial.</p><p>{OPEN_GATES}</p></main>
  return <SourceMonitor offlineBundle={bundle} />
}
