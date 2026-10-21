import { describe, expect, it } from "vitest"

import { DEMO_PROFILE } from "@/lib/commerce-types"
import { proposalDeadline, type DelegationProposal } from "@/lib/delegation-contract"
import {
  proposalRequest,
  type DelegationDraft,
  type DelegationProposalRequest,
} from "@/lib/delegation-draft"
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
