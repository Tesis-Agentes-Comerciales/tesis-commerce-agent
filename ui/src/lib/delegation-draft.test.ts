import { describe, expect, it } from "vitest"

import { DEMO_PROFILE } from "@/lib/commerce-types"
import { proposalDeadline, type DelegationProposal } from "@/lib/delegation-contract"
import { asDelegationDraft } from "@/features/agent-results"
import {
  chatDraft,
  MAX_PREFERENCE_CHARS,
  proposalRequest,
  type DelegationDraft,
  type DelegationProposalRequest,
} from "@/lib/delegation-draft"
import { fixtureState } from "@/storybook/assistant-transport"
import { DELEGATION_REPLAYS, replayed } from "@/storybook/fixtures/delegation-replays"

const { consent, proposalExpired } = DELEGATION_REPLAYS
const proposalBody = () =>
  replayed(consent, "POST /delegations/proposals").request as DelegationProposalRequest

describe("del formulario a la propuesta", () => {
  const draft: DelegationDraft = {
    variantIds: ["SON-XM6-BLK", "BOS-QCU-BLK", "SON-XM6-BLK"],
    total: "2.000.000",
    validForSeconds: 3_600,
    addressLine: DEMO_PROFILE.addressLine,
    // Como la escribió el comprador en la secuencia: con tilde.
    city: "Bogotá",
    state: DEMO_PROFILE.state,
    preference: "Si los dos están disponibles, prefiero los Sony.",
  }

  it("arma el mismo cuerpo que envió la secuencia real", () => {
    const checked = proposalRequest(draft, DEMO_PROFILE)
    expect(checked).toEqual({ ok: true, request: proposalBody() })
  })

  it("envía el destino tal como se escribió, sin normalizarlo", () => {
    const typed = { ...draft, city: "  bogotá ", state: "Cundinamarca" }
    const checked = proposalRequest(typed, DEMO_PROFILE)
    if (!checked.ok) throw new Error(checked.problem)
    expect(checked.request.details.fulfillment.address).toMatchObject({
      city: "  bogotá ",
      state: "Cundinamarca",
      country: "CO",
      postal_code: "110111",
    })
  })

  it("una indicación en blanco viaja como null", () => {
    const checked = proposalRequest({ ...draft, preference: "   " }, DEMO_PROFILE)
    expect(checked.ok && checked.request.preference_text).toBeNull()
  })

  it.each([
    [{ variantIds: [] }, "Elige al menos una opción."],
    [{ variantIds: ["A", "B", "C", "D", "E", "F"] }, "Elige como máximo 5 opciones."],
    [{ total: "" }, "Escribe el total máximo en pesos, sin centavos."],
    [{ total: "0" }, "Escribe el total máximo en pesos, sin centavos."],
    [{ total: "2.000.000,50" }, "Escribe el total máximo en pesos, sin centavos."],
    [{ city: " " }, "Completa la dirección, la ciudad y el departamento."],
    [{ preference: "x".repeat(281) }, "Tu indicación admite 280 caracteres."],
  ] satisfies [Partial<DelegationDraft>, string][])("%o: %s", (change, problem) => {
    expect(proposalRequest({ ...draft, ...change }, DEMO_PROFILE)).toEqual({ ok: false, problem })
  })
})

describe("la vigencia de una propuesta", () => {
  const proposal = replayed(consent, "POST /delegations/proposals").body as DelegationProposal

  it("es la de la propuesta cuando vence antes que el alcance", () => {
    expect(Date.parse(proposal.expires_at)).toBeLessThan(Date.parse(proposal.scope.expires_at))
    expect(proposalDeadline(proposal)).toBe(proposal.expires_at)
  })

  it("es la del alcance cuando la delegación vence primero (como `consent` en el backend)", () => {
    const expired = replayed(proposalExpired, "POST /delegations/proposals").body as DelegationProposal
    expect(Date.parse(expired.scope.expires_at)).toBeLessThan(Date.parse(expired.expires_at))
    expect(proposalDeadline(expired)).toBe(expired.scope.expires_at)
  })
})

describe("el borrador del chat (modo «Delegar»)", () => {
  const view = asDelegationDraft(fixtureState("delegate").delegation_draft)
  if (!view) throw new Error("delegate.sse no trae delegation_draft")

  it("marca las opciones, prellena el total con el presupuesto y la preferencia con el mensaje", () => {
    const draft = chatDraft(view, DEMO_PROFILE)
    expect(draft.variantIds).toEqual(view.offers.map((offer) => offer.variant_id))
    expect(draft.variantIds).toContain("SON-XM6-BLK")
    expect(draft.total).toBe("2.000.000")
    expect(draft.preference).toBe("Quiero audífonos negros por menos de 2 millones")
    expect(draft.validForSeconds).toBe(3_600)
    expect(draft).toMatchObject({
      addressLine: DEMO_PROFILE.addressLine,
      city: DEMO_PROFILE.city,
      state: DEMO_PROFILE.state,
    })
  })

  it("el total prellenado llega a la propuesta como el presupuesto exacto", () => {
    const checked = proposalRequest(chatDraft(view, DEMO_PROFILE), DEMO_PROFILE)
    if (!checked.ok) throw new Error(checked.problem)
    expect(checked.request.total_max_minor).toBe(view.budget_minor)
    expect(checked.request.preference_text).toBe(view.preference_text)
  })

  it("sin presupuesto el total queda vacío y es obligatorio", () => {
    const draft = chatDraft({ ...view, budget_minor: null }, DEMO_PROFILE)
    expect(draft.total).toBe("")
    expect(proposalRequest(draft, DEMO_PROFILE)).toEqual({
      ok: false,
      problem: "Escribe el total máximo en pesos, sin centavos.",
    })
  })

  it("recorta la preferencia a 280 caracteres y marca como máximo 5 opciones", () => {
    const long = "ñ".repeat(MAX_PREFERENCE_CHARS + 40)
    const offers = Array.from({ length: 6 }, (_, index) => ({
      ...view.offers[0]!,
      variant_id: `VAR-${index}`,
    }))
    const draft = chatDraft({ ...view, offers, preference_text: long }, DEMO_PROFILE)
    expect([...draft.preference]).toHaveLength(MAX_PREFERENCE_CHARS)
    expect(draft.variantIds).toEqual(["VAR-0", "VAR-1", "VAR-2", "VAR-3", "VAR-4"])
  })

  it("descarta lo que no tiene la forma del contrato y las opciones que no se pueden delegar", () => {
    expect(asDelegationDraft(null)).toBeNull()
    expect(asDelegationDraft({ offers: "x", preference_text: "a" })).toBeNull()
    const unavailable = { ...view.offers[0]!, available: false }
    const dollars = { ...view.offers[0]!, variant_id: "USD-1", currency: "USD" }
    const parsed = asDelegationDraft({
      ...view,
      offers: [unavailable, dollars],
      budget_minor: -5,
    })
    expect(parsed).toMatchObject({ offers: [], budget_minor: null })
  })
})
