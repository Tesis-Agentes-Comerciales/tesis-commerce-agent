import { describe, expect, it } from "vitest"

import { seenOffers } from "@/features/seen-offers"
import type { AgentMessage } from "@/lib/agent-contract"
import { fixtureState } from "@/storybook/assistant-transport"

describe("las opciones vistas en el chat", () => {
  it("salen de search_catalog: disponibles, en pesos y sin repetir", () => {
    const offers = seenOffers(fixtureState("purchase").messages)
    expect(offers.map((offer) => offer.variant_id)).toEqual(
      expect.arrayContaining(["BOS-QCU-BLK", "SON-XM6-BLK"]),
    )
    expect(new Set(offers.map((offer) => offer.variant_id)).size).toBe(offers.length)
    expect(offers.every((offer) => offer.available && offer.currency === "COP")).toBe(true)
  })

  it("sin búsquedas no hay opciones", () => {
    const greeting: AgentMessage[] = fixtureState("greeting").messages
    expect(seenOffers(greeting)).toEqual([])
  })
})
