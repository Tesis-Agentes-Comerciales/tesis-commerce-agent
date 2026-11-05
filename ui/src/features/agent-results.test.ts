import { describe, expect, it } from "vitest"

import { asDelegationDraft, asSelectResult } from "@/features/agent-results"
import { fixtureState } from "@/storybook/assistant-transport"
import CONDITIONAL from "@/storybook/fixtures/conditional-views.json"

// Los `result` del stream son no confiables (docs/agent-ui-contract.md): lo que no cumple el
// contrato se descarta sin romper el resto del paso.

const PRODUCT = fixtureState("delegate").delegation_draft?.offers[0]
if (!PRODUCT) throw new Error("delegate.sse has no offers")

describe("select_product con intención condicional", () => {
  const select = { decision: "select", product: PRODUCT, rationale: "" }

  it("trae la vista de las ramas cuando cumple el contrato", () => {
    const result = asSelectResult({ ...select, conditional: CONDITIONAL.B })
    expect(result?.conditional).toEqual(CONDITIONAL.B)
    expect(result?.product?.variant_id).toBe(PRODUCT.variant_id)
  })

  it("sin la clave, el resultado es el de siempre", () => {
    expect(asSelectResult(select)).not.toHaveProperty("conditional")
  })

  it("una vista que no cumple se descarta y el producto queda", () => {
    // La rama elegida tiene que ser `eligible`.
    const broken = { ...structuredClone(CONDITIONAL.B), chosen_index: 0 }
    const result = asSelectResult({ ...select, conditional: broken })
    expect(result).not.toHaveProperty("conditional")
    expect(result?.product?.variant_id).toBe(PRODUCT.variant_id)
  })
})

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
