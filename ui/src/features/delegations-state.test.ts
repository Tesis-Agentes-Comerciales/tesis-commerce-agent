import { describe, expect, it } from "vitest"

import {
  activeGrantIds,
  delegationsReducer,
  hasTaskInFlight,
  INITIAL_DELEGATIONS,
  type DelegationsAction,
  type DelegationsState,
} from "@/features/delegations-state"
import { DEMO_PROFILE } from "@/lib/commerce-types"
import type { DelegationDraft } from "@/lib/delegation-draft"
import {
  parseDelegationList,
  parseDelegationProposal,
  parseDelegationRevokeAllReceipt,
  parseDelegationRevokeReceipt,
  parseDelegationView,
} from "@/lib/delegation-contract"
import { DELEGATION_REPLAYS, replayed } from "@/storybook/fixtures/delegation-replays"

const { consent, revoke, revokeAll } = DELEGATION_REPLAYS

const PROPOSAL = parseDelegationProposal(replayed(consent, "POST /delegations/proposals").body)
const GRANTED = parseDelegationView(replayed(consent, "POST /delegations").body)
const LISTED = parseDelegationList(replayed(revokeAll, "GET /me/delegations", 0).body).delegations
const ALL_RECEIPTS = parseDelegationRevokeAllReceipt(
  replayed(revokeAll, "POST /me/delegations/revoke-all").body,
).receipts
const DRAFT: DelegationDraft = {
  variantIds: ["BOS-QCU-BLK"],
  total: "2000000",
  validForSeconds: 3_600,
  addressLine: DEMO_PROFILE.addressLine,
  city: DEMO_PROFILE.city,
  state: DEMO_PROFILE.state,
  preference: "",
}

const run = (actions: DelegationsAction[], from: DelegationsState = INITIAL_DELEGATIONS) =>
  actions.reduce(delegationsReducer, from)

const listed = run([{ type: "list-loaded", delegations: LISTED }])

describe("la propuesta y su consentimiento", () => {
  it("formulario → propuesta → autorizada: la tarjeta nueva va primero y toma el foco", () => {
    const state = run([
      { type: "list-loaded", delegations: [] },
      { type: "form-opened" },
      { type: "proposing", draft: DRAFT },
      { type: "proposed", proposal: PROPOSAL },
      { type: "authorizing" },
      { type: "authorized", delegation: GRANTED },
    ])
    expect(state.flow).toEqual({ step: "idle" })
    expect(state.draft).toBeNull()
    expect(state.consentedId).toBe(GRANTED.grant_id)
    expect(state.delegations).toEqual([GRANTED])
  })

  it("volver de la propuesta conserva lo escrito", () => {
    const state = run([
      { type: "form-opened" },
      { type: "proposing", draft: DRAFT },
      { type: "proposed", proposal: PROPOSAL },
      { type: "form-opened" },
    ])
    expect(state.flow).toEqual({ step: "form", submitting: false, error: null })
    expect(state.draft).toEqual(DRAFT)
  })

  it("una propuesta que ya no vale deja de ofrecer «Autorizar»; un error pasajero no", () => {
    const proposed = run([{ type: "proposed", proposal: PROPOSAL }])
    const stale = run(
      [{ type: "authorizing" }, { type: "authorize-failed", error: "Venció.", canAuthorize: false }],
      proposed,
    )
    const transient = run(
      [{ type: "authorizing" }, { type: "authorize-failed", error: "Red.", canAuthorize: true }],
      proposed,
    )
    expect(stale.flow).toMatchObject({ step: "consent", authorizing: false, canAuthorize: false })
    expect(transient.flow).toMatchObject({ step: "consent", error: "Red.", canAuthorize: true })
    // Un error del backend al proponer vuelve al formulario con su texto.
    expect(run([{ type: "form-opened" }, { type: "propose-failed", error: "Bogotá D.C." }]).flow).toEqual(
      { step: "form", submitting: false, error: "Bogotá D.C." },
    )
  })
})

describe("las revocaciones", () => {
  const [one, two] = activeGrantIds(listed)

  it("«Revocar todo» alcanza solo las activas", () => {
    expect(activeGrantIds(listed)).toHaveLength(3)
    expect(activeGrantIds(listed)).not.toContain(LISTED.find((item) => item.state !== "active")?.grant_id)
  })

  it("revocar una no termina la que sigue en curso en otra tarjeta", () => {
    const answer = replayed(revoke, "POST /delegations/dg_fae0f66102c54aa5b899fad0da0e88ca/revoke")
    const receipt = { ...parseDelegationRevokeReceipt(answer.body), grant_id: one! }
    const state = run(
      [
        { type: "revoking", grantIds: [one!] },
        { type: "revoking", grantIds: [two!] },
        { type: "revoked", receipts: [receipt] },
      ],
      listed,
    )
    expect(state.revocations[one!]).toEqual({ revoking: false, error: null, receipt })
    expect(state.revocations[two!]).toEqual({ revoking: true, error: null, receipt: null })
  })

  it("«Revocar todo» reparte cada recibo a su tarjeta y termina", () => {
    const state = run(
      [
        { type: "revoking", grantIds: activeGrantIds(listed), all: true },
        { type: "revoked", receipts: ALL_RECEIPTS, all: true },
      ],
      listed,
    )
    expect(state.revokingAll).toBe(false)
    for (const receipt of ALL_RECEIPTS) {
      expect(state.revocations[receipt.grant_id]).toEqual({ revoking: false, error: null, receipt })
    }
  })

  it("si «Revocar todo» falla, nada queda en curso y el error va arriba", () => {
    const state = run(
      [
        { type: "revoking", grantIds: activeGrantIds(listed), all: true },
        { type: "revoke-all-failed", error: "No pude comunicarme con el agente." },
      ],
      listed,
    )
    expect(state.revokingAll).toBe(false)
    expect(state.revokeAllError).toBe("No pude comunicarme con el agente.")
    expect(Object.values(state.revocations).every((item) => !item.revoking)).toBe(true)
  })

  it("la vista releída reemplaza la vieja en su lugar", () => {
    const reread = { ...LISTED[1]!, state: "revoked" as const }
    const state = run([{ type: "delegation-read", delegation: reread }], listed)
    expect(state.delegations.map((item) => item.grant_id)).toEqual(LISTED.map((item) => item.grant_id))
    expect(state.delegations[1]).toBe(reread)
  })
})

describe("hasTaskInFlight", () => {
  const view = (state: string, task: string) =>
    parseDelegationView({ ...GRANTED, state, task: { ...GRANTED.task, state: task } })

  it("relee mientras una tarea espera el disparo o compra", () => {
    expect(hasTaskInFlight([GRANTED])).toBe(true)
    expect(hasTaskInFlight([view("active", "running")])).toBe(true)
  })

  it("una compra comprometida sigue aunque la delegación se haya revocado (§8.4)", () => {
    expect(hasTaskInFlight([{ ...view("active", "running"), state: "revoked", revoked_reason: "owner" }])).toBe(true)
  })

  it("no relee lo que ya no corre: tareas terminadas o programadas de una delegación cerrada", () => {
    expect(hasTaskInFlight([])).toBe(false)
    expect(hasTaskInFlight([{ ...GRANTED, state: "expired" }])).toBe(false)
    expect(hasTaskInFlight([{ ...GRANTED, state: "revoked", revoked_reason: "owner" }])).toBe(false)
    expect(hasTaskInFlight(LISTED.filter((item) => item.task.state === "canceled"))).toBe(false)
  })
})
