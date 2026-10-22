import { useEffect, useRef, useState } from "react"

import { getPurchaseTrace } from "@/lib/commerce-api"
import type { DelegationView } from "@/lib/delegation-contract"
import type { PurchaseStatus, PurchaseTrace } from "@/lib/trace-contract"

// Gobernanza de las compras delegadas en «Mis delegaciones» (docs/design/trace-panel.md, «Compra
// delegada»). Como la traza de un mensaje del chat (`purchase-trace.ts`), se relee por REST: aquí,
// cada 2 s mientras la compra de una tarea pueda cambiar.

/** Cada cuánto se relee lo que sigue en curso: la lista de delegaciones y las trazas. */
export const DELEGATION_POLL_MS = 2_000

/** Estados de una traza que ya no cambian solos; los demás siguen en curso. */
const SETTLED: ReadonlySet<PurchaseStatus> = new Set([
  "completed",
  "blocked",
  "stale_terms",
  "canceled",
  "failed",
  "uncertain",
])

/** La compra de una tarea y la versión de su vista: cambia cuando la tarea avanza. */
export type TaskPurchase = { runId: string; revision: string; running: boolean }

/** Las compras que tienen traza que leer: las de las tareas que ya empezaron una. */
export function taskPurchases(delegations: readonly DelegationView[]): TaskPurchase[] {
  return delegations.flatMap(({ task }) =>
    task.run_id
      ? [{ runId: task.run_id, revision: `${task.state}:${task.updated_at}`, running: task.state === "running" }]
      : [],
  )
}

/**
 * Si la traza de una compra puede haber cambiado desde la última lectura. No, si su tarea terminó,
 * ya se leyó en esa versión de la tarea y la traza quedó cerrada; tampoco si esa lectura falló (un
 * 404 o un 503 se reintenta cuando la tarea avanza, no cada 2 s).
 */
export function traceMayChange(
  purchase: TaskPurchase,
  trace: PurchaseTrace | undefined,
  readRevision: string | undefined,
) {
  if (purchase.running || readRevision !== purchase.revision) return true
  return trace !== undefined && !SETTLED.has(trace.status)
}

/**
 * Lee la traza de la compra de cada delegación mientras `active` (el diálogo abierto): al aparecer
 * su `run_id`, cada vez que la tarea avanza y cada 2 s mientras la compra pueda cambiar. Al cerrar
 * se aborta lo que estaba en camino y no queda ningún temporizador. Una lectura que falla no rompe
 * la tarjeta: conserva la última traza o, si no hay ninguna, no muestra la gobernanza.
 */
export function useDelegationTraces(
  delegations: readonly DelegationView[],
  active: boolean,
): Record<string, PurchaseTrace> {
  const [traces, setTraces] = useState<Record<string, PurchaseTrace>>({})
  const known = useRef<Record<string, PurchaseTrace>>({})
  // Versión de la tarea en la última lectura de cada compra, haya respondido o no.
  const readAt = useRef(new Map<string, string>())
  // La lista releída trae objetos nuevos aunque nada haya cambiado: el efecto depende de su texto.
  const key = JSON.stringify(taskPurchases(delegations))

  useEffect(() => {
    const purchases: TaskPurchase[] = JSON.parse(key)
    if (!active || purchases.length === 0) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined

    const due = () =>
      purchases.filter((purchase) =>
        traceMayChange(purchase, known.current[purchase.runId], readAt.current.get(purchase.runId)),
      )

    const read = async ({ runId, revision }: TaskPurchase) => {
      try {
        const trace = await getPurchaseTrace(runId, controller.signal)
        if (controller.signal.aborted) return
        known.current = { ...known.current, [runId]: trace }
        setTraces(known.current)
      } catch {
        // Sin traza todavía (la compra aún no existe) o no disponible: se conserva la anterior.
      }
      if (!controller.signal.aborted) readAt.current.set(runId, revision)
    }

    const poll = async () => {
      await Promise.all(due().map(read))
      if (!controller.signal.aborted && due().length > 0) {
        timer = setTimeout(() => void poll(), DELEGATION_POLL_MS)
      }
    }
    void poll()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [active, key])

  return traces
}
