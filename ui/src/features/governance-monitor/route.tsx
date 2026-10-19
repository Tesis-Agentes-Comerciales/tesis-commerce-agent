import evidence from "./evidence.json"
import { parseHistoricalEvidence } from "./historical.ts"
import { SourceMonitor } from "./SourceMonitor.tsx"

function readEvidence() {
  try {
    return parseHistoricalEvidence(evidence)
  } catch {
    return null
  }
}

export default function GovernanceRoute() {
  const bundle = readEvidence()
  if (!bundle) return <main className="p-8"><h1>Monitor de gobernanza · PARTIAL</h1><p role="alert">Evidencia inconsistente, no disponible o incompatible. No se habilita ninguna acción comercial.</p><p>I10/D08/D09 siguen parciales. AUDIT-MERCHANT-CANCEL-01 conserva su gate histórico sin aprobar.</p></main>
  return <SourceMonitor offlineBundle={bundle} />
}
