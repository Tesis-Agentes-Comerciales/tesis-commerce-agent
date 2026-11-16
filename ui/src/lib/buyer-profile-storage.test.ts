import { describe, expect, it } from "vitest"

import {
  loadBuyerProfile,
  profileStorageKey,
  saveBuyerProfile,
  type ProfileStorage,
} from "@/lib/buyer-profile-storage"
import { DEMO_PROFILE, type BuyerProfile } from "@/lib/commerce-types"

function memoryStorage(entries: Record<string, string> = {}): ProfileStorage {
  const values = new Map(Object.entries(entries))
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => {
      values.set(key, value)
    },
  }
}

const brokenStorage: ProfileStorage = {
  getItem: () => {
    throw new DOMException("bloqueado", "SecurityError")
  },
  setItem: () => {
    throw new DOMException("lleno", "QuotaExceededError")
  },
}

const EDITED: BuyerProfile = {
  ...DEMO_PROFILE,
  firstName: "Lucía",
  addressLine: "Carrera 7 # 72-41, apto 501",
}

describe("los datos de entrega guardados", () => {
  it("sobreviven a una recarga, por cuenta", () => {
    const storage = memoryStorage()
    saveBuyerProfile("ana", EDITED, storage)

    expect(loadBuyerProfile("ana", storage)).toEqual(EDITED)
    expect(loadBuyerProfile("otra-cuenta", storage)).toBe(DEMO_PROFILE)
  })

  it("sin nada guardado o sin almacenamiento son los del perfil de demo", () => {
    expect(loadBuyerProfile("ana", memoryStorage())).toBe(DEMO_PROFILE)
    expect(loadBuyerProfile("ana", null)).toBe(DEMO_PROFILE)
  })

  it.each([
    ["texto que no es JSON", "{perfil"],
    ["una lista", "[]"],
    ["un campo que falta", JSON.stringify({ ...DEMO_PROFILE, city: undefined })],
    ["un campo que no es texto", JSON.stringify({ ...DEMO_PROFILE, postalCode: 110111 })],
    ["null", "null"],
  ])("con %s vuelven a los de demo", (_, saved) => {
    const storage = memoryStorage({ [profileStorageKey("ana")]: saved })

    expect(loadBuyerProfile("ana", storage)).toBe(DEMO_PROFILE)
  })

  it("solo conservan los campos del perfil", () => {
    const saved = JSON.stringify({ ...EDITED, token: "no-debe-viajar" })
    const storage = memoryStorage({ [profileStorageKey("ana")]: saved })

    const profile = loadBuyerProfile("ana", storage)

    expect(profile).toEqual(EDITED)
    expect(profile).not.toHaveProperty("token")
  })

  it("un almacenamiento que falla no rompe la carga ni el guardado", () => {
    expect(loadBuyerProfile("ana", brokenStorage)).toBe(DEMO_PROFILE)
    expect(() => saveBuyerProfile("ana", EDITED, brokenStorage)).not.toThrow()
  })

  it("en Node, sin `window`, no hay almacenamiento y no falla", () => {
    expect(loadBuyerProfile("ana")).toBe(DEMO_PROFILE)
    expect(() => saveBuyerProfile("ana", EDITED)).not.toThrow()
  })
})
