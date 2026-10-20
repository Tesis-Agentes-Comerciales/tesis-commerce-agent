import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import type { EvidenceBundle } from "./adapter.ts"
import { GovernanceMonitor } from "./GovernanceMonitor.tsx"
import { loadLocalSnapshot, SnapshotReadError } from "./snapshot-reader.ts"
import { offlineSource, beginLocalRead, acceptLocalRead, rejectLocalRead, sourceDescription, type SourceState } from "./source-state.ts"

export function SourceView({ state, onOffline, onLocal }: { state: SourceState; onOffline: () => void; onLocal: () => void }) {
  return <main className="h-svh overflow-y-auto bg-muted/30">
    <section aria-label="Fuente del monitor" className="mx-auto max-w-6xl space-y-3 px-4 pt-6 sm:px-8">
      <h1 className="text-xl font-semibold">Fuente de evidencia · demo local PARTIAL</h1>
      <div className="flex flex-wrap gap-2" aria-label="Seleccionar fuente">
        <Button type="button" aria-pressed={state.mode === "offline"} variant={state.mode === "offline" ? "default" : "outline"} onClick={onOffline}>Reproducción offline</Button>
        <Button type="button" aria-pressed={state.mode === "local"} variant={state.mode === "local" ? "default" : "outline"} onClick={onLocal}>Snapshot backend local</Button>
        {state.mode === "local" && <Button type="button" variant="outline" onClick={onLocal}>Releer snapshot</Button>}
      </div>
      <p role={state.status === "inconsistent" || state.status === "disconnected" ? "alert" : "status"} className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">{sourceDescription(state)}</p>
      <p className="text-xs text-muted-foreground">Sin autenticación real: exclusivamente fixtures sanitizadas de demostración. Nunca evidencia de usuarios autenticados ni eventos en vivo; cada escenario declara según su tipo de ejecución si intervino un LLM.</p>
      {state.readAt && <p className="text-xs">Hora de lectura del navegador (no tiempo del evento): {state.readAt}</p>}
      {state.error && <p className="text-sm text-amber-900">{state.error}. No se mezclan datos ni se cambia de fuente automáticamente.</p>}
      {!state.bundle && <p className="text-sm">No hay evidencia mostrable de esta lectura. Puedes seleccionar Reproducción offline.</p>}
    </section>
    {state.bundle && <GovernanceMonitor key={state.mode} bundle={state.bundle} embedded />}
    <footer className="mx-auto max-w-6xl px-4 py-4 text-xs sm:px-8">PARTIAL · I10/D08/D09 parciales · AUDIT-MERCHANT-CANCEL-01 con gate histórico sin aprobar.</footer>
  </main>
}

export function SourceMonitor({ offlineBundle }: { offlineBundle: EvidenceBundle }) {
  const [state, setState] = useState(() => offlineSource(offlineBundle))
  const sequence = useRef(0)
  const pending = useRef<AbortController | null>(null)
  useEffect(() => () => { sequence.current++; pending.current?.abort() }, [])
  function chooseOffline() {
    sequence.current++
    pending.current?.abort()
    setState(offlineSource(offlineBundle))
  }
  async function chooseLocal() {
    const id = ++sequence.current
    pending.current?.abort()
    const controller = new AbortController()
    pending.current = controller
    const previous = state
    setState(beginLocalRead(previous))
    try {
      const snapshot = await loadLocalSnapshot({ signal: controller.signal })
      if (id === sequence.current) setState(acceptLocalRead(snapshot, new Date().toISOString()))
    } catch (error) {
      if (id === sequence.current) setState(rejectLocalRead(previous, error instanceof SnapshotReadError ? error : new SnapshotReadError("disconnected")))
    }
  }
  return <SourceView state={state} onOffline={chooseOffline} onLocal={() => { void chooseLocal() }} />
}
