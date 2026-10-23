/**
 * Ritmo mínimo de lo que el chat muestra (docs/design/trace-panel.md, «Mientras carga un turno»).
 * Cuando la respuesta ya llegó, el plan y la gobernanza no aparecen de golpe: se ve cada paso.
 */

/** El plan visible avanza como mucho un paso cada tanto: cada paso se ve en curso antes de hecho. */
export const PLAN_STEP_MIN_MS = 700

/** Las filas nuevas de la línea de tiempo de la gobernanza entran de a una, cada tanto. */
export const GOVERNANCE_ROW_MS = 350

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

  /** Lo que hay que mostrar ahora. `instant` (movimiento reducido) revela todo de una vez. */
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
    this.#reveal(hidden.slice(0, 1))
    this.#tick()
  }

  #reveal(keys: readonly string[]) {
    this.revealed = new Set([...this.revealed, ...keys])
    this.#lastAt = Date.now()
    for (const listener of this.#listeners) listener()
  }
}
