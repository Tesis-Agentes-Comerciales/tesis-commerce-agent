import { useEffect, useState } from "react"

/**
 * Milisegundos desde `startedAt` (epoch ms, como `ToolCallTiming`). Avanza mientras `active` y se
 * congela en `completedAt` cuando existe; sin `startedAt` devuelve `undefined`.
 */
export function useElapsedMs(startedAt: number | undefined, active: boolean, completedAt?: number) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!active || startedAt === undefined) return
    const timer = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(timer)
  }, [active, startedAt])

  if (startedAt === undefined) return undefined
  return Math.max(0, (completedAt ?? now) - startedAt)
}
