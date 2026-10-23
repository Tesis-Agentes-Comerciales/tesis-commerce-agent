import { useLayoutEffect, useState, useSyncExternalStore } from "react"

import { pacedPlanProgress, planReach, type PlanProgress } from "@/components/agent/agent-steps"
import { PacedReveal, PLAN_STEP_MIN_MS } from "@/components/agent/pacing"

const REDUCED_MOTION = "(prefers-reduced-motion: reduce)"
const NO_KEYS: ReadonlySet<string> = new Set()
const noKeys = () => NO_KEYS
const noSubscribe = () => () => {}

function subscribeReducedMotion(onChange: () => void) {
  const query = window.matchMedia?.(REDUCED_MOTION)
  query?.addEventListener("change", onChange)
  return () => query?.removeEventListener("change", onChange)
}

/** `prefers-reduced-motion: reduce`: lo que va a ritmo sale al instante. */
export function useReducedMotion() {
  return useSyncExternalStore(
    subscribeReducedMotion,
    () => window.matchMedia?.(REDUCED_MOTION).matches ?? false,
  )
}

/**
 * Un `PacedReveal` que dura lo que el componente, o `null` sin ritmo. Recuerda lo ya revelado: si
 * lo usa un hijo que se desmonta (el contenido de un plegable), al volver nada se anima otra vez.
 */
export function usePacer(stepMs: number, paced: boolean) {
  const [pacer] = useState(() => new PacedReveal(stepMs))
  return paced ? pacer : null
}

/**
 * Las claves de `keys` que se ven: con `pacer`, las que ya reveló a su ritmo; sin él o con
 * movimiento reducido, todas.
 */
export function usePacedKeys(keys: readonly string[], pacer: PacedReveal | null) {
  const instant = useReducedMotion()
  const revealed = useSyncExternalStore(
    pacer?.subscribe ?? noSubscribe,
    pacer?.getSnapshot ?? noKeys,
  )
  const signature = keys.join("\n")
  // Antes de pintar: lo que se revela al instante sale en el mismo cuadro, sin un hueco.
  useLayoutEffect(() => {
    if (!pacer) return
    pacer.show(signature ? signature.split("\n") : [], instant)
    return () => pacer.stop()
  }, [pacer, signature, instant])
  return pacer && !instant ? revealed : new Set(keys)
}

/**
 * El plan que se ve: avanza como mucho un paso cada `PLAN_STEP_MIN_MS`, cada uno en curso antes de
 * hecho, aunque el progreso real vaya adelantado. Solo presentación: `progress` no cambia.
 */
export function usePacedPlan(progress: PlanProgress, paced = true) {
  const pacer = usePacer(PLAN_STEP_MIN_MS, paced)
  const revealed = usePacedKeys(planReach(progress), pacer)
  return pacedPlanProgress(progress, revealed)
}
