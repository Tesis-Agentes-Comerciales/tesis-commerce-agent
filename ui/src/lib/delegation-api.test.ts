import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { AgentApiError, AGENT_API_URL } from "@/lib/commerce-api"
import {
  consentToDelegation,
  delegationErrorCode,
  delegationErrorText,
  getDelegation,
  myDelegations,
  needsNewProposal,
  proposeDelegation,
  revokeAllDelegations,
  revokeDelegation,
} from "@/lib/delegation-api"
import type { DelegationProposalRequest } from "@/lib/delegation-draft"
import {
  DELEGATION_REPLAYS,
  replayed,
  type ReplayExchange,
} from "@/storybook/fixtures/delegation-replays"

vi.mock("@/lib/auth-client", () => ({
  buyerHeaders: async () => ({ Authorization: "Bearer buyer-token" }),
}))

// Cada respuesta sale de una secuencia real del backend (delegation-api/*.json).

type Sent = { method: string; path: string; authorization: string | null; body: unknown }
let sent: Sent[] = []

/** `fetch` responde, en orden, las respuestas de la secuencia para cada método y ruta. */
function replay(exchanges: readonly ReplayExchange[]) {
  const answered = new Map<string, number>()
  vi.stubGlobal("fetch", async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET"
    const path = url.slice(AGENT_API_URL.length)
    const route = `${method} ${path}`
    const nth = answered.get(route) ?? 0
    answered.set(route, nth + 1)
    sent.push({
      method,
      path,
      authorization: new Headers(init.headers).get("authorization"),
      body: typeof init.body === "string" ? JSON.parse(init.body) : null,
    })
    const exchange = replayed(exchanges, route, nth)
    return new Response(JSON.stringify(exchange.body), {
      status: exchange.status,
      headers: { "Content-Type": "application/json" },
    })
  })
}

beforeEach(() => {
  sent = []
})
afterEach(() => {
  vi.unstubAllGlobals()
})

const {
  consent,
  scopeMismatch,
  proposalExpired,
  proposalSuperseded,
  destinationUnsupported,
  revoke,
  revokeAll,
  notRevocable,
  flagOff,
} = DELEGATION_REPLAYS

const proposalBody = (exchanges: readonly ReplayExchange[]) =>
  replayed(exchanges, "POST /delegations/proposals").request as DelegationProposalRequest

describe("las rutas del comprador", () => {
  it("propone, autoriza y relee la lista con el token del comprador", async () => {
    replay(consent)
    expect((await myDelegations()).delegations).toEqual([])
    const proposal = await proposeDelegation(proposalBody(consent))
    const delegation = await consentToDelegation({
      proposal_id: proposal.proposal_id,
      scope_digest: proposal.scope_digest,
    })
    const mine = await myDelegations()

    expect(delegation.state).toBe("active")
    expect(delegation.scope).toEqual(proposal.scope)
    expect(mine.delegations.map((item) => item.grant_id)).toEqual([delegation.grant_id])
    expect(sent.map(({ method, path }) => `${method} ${path}`)).toEqual([
      "GET /me/delegations",
      "POST /delegations/proposals",
      "POST /delegations",
      "GET /me/delegations",
    ])
    expect(sent.every((call) => call.authorization === "Bearer buyer-token")).toBe(true)
    // Lo que se envía es lo que envió la secuencia original.
    expect(sent[1]?.body).toEqual(proposalBody(consent))
    expect(sent[2]?.body).toEqual(replayed(consent, "POST /delegations").request)
  })

  it("revoca una delegación y la relee", async () => {
    replay(revoke)
    const listed = (await myDelegations()).delegations
    const grantId = "dg_fae0f66102c54aa5b899fad0da0e88ca"
    const receipt = await revokeDelegation(grantId)
    const reread = await getDelegation(grantId)

    expect(listed.find((item) => item.grant_id === grantId)?.state).toBe("active")
    expect(receipt.grant_id).toBe(grantId)
    expect(receipt.effect).toBe("no_effect")
    expect(reread.state).toBe("revoked")
    expect(sent.map((call) => call.body)).toEqual([null, null, null])
  })

  it("«Revocar todo» devuelve un recibo por cada delegación activa", async () => {
    replay(revokeAll)
    const before = (await myDelegations()).delegations
    const { receipts } = await revokeAllDelegations()
    const active = before.filter((item) => item.state === "active").map((item) => item.grant_id)

    expect(receipts.map((receipt) => receipt.grant_id).sort()).toEqual(active.sort())
    expect(receipts.map((receipt) => receipt.effect).sort()).toEqual([
      "in_flight",
      "no_effect",
      "released",
    ])
  })
})

describe("los errores", () => {
  it.each([
    ["delegation_scope_mismatch", scopeMismatch],
    ["delegation_proposal_expired", proposalExpired],
    ["delegation_proposal_superseded", proposalSuperseded],
  ] as const)("%s pide una propuesta nueva y muestra el mensaje del backend", async (code, exchanges) => {
    replay(exchanges)
    const proposal = await proposeDelegation(proposalBody(exchanges))
    const error = await consentToDelegation(
      replayed(exchanges, "POST /delegations").request as { proposal_id: string; scope_digest: string },
    ).catch((reason: unknown) => reason)

    expect(proposal.proposal_id).toMatch(/^dpr_/)
    expect(error).toBeInstanceOf(AgentApiError)
    expect(delegationErrorCode(error)).toBe(code)
    expect(needsNewProposal(error)).toBe(true)
    expect(delegationErrorText(error)).toBe(
      (replayed(exchanges, "POST /delegations").body as { message: string }).message,
    )
  })

  it("una delegación que ya terminó (409) no se puede revocar ni pide otra propuesta", async () => {
    replay(notRevocable)
    const [delegation] = (await myDelegations()).delegations
    const error = await revokeDelegation(delegation!.grant_id).catch((reason: unknown) => reason)

    expect(delegationErrorCode(error)).toBe("delegation_not_revocable")
    expect(needsNewProposal(error)).toBe(false)
    expect(delegationErrorText(error)).toBe(
      "Esta delegación ya terminó, así que no hay nada que revocar.",
    )
    expect((await getDelegation(delegation!.grant_id)).state).toBe("expired")
  })

  it("con HNP apagado cada ruta es el 404 genérico, sin código propio", async () => {
    replay(flagOff)
    const error = await myDelegations().catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(AgentApiError)
    expect((error as AgentApiError).status).toBe(404)
    expect(delegationErrorCode(error)).toBeNull()
    expect(delegationErrorText(error)).toBe(
      "El agente no pudo atender la solicitud. Intenta de nuevo.",
    )
  })

  it("un destino que el comercio no atiende (422) se explica en la UI", async () => {
    replay(destinationUnsupported)
    await myDelegations()
    const error = await proposeDelegation(proposalBody(destinationUnsupported)).catch(
      (reason: unknown) => reason,
    )

    expect(delegationErrorCode(error)).toBe("delegation_destination_unsupported")
    expect(needsNewProposal(error)).toBe(false)
    expect(delegationErrorText(error)).toBe(
      "Por ahora el comercio solo entrega en Bogotá D.C. (departamento DC, código postal 110111). Revisa la dirección de entrega.",
    )
  })

  it("una sesión vencida, un 5xx y la red tienen su propio texto", () => {
    expect(delegationErrorText(new AgentApiError("x", "http_error", 401, null))).toMatch(
      /Tu sesión venció/,
    )
    expect(delegationErrorText(new AgentApiError("Traceback", "http_error", 500, null))).toBe(
      "El agente no pudo atender la solicitud. Intenta de nuevo.",
    )
    expect(delegationErrorText(new TypeError("Failed to fetch"))).toBe(
      "No pude comunicarme con el agente.",
    )
  })
})
