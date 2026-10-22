import { describe, expect, it } from "vitest"

import { isFollowUpOnly, productSlot } from "@/features/turn-layout"

const tool = (toolName: string, extra: Record<string, unknown> = {}) => ({
  type: "tool-call",
  toolName,
  args: {},
  ...extra,
})
const done = (toolName: string) => tool(toolName, { result: {} })
const blocked = tool("prepare_checkout", {
  result: { error: { code: "d1_denied", message: "x" } },
  isError: true,
})

describe("isFollowUpOnly", () => {
  it("un mensaje que solo trae purchase_update es un seguimiento", () => {
    expect(isFollowUpOnly([tool("purchase_update")])).toBe(true)
    expect(isFollowUpOnly([done("purchase_update"), { type: "text", text: "Listo." }])).toBe(true)
  })

  it("un turno con otros pasos, o sin pasos todavía, no lo es", () => {
    expect(isFollowUpOnly([])).toBe(false)
    expect(isFollowUpOnly([{ type: "text", text: "Hola" }])).toBe(false)
    expect(isFollowUpOnly([done("interpret_request"), tool("purchase_update")])).toBe(false)
  })
})

describe("productSlot", () => {
  it("mientras elige, un marcador", () => {
    expect(productSlot({ select: "running", prepare: undefined, messageRunning: true })).toBe(
      "marker",
    )
  })

  it("elegido y con el checkout en preparación, el producto espera en el marcador", () => {
    expect(productSlot({ select: "complete", prepare: undefined, messageRunning: true })).toBe(
      "marker",
    )
    expect(
      productSlot({ select: "complete", prepare: tool("prepare_checkout"), messageRunning: true }),
    ).toBe("marker")
  })

  it("con el checkout listo, la tarjeta del producto", () => {
    expect(
      productSlot({ select: "complete", prepare: done("prepare_checkout"), messageRunning: true }),
    ).toBe("card")
  })

  it("un turno que terminó sin checkout muestra la tarjeta", () => {
    expect(productSlot({ select: "complete", prepare: undefined, messageRunning: false })).toBe(
      "card",
    )
  })

  it("si la política detuvo la compra, el producto va en la tarjeta de bloqueo", () => {
    expect(productSlot({ select: "complete", prepare: blocked, messageRunning: true })).toBe("none")
    expect(productSlot({ select: "complete", prepare: blocked, messageRunning: false })).toBe("none")
  })

  it("otro error del checkout deja la tarjeta del producto", () => {
    const failed = tool("prepare_checkout", {
      result: { error: { code: "checkout_failed", message: "x" } },
      isError: true,
    })
    expect(productSlot({ select: "complete", prepare: failed, messageRunning: false })).toBe("card")
  })
})
