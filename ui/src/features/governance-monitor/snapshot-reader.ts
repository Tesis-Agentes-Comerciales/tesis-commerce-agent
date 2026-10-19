import { DEMO_SNAPSHOT_URL, MAX_SNAPSHOT_BYTES, parseDemoSnapshot, type DemoSnapshot } from "./snapshot-contract.ts"
import { DEMO_SNAPSHOT_SHA256 } from "./snapshot-pin.ts"

export class SnapshotReadError extends Error {
  kind: "timeout" | "disconnected" | "inconsistent"
  constructor(kind: SnapshotReadError["kind"]) {
    super(kind === "inconsistent" ? "Evidencia inconsistente" : kind === "timeout" ? "Tiempo de lectura agotado" : "Fuente desconectada")
    this.kind = kind
  }
}

export async function loadLocalSnapshot({ fetchImpl = globalThis.fetch, signal, timeoutMs = 2000 }: { fetchImpl?: typeof fetch; signal?: AbortSignal; timeoutMs?: number } = {}): Promise<DemoSnapshot> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (signal?.aborted) throw new SnapshotReadError("disconnected")
  signal?.addEventListener("abort", abort, { once: true })
  let timer: ReturnType<typeof setTimeout> | undefined
  let rejectAbort: (() => void) | undefined
  const aborted = new Promise<never>((_, reject) => {
    rejectAbort = () => reject(new SnapshotReadError("disconnected"))
    controller.signal.addEventListener("abort", rejectAbort, { once: true })
  })
  try {
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => { reject(new SnapshotReadError("timeout")); controller.abort() }, timeoutMs)
    })
    const work = async () => {
      const response = await fetchImpl(DEMO_SNAPSHOT_URL, { method: "GET", credentials: "omit", cache: "no-store", redirect: "error", signal: controller.signal, headers: { Accept: "application/json" } })
      if (!response.ok) throw new SnapshotReadError(response.status === 422 ? "inconsistent" : "disconnected")
      if (!response.headers.get("content-type")?.startsWith("application/json") || !response.body) throw new SnapshotReadError("inconsistent")
      const reader = response.body.getReader(), chunks: Uint8Array[] = []
      let length = 0
      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          length += value.byteLength
          if (length > MAX_SNAPSHOT_BYTES) throw new SnapshotReadError("inconsistent")
          chunks.push(value)
        }
      } finally { await reader.cancel().catch(() => {}) }
      const bytes = new Uint8Array(length)
      let offset = 0
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), (x) => x.toString(16).padStart(2, "0")).join("")
      if (digest !== DEMO_SNAPSHOT_SHA256) throw new SnapshotReadError("inconsistent")
      try { return parseDemoSnapshot(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes))) }
      catch { throw new SnapshotReadError("inconsistent") }
    }
    return await Promise.race([work(), timeout, aborted])
  } catch (error) {
    if (error instanceof SnapshotReadError) throw error
    throw new SnapshotReadError("disconnected")
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener("abort", abort)
    if (rejectAbort) controller.signal.removeEventListener("abort", rejectAbort)
  }
}
