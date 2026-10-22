import { describe, expect, it } from "vitest"

import { taskPurchases, traceMayChange, type TaskPurchase } from "@/features/delegation-traces"
import { parseDelegationView } from "@/lib/delegation-contract"
import type { PurchaseTrace } from "@/lib/trace-contract"
import { DELEGATION_REPLAYS, replayed } from "@/storybook/fixtures/delegation-replays"
import revokedInFlight from "@/storybook/trace-fixtures/hnp/revoked-in-flight.json"
import enforceCompleted from "@/storybook/trace-fixtures/hnp/enforce-completed.json"

// Las fixtures se leen como datos: `parsePurchaseTrace` ya las valida en sus historias.
const COMPLETED = enforceCompleted as PurchaseTrace
const IN_PROGRESS = revokedInFlight as PurchaseTrace
const GRANTED = parseDelegationView(replayed(DELEGATION_REPLAYS.consent, "POST /delegations").body)
const RUN_ID = "run_0123456789abcdef0123456789abcdef"

const withTask = (task: Partial<typeof GRANTED.task>) =>
  parseDelegationView({ ...GRANTED, task: { ...GRANTED.task, ...task } })

describe("taskPurchases", () => {
  it("solo las tareas que ya empezaron una compra, con la versión de su vista", () => {
    const running = withTask({ state: "running", run_id: RUN_ID })
    expect(taskPurchases([GRANTED, running])).toEqual([
      { runId: RUN_ID, revision: `running:${running.task.updated_at}`, running: true },
    ])
  })
})

describe("traceMayChange", () => {
  const purchase: TaskPurchase = { runId: RUN_ID, revision: "purchased:t1", running: false }

  it("lee la traza que nunca se leyó en esta versión de la tarea", () => {
    expect(traceMayChange(purchase, undefined, undefined)).toBe(true)
    expect(traceMayChange(purchase, COMPLETED, "running:t0")).toBe(true)
  })

  it("sigue leyendo mientras la tarea compra o la compra sigue en curso", () => {
    expect(traceMayChange({ ...purchase, running: true }, COMPLETED, purchase.revision)).toBe(true)
    expect(traceMayChange(purchase, IN_PROGRESS, purchase.revision)).toBe(true)
  })

  it("deja de leer una compra cerrada, o una lectura que falló, hasta que la tarea avance", () => {
    expect(traceMayChange(purchase, COMPLETED, purchase.revision)).toBe(false)
    expect(traceMayChange(purchase, undefined, purchase.revision)).toBe(false)
  })
})
