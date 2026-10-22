import { describe, expect, it } from "vitest"

import { messagePolicyBlock, policyBlockOf } from "@/features/policy-block"

const RUN_ID = "run_904404d3346ead96c6e88c9595c4fac3"

function prepare(code: string | null, overrides: Record<string, unknown> = {}) {
  return {
    type: "tool-call",
    toolName: "prepare_checkout",
    args: { variant_id: "SON-XM6-BLK", run_id: RUN_ID },
    ...(code === null
      ? {}
      : { result: { error: { code, message: "Texto del backend" } }, isError: true }),
    ...overrides,
  }
}

describe("policyBlockOf", () => {
  it.each([
    ["d1_denied", "denied"],
    ["d1_unavailable", "unavailable"],
    ["d1_allow_unapplied", "allow_unapplied"],
  ])("lee %s como un bloqueo de la política (%s)", (code, reason) => {
    expect(policyBlockOf(code)).toBe(reason)
  })

  it.each(["d1_dispatch_blocked", "checkout_terms_changed", "agent_error", undefined, 3])(
    "no lee %s como bloqueo",
    (code) => {
      expect(policyBlockOf(code)).toBeNull()
    },
  )
})

describe("messagePolicyBlock", () => {
  it("explica el bloqueo desde el paso fallido, sin esperar la traza", () => {
    expect(messagePolicyBlock([prepare("d1_denied")])).toEqual({ reason: "denied", runId: RUN_ID })
  })

  it("sin run_id en los args, el bloqueo sigue explicado", () => {
    expect(messagePolicyBlock([prepare("d1_unavailable", { args: {} })])).toEqual({
      reason: "unavailable",
      runId: null,
    })
  })

  it("un checkout en curso, preparado o con otro error no es un bloqueo", () => {
    expect(messagePolicyBlock([prepare(null)])).toBeNull()
    expect(
      messagePolicyBlock([prepare(null, { result: { run_id: RUN_ID, checkout_id: "co_1" } })]),
    ).toBeNull()
    expect(messagePolicyBlock([prepare("checkout_failed")])).toBeNull()
  })

  it("solo cuenta prepare_checkout", () => {
    expect(messagePolicyBlock([prepare("d1_denied", { toolName: "request_approval" })])).toBeNull()
  })
})
