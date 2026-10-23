import { describe, expect, it } from "vitest"

import {
  isDelegateTurn,
  isFollowUpOnly,
  productSlot,
  streamPurchaseOf,
} from "@/features/turn-layout"

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

describe("streamPurchaseOf", () => {
  const RUN = "run_0123456789abcdef"
  const selected = tool("select_product", {
    result: {
      decision: "select",
      product: {
        product_id: "SON-XM6",
        variant_id: "SON-XM6-BLK",
        title: "Sony WH-1000XM6",
        variant_title: "Sony WH-1000XM6 negro",
        observed_unit_amount_minor: 189_900_000,
        currency: "COP",
      },
    },
  })
  const preparing = tool("prepare_checkout", { args: { variant_id: "SON", run_id: RUN } })

  it("sale cuando empieza prepare_checkout con el run_id en sus args, antes de la traza", () => {
    expect(streamPurchaseOf([selected, preparing])).toEqual({
      status: "in_progress",
      summary: "Comprando…",
      product: "Sony WH-1000XM6 negro",
    })
  })

  it("con la aprobación pedida, espera a la persona", () => {
    const approval = tool("request_approval", { result: {} })
    expect(streamPurchaseOf([selected, { ...preparing, result: {} }, approval])?.status).toBe(
      "awaiting_approval",
    )
  })

  it("un bloqueo de la política ya es definitivo; el allow sin aplicar no culpa a la política", () => {
    const denied = { ...blocked, args: { run_id: RUN } }
    expect(streamPurchaseOf([selected, denied])).toMatchObject({
      status: "blocked",
      summary: "Bloqueada por la política de compra",
    })
    const unapplied = {
      ...denied,
      result: { error: { code: "d1_allow_unapplied", message: "x" } },
    }
    expect(streamPurchaseOf([selected, unapplied])?.summary).toBe("Detenida por precaución")
  })

  it("sin run_id, sin checkout o con otro error, no hay fila del stream", () => {
    expect(streamPurchaseOf([selected])).toBeNull()
    expect(streamPurchaseOf([selected, tool("prepare_checkout")])).toBeNull()
    const failed = {
      ...preparing,
      result: { error: { code: "checkout_failed", message: "x" } },
      isError: true,
    }
    expect(streamPurchaseOf([selected, failed])).toBeNull()
  })
})

describe("isDelegateTurn", () => {
  const turn = { latest: true, active: false, delegating: false, drafted: false }

  it("mientras corre en «Delegar», antes de que llegue el borrador, ya es delegado", () => {
    expect(isDelegateTurn({ ...turn, active: true, delegating: true })).toBe(true)
  })

  it("terminado, lo dice el borrador del estado aunque el selector ya vuelva a «Comprar conmigo»", () => {
    expect(isDelegateTurn({ ...turn, drafted: true, delegating: true })).toBe(true)
    expect(isDelegateTurn({ ...turn, drafted: true, delegating: false })).toBe(true)
  })

  it("un turno HP no lo es, ni cuando el selector pasa a «Delegar» sin enviar", () => {
    expect(isDelegateTurn(turn)).toBe(false)
    expect(isDelegateTurn({ ...turn, active: true })).toBe(false)
    expect(isDelegateTurn({ ...turn, delegating: true })).toBe(false)
  })

  it("un mensaje anterior conserva su plan: la compra HP pendiente lo sigue mostrando", () => {
    expect(isDelegateTurn({ ...turn, latest: false, drafted: true, delegating: true })).toBe(false)
  })
})
