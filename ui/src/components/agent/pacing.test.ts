import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  pacedPlanProgress,
  planReach,
  planStepStates,
  type PlanProgress,
} from "@/components/agent/agent-steps"
import {
  GOVERNANCE_ROW_MS,
  PacedReveal,
  PLAN_STEP_MIN_MS,
  readyBlocks,
  TURN_PART_STAGGER_MS,
  type TurnBlock,
} from "@/components/agent/pacing"

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

const revealed = (pacer: PacedReveal) => [...pacer.revealed]

describe("PacedReveal", () => {
  it("revela la primera clave al instante y las siguientes de a una cada paso", () => {
    const pacer = new PacedReveal(GOVERNANCE_ROW_MS)
    pacer.show(["a", "b", "c"])
    expect(revealed(pacer)).toEqual(["a"])
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS - 1)
    expect(revealed(pacer)).toEqual(["a"])
    vi.advanceTimersByTime(1)
    expect(revealed(pacer)).toEqual(["a", "b"])
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS)
    expect(revealed(pacer)).toEqual(["a", "b", "c"])
    expect(vi.getTimerCount()).toBe(0)
  })

  it("una clave nueva espera lo que falta del paso, y tras una pausa sale al instante", () => {
    const pacer = new PacedReveal(GOVERNANCE_ROW_MS)
    pacer.show(["a"])
    vi.advanceTimersByTime(100)
    pacer.show(["a", "b"])
    expect(revealed(pacer)).toEqual(["a"])
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS - 100)
    expect(revealed(pacer)).toEqual(["a", "b"])

    vi.advanceTimersByTime(5_000)
    pacer.show(["a", "b", "c"])
    expect(revealed(pacer)).toEqual(["a", "b", "c"])
  })

  it("volver a leer la misma lista no reinicia la espera ni repite lo ya mostrado", () => {
    const pacer = new PacedReveal(GOVERNANCE_ROW_MS)
    const listener = vi.fn()
    pacer.subscribe(listener)
    pacer.show(["a", "b"])
    vi.advanceTimersByTime(200)
    pacer.show(["a", "b"])
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS - 200)
    expect(revealed(pacer)).toEqual(["a", "b"])
    pacer.show(["a", "b"])
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS * 3)
    expect(listener).toHaveBeenCalledTimes(2)
  })

  it("una fila que entra en medio sale en su lugar sin ocultar las que ya se veían", () => {
    const pacer = new PacedReveal(GOVERNANCE_ROW_MS)
    pacer.show(["a", "c"])
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS)
    pacer.show(["a", "b", "c", "d"])
    expect(revealed(pacer)).toEqual(["a", "c"])
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS)
    expect(revealed(pacer)).toEqual(["a", "c", "b"])
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS)
    expect(revealed(pacer)).toEqual(["a", "c", "b", "d"])
  })

  it("al instante (movimiento reducido) revela todo de una vez", () => {
    const pacer = new PacedReveal(GOVERNANCE_ROW_MS)
    pacer.show(["a", "b", "c"], true)
    expect(revealed(pacer)).toEqual(["a", "b", "c"])
    expect(vi.getTimerCount()).toBe(0)
  })

  it("lo revelado al instante no cuenta como paso: lo siguiente sale enseguida", () => {
    const pacer = new PacedReveal(GOVERNANCE_ROW_MS)
    pacer.show(["a", "b"], true)
    pacer.show(["a", "b", "c", "d"])
    expect(revealed(pacer)).toEqual(["a", "b", "c"])
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS)
    expect(revealed(pacer)).toEqual(["a", "b", "c", "d"])
  })

  it("detenido no sigue revelando; al volver a mostrar sigue donde iba", () => {
    const pacer = new PacedReveal(GOVERNANCE_ROW_MS)
    pacer.show(["a", "b", "c"])
    pacer.stop()
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS * 5)
    expect(revealed(pacer)).toEqual(["a"])
    pacer.show(["a", "b", "c"])
    expect(revealed(pacer)).toEqual(["a", "b"])
    vi.advanceTimersByTime(GOVERNANCE_ROW_MS)
    expect(revealed(pacer)).toEqual(["a", "b", "c"])
  })
})

/** Lo que hace `usePacedPlan`: el pacer revela los pasos alcanzados y el plan se arma con ellos. */
function plan(pacer: PacedReveal, progress: PlanProgress) {
  pacer.show(planReach(progress))
  return () => pacedPlanProgress(progress, pacer.revealed)
}

describe("plan a ritmo", () => {
  it("el ritmo mínimo es de 700 ms por paso y 350 ms por fila de la gobernanza", () => {
    expect(PLAN_STEP_MIN_MS).toBe(700)
    expect(GOVERNANCE_ROW_MS).toBe(350)
  })

  it("planReach lista los pasos alcanzados, con `done` al terminar", () => {
    expect(planReach({ current: null })).toEqual([])
    expect(planReach({ current: "search" })).toEqual(["understand", "search"])
    expect(planReach({ current: "checkout", stopped: "checkout" })).toEqual([
      "understand",
      "search",
      "choose",
      "checkout",
    ])
    expect(planReach({ current: "done" })).toEqual([
      "understand",
      "search",
      "choose",
      "checkout",
      "approval",
      "complete",
      "done",
    ])
  })

  it("si el progreso real ya va en Elegir, muestra Entender y Buscar en curso antes", () => {
    const view = plan(new PacedReveal(PLAN_STEP_MIN_MS), { current: "choose" })
    expect(view()).toEqual({ current: "understand" })
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS - 1)
    expect(view()).toEqual({ current: "understand" })
    vi.advanceTimersByTime(1)
    expect(view()).toEqual({ current: "search" })
    expect(planStepStates(view())).toMatchObject({ understand: "done", search: "active" })
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS)
    expect(view()).toEqual({ current: "choose" })
  })

  it("esperar la aprobación se aplica cuando el plan visible llega a ese paso", () => {
    const progress: PlanProgress = { current: "approval", waiting: true }
    const view = plan(new PacedReveal(PLAN_STEP_MIN_MS), progress)
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS * 3)
    expect(view()).toEqual({ current: "checkout" })
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS)
    expect(view()).toBe(progress)
  })

  it("la parada de la política y la falla se aplican al llegar a su paso", () => {
    const stopped: PlanProgress = { current: "checkout", stopped: "checkout" }
    const stoppedView = plan(new PacedReveal(PLAN_STEP_MIN_MS), stopped)
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS * 2)
    expect(stoppedView()).toEqual({ current: "choose" })
    expect(planStepStates(stoppedView()).checkout).toBe("pending")
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS)
    expect(planStepStates(stoppedView()).checkout).toBe("stopped")

    const failed: PlanProgress = { current: "approval", failed: "approval" }
    const failedView = plan(new PacedReveal(PLAN_STEP_MIN_MS), failed)
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS * 3)
    expect(planStepStates(failedView())).toMatchObject({ checkout: "active", approval: "pending" })
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS)
    expect(failedView()).toBe(failed)
  })

  it("completado: Completar sale en curso antes de marcar todo como hecho", () => {
    const pacer = new PacedReveal(PLAN_STEP_MIN_MS)
    plan(pacer, { current: "approval", waiting: true })
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS * 10)
    const view = plan(pacer, { current: "done" })
    expect(view()).toEqual({ current: "complete" })
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS)
    expect(view()).toEqual({ current: "done" })
  })

  it("un plan que avanza al ritmo real no se demora", () => {
    const pacer = new PacedReveal(PLAN_STEP_MIN_MS)
    expect(plan(pacer, { current: "understand" })()).toEqual({ current: "understand" })
    vi.advanceTimersByTime(2_000)
    expect(plan(pacer, { current: "search" })()).toEqual({ current: "search" })
  })
})

const TURN: TurnBlock[] = [
  { key: "activity", step: "checkout" },
  { key: "part:3", step: "choose" },
  { key: "part:6", step: "approval" },
  { key: "text:7", step: "end" },
]

describe("partes del turno", () => {
  it("el escalonamiento es de 400 ms entre partes", () => {
    expect(TURN_PART_STAGGER_MS).toBe(400)
  })

  it("readyBlocks: sin plan, o con el plan visible al día, todas", () => {
    expect(readyBlocks(TURN, null)).toEqual(["activity", "part:3", "part:6", "text:7"])
    const real: PlanProgress = { current: "approval", waiting: true }
    expect(readyBlocks(TURN, { real, visible: real })).toEqual([
      "activity",
      "part:3",
      "part:6",
      "text:7",
    ])
  })

  it("readyBlocks: en orden, hasta la primera parte cuyo paso el plan visible no alcanzó", () => {
    const real: PlanProgress = { current: "approval", waiting: true }
    expect(readyBlocks(TURN, { real, visible: { current: "choose" } })).toEqual([])
    expect(readyBlocks(TURN, { real, visible: { current: "checkout" } })).toEqual([
      "activity",
      "part:3",
    ])
    // El texto espera a que el plan visible llegue al real, aunque ya pasó su paso.
    const tail: TurnBlock[] = [{ key: "text:1", step: "end" }]
    expect(readyBlocks(tail, { real, visible: { current: "checkout" } })).toEqual([])
  })

  it("sin plan, las partes que llegan juntas salen de a una cada 400 ms", () => {
    const pacer = new PacedReveal(TURN_PART_STAGGER_MS)
    pacer.show(readyBlocks(TURN, null))
    expect(revealed(pacer)).toEqual(["activity"])
    vi.advanceTimersByTime(TURN_PART_STAGGER_MS)
    expect(revealed(pacer)).toEqual(["activity", "part:3"])
    vi.advanceTimersByTime(TURN_PART_STAGGER_MS * 2)
    expect(revealed(pacer)).toEqual(["activity", "part:3", "part:6", "text:7"])
  })

  it("se encadena con el plan: cada parte sale después de que el plan visible llega a su paso", () => {
    // Lo que hace el mensaje: cada vez que el plan visible avanza, recalcula qué partes pueden salir.
    const planPacer = new PacedReveal(PLAN_STEP_MIN_MS)
    const blockPacer = new PacedReveal(TURN_PART_STAGGER_MS)
    let real: PlanProgress = { current: "choose" }
    let blocks: TurnBlock[] = []
    const sync = () => {
      const visible = pacedPlanProgress(real, planPacer.revealed)
      blockPacer.show(readyBlocks(blocks, { real, visible }))
    }
    planPacer.subscribe(sync)
    planPacer.show(planReach(real))
    vi.advanceTimersByTime(5_000)
    expect(revealed(planPacer)).toEqual(["understand", "search", "choose"])

    // Tras la espera larga del modelo llegan juntas la fila de actividad, la tarjeta, la
    // aprobación y el texto.
    real = { current: "approval", waiting: true }
    blocks = TURN
    planPacer.show(planReach(real))
    sync()
    expect(pacedPlanProgress(real, planPacer.revealed)).toEqual({ current: "checkout" })
    expect(revealed(blockPacer)).toEqual(["activity"])
    vi.advanceTimersByTime(TURN_PART_STAGGER_MS - 1)
    expect(revealed(blockPacer)).toEqual(["activity"])
    vi.advanceTimersByTime(1)
    expect(revealed(blockPacer)).toEqual(["activity", "part:3"])
    // La aprobación espera a que el plan visible llegue a Aprobación (700 ms) y a su turno (800).
    vi.advanceTimersByTime(PLAN_STEP_MIN_MS - TURN_PART_STAGGER_MS)
    expect(pacedPlanProgress(real, planPacer.revealed)).toBe(real)
    expect(revealed(blockPacer)).toEqual(["activity", "part:3"])
    vi.advanceTimersByTime(TURN_PART_STAGGER_MS * 2 - PLAN_STEP_MIN_MS)
    expect(revealed(blockPacer)).toEqual(["activity", "part:3", "part:6"])
    vi.advanceTimersByTime(TURN_PART_STAGGER_MS)
    expect(revealed(blockPacer)).toEqual(["activity", "part:3", "part:6", "text:7"])
    expect(vi.getTimerCount()).toBe(0)
  })
})
