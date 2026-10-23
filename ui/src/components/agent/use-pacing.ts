import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useState,
  useSyncExternalStore,
} from "react"

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

/**
 * Cómo entra lo que va a ritmo. `live`: a ritmo, como un turno que llega ahora. `settled`: lo que ya
 * estaba al montar sale entero y solo lo que llega después va a ritmo (un mensaje del historial no
 * se vuelve a animar al recargar). `instant`: todo al instante (Storybook, salvo las historias del
 * ritmo). `prefers-reduced-motion: reduce` vale siempre como `instant`.
 */
export type Pacing = "live" | "settled" | "instant"

export const PacingContext = createContext<Pacing>("live")

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
  const pacing = useContext(PacingContext)
  const reduced = useReducedMotion()
  const revealed = useSyncExternalStore(
    pacer?.subscribe ?? noSubscribe,
    pacer?.getSnapshot ?? noKeys,
  )
  // En un mensaje del historial, lo primero que se muestra ya estaba: sale entero.
  const instant = reduced || pacing === "instant" || (pacing === "settled" && revealed.size === 0)
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

/** El texto del asistente se escribe en a lo sumo este tiempo, sea corto o largo. */
export const TEXT_TYPE_MAX_MS = 1200
const TEXT_TYPE_TICK_MS = 30

/**
 * El texto que se ve de una parte que llega ahora: se escribe de a pocos caracteres, en no más de
 * `TEXT_TYPE_MAX_MS`, en vez de aparecer de golpe. Si crece (el stream agrega frases), sigue desde
 * donde iba. Fuera de un turno en vivo, o con movimiento reducido, entero.
 */
export function useTypedText(text: string) {
  const pacing = useContext(PacingContext)
  const reduced = useReducedMotion()
  const typing = pacing === "live" && !reduced
  const [count, setCount] = useState(() => (typing ? 0 : text.length))
  const done = !typing || count >= text.length
  const step = Math.max(2, Math.ceil((text.length * TEXT_TYPE_TICK_MS) / TEXT_TYPE_MAX_MS))
  useEffect(() => {
    if (done) return
    const timer = setTimeout(() => setCount((value) => value + step), TEXT_TYPE_TICK_MS)
    return () => clearTimeout(timer)
  }, [done, count, step])
  return done ? text : text.slice(0, count)
}
