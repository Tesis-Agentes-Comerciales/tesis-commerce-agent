import { describe, expect, it } from "vitest"

import { stepDetail } from "@/components/trace/trace-copy"
import type { EscalationReason, TraceStep } from "@/lib/trace-contract"
import { HNP_TRACES } from "@/storybook/trace-fixtures/hnp"

// Con prioridad (p0-conditional-intent.md §3.9), `no_candidate_in_scope` y `selection_unavailable`
// cubren causas que no son un fallo del servicio: la traza no la adivina.

const NO_CHARGE = "No se hizo ningún cobro."
const ESCALATED = HNP_TRACES.escalated.steps.find((step) => step.kind === "escalated")
if (!ESCALATED) throw new Error("escalated.json has no escalated step")

const escalatedBy = (reason: EscalationReason) => ({ ...ESCALATED, reason }) as TraceStep

describe("el escalamiento de una compra delegada", () => {
  it.each([
    [
      "no_candidate_in_scope",
      "Ninguna de las opciones que autorizaste se puede comprar ahora dentro de lo que autorizaste.",
    ],
    ["selection_unavailable", "No pude elegir con seguridad entre las opciones que autorizaste."],
  ] satisfies [EscalationReason, string][])("%s no supone la causa", (reason, cause) => {
    expect(stepDetail(escalatedBy(reason))).toBe(`${cause} ${NO_CHARGE}`)
  })
})
