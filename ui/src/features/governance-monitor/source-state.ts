import type { EvidenceBundle } from "./adapter.ts"
import type { DemoSnapshot } from "./snapshot-contract.ts"
import { SnapshotReadError } from "./snapshot-reader.ts"

export type SourceState = {
  mode: "offline" | "local"
  status: "offline" | "loading" | "read" | "disconnected" | "stale" | "inconsistent"
  bundle: EvidenceBundle | null
  readAt: string | null
  error: string | null
}
export const offlineSource = (bundle: EvidenceBundle): SourceState => ({ mode: "offline", status: "offline", bundle, readAt: null, error: null })
export const beginLocalRead = (state: SourceState): SourceState => ({ mode: "local", status: "loading", bundle: state.mode === "local" ? state.bundle : null, readAt: state.mode === "local" ? state.readAt : null, error: null })
export const acceptLocalRead = (snapshot: DemoSnapshot, readAt: string): SourceState => ({ mode: "local", status: "read", bundle: snapshot.bundle, readAt, error: null })
export function rejectLocalRead(previous: SourceState, error: SnapshotReadError): SourceState {
  if (error.kind === "inconsistent") return { mode: "local", status: "inconsistent", bundle: null, readAt: null, error: error.message }
  const hasPrevious = previous.mode === "local" && previous.bundle !== null && previous.readAt !== null
  return { mode: "local", status: hasPrevious ? "stale" : "disconnected", bundle: hasPrevious ? previous.bundle : null, readAt: hasPrevious ? previous.readAt : null, error: error.message }
}

export function sourceDescription(state: SourceState): string {
  if (state.status === "offline") return "Reproducción offline · artefactos históricos locales · sin lectura HTTP"
  if (state.status === "loading") return "Leyendo snapshot local; no es un evento en vivo. La lectura anterior, si existe, está obsoleta."
  if (state.status === "read") return "Snapshot leído por HTTP local · fixture histórica/obsoleta · no es un evento en vivo"
  if (state.status === "inconsistent") return "Evidencia inconsistente · snapshot rechazado íntegramente"
  if (state.status === "stale") return "Fuente desconectada · última lectura obsoleta; no confirma el estado actual"
  return "Fuente desconectada · no se obtuvo un snapshot; elige explícitamente Reproducción offline"
}
