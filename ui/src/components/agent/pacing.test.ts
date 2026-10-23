import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  pacedPlanProgress,
  planReach,
  planStepStates,
  type PlanProgress,
} from "@/components/agent/agent-steps"
import { GOVERNANCE_ROW_MS, PacedReveal, PLAN_STEP_MIN_MS } from "@/components/agent/pacing"

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
