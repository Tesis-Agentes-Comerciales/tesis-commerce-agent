import { describe, expect, it } from "vitest"

import { formatMoney } from "@/components/agent/agent-format"
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
