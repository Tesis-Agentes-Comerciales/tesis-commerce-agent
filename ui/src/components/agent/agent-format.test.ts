import { describe, expect, it } from "vitest"

import { formatLiveElapsed, formatMoney } from "@/components/agent/agent-format"
import notation from "@/storybook/fixtures/money-notation.json"

// K16: los mensajes HNP del backend escriben los montos con esta misma tabla
// (tests/test_money_notation.py), así que la tarjeta no mezcla dos notaciones.
describe("formatMoney", () => {
  it.each(notation.cases)("escribe $minor centavos como la tabla compartida", ({ minor, text }) => {
    expect(formatMoney(minor, notation.currency)).toBe(text)
  })

  it("separa el símbolo del número con un espacio duro", () => {
    expect(formatMoney(192400000, "COP")).toMatch(/^\$ 1\.924\.000/)
  })
})

// El contador en vivo cambia cada 250 ms: con un formato fijo no mueve lo que va a su lado.
describe("formatLiveElapsed", () => {
  it.each([
    [0, "0,0 s"],
    [2000, "2,0 s"],
    [2300, "2,3 s"],
    [9949, "9,9 s"],
    [12_400, "12 s"],
    [65_000, "1 min 5 s"],
  ])("escribe %i ms como %s", (ms, text) => {
    expect(formatLiveElapsed(ms)).toBe(text)
  })

  it("no escribe un tiempo inválido", () => {
    expect(formatLiveElapsed(-1)).toBeNull()
    expect(formatLiveElapsed(Number.NaN)).toBeNull()
  })
})
