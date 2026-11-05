import { describe, expect, it } from "vitest"

import { asDelegationDraft } from "@/features/agent-results"
import { fixtureState } from "@/storybook/assistant-transport"

// Los `result` del stream son no confiables (docs/agent-ui-contract.md): lo que no cumple el
// contrato se descarta sin romper el resto del paso.

describe("el orden del borrador de delegación", () => {
  const raw = fixtureState("delegate").delegation_draft
  if (!raw) throw new Error("delegate.sse has no delegation_draft")
  const [first, second, third] = raw.offers.map((offer) => offer.variant_id)

  it("sin `priority` no hay orden", () => {
    expect(asDelegationDraft(raw)).not.toHaveProperty("priority")
  })

  it("conserva el orden que leyó el turno", () => {
    expect(asDelegationDraft({ ...raw, priority: [second, first] })?.priority).toEqual([
      second,
      first,
    ])
  })

  it("quita las variantes que no están entre las opciones que se pueden delegar", () => {
    const view = asDelegationDraft({ ...raw, priority: ["OTRA-VAR", third, first] })
    expect(view?.priority).toEqual([third, first])
    expect(asDelegationDraft({ ...raw, priority: ["OTRA-VAR"] })).not.toHaveProperty("priority")
  })

  it.each([
    ["no es una lista", "SON-XM6-BLK"],
    ["vacía", []],
    ["con repetidas", [first, first]],
    ["con más de 3", [first, second, third, "OTRA-VAR"]],
    ["con algo que no es texto", [first, 7]],
    ["null", null],
  ])("%s: se descarta", (_, priority) => {
    expect(asDelegationDraft({ ...raw, priority })).not.toHaveProperty("priority")
  })
})
