import { planReach, type PlanProgress, type PlanStepId } from "@/components/agent/agent-steps"

/**
 * Ritmo mínimo de lo que el chat muestra (docs/design/trace-panel.md, «Mientras carga un turno»).
 * Cuando la respuesta ya llegó, el plan, las partes del turno y la gobernanza no aparecen de golpe:
 * se ve cada paso.
 */

/** El plan visible avanza como mucho un paso cada tanto: cada paso se ve en curso antes de hecho. */
export const PLAN_STEP_MIN_MS = 700

/** Las filas nuevas de la línea de tiempo de la gobernanza entran de a una, cada tanto. */
export const GOVERNANCE_ROW_MS = 350

/** Las partes de un turno que llegan juntas (la tarjeta, la aprobación, el texto) salen de a una. */
export const TURN_PART_STAGGER_MS = 400

/** Una parte del turno y el paso del plan después del cual sale; `"end"`, con el plan al día. */
export type TurnBlock = { key: string; step: PlanStepId | "end" }

/**
 * Las partes del turno que ya pueden salir: en su orden, hasta la primera cuyo paso el plan visible
 * todavía no alcanzó. Así nada entra encima de lo que ya se ve. Sin plan, o con el plan visible al
 * día con el real, todas: ninguna parte espera un paso que no va a llegar.
 */
export function readyBlocks(
  blocks: readonly TurnBlock[],
  plan: { real: PlanProgress; visible: PlanProgress } | null,
) {
  const keys = blocks.map(({ key }) => key)
  if (!plan) return keys
  const reached: ReadonlySet<string> = new Set(planReach(plan.visible))
  if (reached.size >= planReach(plan.real).length) return keys
  const waiting = blocks.findIndex(({ step }) => step === "end" || !reached.has(step))
  return waiting === -1 ? keys : keys.slice(0, waiting)
}

/**
 * Revela claves en el orden de la lista: la primera al instante y las siguientes de a una, con al
 * menos `stepMs` entre una y otra. Lo revelado no se oculta ni se vuelve a revelar, aunque la lista
 * cambie. Sin React: `usePacedKeys` solo lo conecta, y las pruebas lo manejan con timers falsos.
 */
export class PacedReveal {
  /** Se reemplaza en cada revelación: sirve de instantánea para `useSyncExternalStore`. */
  revealed: ReadonlySet<string> = new Set()
  readonly stepMs: number
  #keys: readonly string[] = []
  #instant = false
  #lastAt = Number.NEGATIVE_INFINITY
  #timer: ReturnType<typeof setTimeout> | undefined
  #listeners = new Set<() => void>()

  constructor(stepMs: number) {
    this.stepMs = stepMs
  }

  /**
   * Lo que hay que mostrar ahora. `instant` revela todo de una vez y no cuenta como un paso: así
   * entra lo que ya estaba (movimiento reducido, un mensaje del historial) y lo siguiente no espera.
   */
  show(keys: readonly string[], instant = false) {
    this.#keys = keys
    this.#instant = instant
    this.#tick()
  }

  /** Deja de revelar hasta el próximo `show`, que sigue donde iba. */
  stop() {
    clearTimeout(this.#timer)
    this.#timer = undefined
  }

  subscribe = (listener: () => void) => {
    this.#listeners.add(listener)
    return () => {
      this.#listeners.delete(listener)
    }
  }

  getSnapshot = () => this.revealed

  #tick = () => {
    this.stop()
    const hidden = this.#keys.filter((key) => !this.revealed.has(key))
    if (hidden.length === 0) return
    if (this.#instant) return this.#reveal(hidden)
    const wait = this.#lastAt + this.stepMs - Date.now()
    if (wait > 0) {
      this.#timer = setTimeout(this.#tick, wait)
      return
    }
    this.#lastAt = Date.now()
    this.#reveal(hidden.slice(0, 1))
    this.#tick()
  }

  #reveal(keys: readonly string[]) {
    this.revealed = new Set([...this.revealed, ...keys])
    for (const listener of this.#listeners) listener()
  }
}
