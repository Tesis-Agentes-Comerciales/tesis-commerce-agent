import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import {
  parseDelegationList,
  parseDelegationProposal,
  parseDelegationRevokeAllReceipt,
  parseDelegationRevokeReceipt,
  parseDelegationView,
} from "@/lib/delegation-contract"
import {
  DELEGATION_PROPOSAL,
  DELEGATIONS,
  REVOKE_RECEIPTS,
} from "@/storybook/fixtures/delegation-fixtures"

// Prueba del contrato de delegaciones del lado UI (paso 2 de docs/workflow.md): valida cada JSON de
// la carpeta y comprueba que los validadores rechazan lo mismo que tests/test_delegation_contract.py,
// con la ruta del campo. No es una tarjeta (esas son de HNP-07): el catálogo muestra los valores
// crudos del contrato para desarrollo.

const FILES = import.meta.glob<unknown>("./delegation-*.json", { eager: true, import: "default" })

/** Cada fixture con su validador: la propuesta, los recibos y las vistas de una delegación. */
const PARSERS: Record<string, (value: unknown) => unknown> = {
  "./delegation-proposed.json": parseDelegationProposal,
  "./delegation-revoke-receipts.json": parseDelegationRevokeAllReceipt,
}
const parserOf = (file: string) => PARSERS[file] ?? parseDelegationView

function FixtureCatalog() {
  return (
    <table className="text-left text-sm">
      <caption className="pb-2 text-left font-semibold">Fixtures de DelegationView</caption>
      <thead className="text-xs text-[#6b6b6b]">
        <tr>
          <th className="pr-6 font-medium">Fixture</th>
          <th className="pr-6 font-medium">state</th>
          <th className="pr-6 font-medium">task.state</th>
          <th className="font-medium">task.outcome_code</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(DELEGATIONS).map(([name, delegation]) => (
          <tr key={name} className="border-t border-[#ececec]">
            <td className="py-2 pr-6 font-mono text-xs">{name}</td>
            <td className="py-2 pr-6 font-mono text-xs">{delegation.state}</td>
            <td className="py-2 pr-6 font-mono text-xs">{delegation.task.state}</td>
            <td className="py-2 font-mono text-xs">{delegation.task.outcome_code ?? "null"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

const meta = {
  title: "Delegation/Contract",
  component: FixtureCatalog,
} satisfies Meta<typeof FixtureCatalog>

export default meta
type Story = StoryObj<typeof meta>

/** Cada archivo cumple su contrato y, entre todos, cubren cada estado de la delegación y la tarea. */
export const Fixtures: Story = {
  play: async () => {
    const files = Object.entries(FILES)
    await expect(files).toHaveLength(Object.keys(DELEGATIONS).length + 2)
    for (const [file, json] of files) {
      await expect(() => parserOf(file)(json), file).not.toThrow()
    }
    const states = Object.fromEntries(
      Object.entries(DELEGATIONS).map(([name, item]) => [name, [item.state, item.task.state]]),
    )
    await expect(states).toEqual({
      active: ["active", "scheduled"],
      running: ["active", "running"],
      purchased: ["consumed", "purchased"],
      escalated: ["revoked", "escalated"],
      revoked: ["revoked", "canceled"],
      expired: ["expired", "canceled"],
      failed: ["revoked", "failed"],
      uncertain: ["active", "uncertain"],
    })
    await expect(REVOKE_RECEIPTS.receipts.map((receipt) => receipt.effect)).toEqual([
      "no_effect",
      "released",
      "in_flight",
    ])
    await expect(DELEGATION_PROPOSAL.scope.uses).toBe(1)
    const delegations = Object.values(DELEGATIONS)
    await expect(parseDelegationList({ delegations })).toEqual({ delegations })
  },
}

type Item = Record<string, unknown>

/** El objeto anidado en esa ruta de la fixture, para mutarlo. */
const at = (item: Item, ...keys: (string | number)[]) =>
  keys.reduce<unknown>((value, key) => (value as Item)[key], item) as Item

function failureOf(name: string, mutate: (item: Item) => void) {
  const file = `./delegation-${name}.json`
  const item = structuredClone(FILES[file]) as Item
  mutate(item)
  try {
    parserOf(file)(item)
    return null
  } catch (error) {
    return (error as Error).message
  }
}

type Case = [id: string, fixture: string, mutate: (item: Item) => void, path: string]

/** Los mismos casos, con el mismo id, que `REJECTED` en tests/test_delegation_contract.py. */
const REJECTED: Case[] = [
  // Campos fuera del contrato, en cada nivel.
  ["proposal-extra", "proposed", (item) => (item.owner_id = "buyer-a"), "proposal.owner_id"],
  [
    "scope-extra",
    "proposed",
    (item) => (at(item, "scope").address = "Calle 1"),
    "proposal.scope.address",
  ],
  [
    "variant-extra",
    "proposed",
    (item) => (at(item, "scope", "variants", 0).price = 1),
    "proposal.scope.variants[0].price",
  ],
  [
    "money-extra",
    "proposed",
    (item) => (at(item, "scope", "total_max").display = "COP 2.000.000"),
    "proposal.scope.total_max.display",
  ],
  [
    "view-digest",
    "active",
    (item) => (item.scope_digest = `sha256:${"a".repeat(64)}`),
    "delegation.scope_digest",
  ],
  ["task-extra", "running", (item) => (at(item, "task").lease_id = "l-1"), "delegation.task.lease_id"],
  [
    "escalation-extra",
    "escalated",
    (item) => (at(item, "escalation").checkout_id = "c-1"),
    "delegation.escalation.checkout_id",
  ],
  [
    "receipt-extra",
    "revoke-receipts",
    (item) => (at(item, "receipts", 0).reservation_id = "rsv_1"),
    "receipts.receipts[0].reservation_id",
  ],
  ["receipts-extra", "revoke-receipts", (item) => (item.count = 3), "receipts.count"],
  // Valores fuera del contrato.
  ["two-uses", "proposed", (item) => (at(item, "scope").uses = 2), "proposal.scope.uses"],
  ["boolean-uses", "proposed", (item) => (at(item, "scope").uses = true), "proposal.scope.uses"],
  ["no-variants", "proposed", (item) => (at(item, "scope").variants = []), "proposal.scope.variants"],
  [
    "six-variants",
    "proposed",
    (item) => {
      const variants = at(item, "scope").variants as unknown[]
      at(item, "scope").variants = [...variants, ...variants, ...variants]
    },
    "proposal.scope.variants",
  ],
  [
    "empty-preference",
    "proposed",
    (item) => (at(item, "scope").preference_text = ""),
    "proposal.scope.preference_text",
  ],
  [
    "long-preference",
    "proposed",
    (item) => (at(item, "scope").preference_text = "ñ".repeat(281)),
    "proposal.scope.preference_text",
  ],
  ["short-digest", "proposed", (item) => (item.scope_digest = "sha256:abc"), "proposal.scope_digest"],
  [
    "offset-time",
    "proposed",
    (item) => (item.presented_at = "2026-10-21T15:00:00+00:00"),
    "proposal.presented_at",
  ],
  ["proposal-id-prefix", "proposed", (item) => (item.proposal_id = "dg_1"), "proposal.proposal_id"],
  ["grant-id-prefix", "active", (item) => (item.grant_id = "ht_1"), "delegation.grant_id"],
  ["unknown-grant-state", "active", (item) => (item.state = "pending"), "delegation.state"],
  ["unknown-task", "purchased", (item) => (at(item, "task").state = "done"), "delegation.task.state"],
  [
    "reason-while-active",
    "active",
    (item) => (item.revoked_reason = "owner"),
    "delegation.revoked_reason",
  ],
  [
    "revoked-without-reason",
    "revoked",
    (item) => (item.revoked_reason = null),
    "delegation.revoked_reason",
  ],
  [
    "code-while-scheduled",
    "active",
    (item) => (at(item, "task").outcome_code = "kill_switch"),
    "delegation.task.outcome_code",
  ],
  [
    "failed-without-code",
    "failed",
    (item) => (at(item, "task").outcome_code = null),
    "delegation.task.outcome_code",
  ],
  [
    "code-of-another-state",
    "revoked",
    (item) => (at(item, "task").outcome_code = "dispatch_timeout"),
    "delegation.task.outcome_code",
  ],
  ["missing-escalation", "escalated", (item) => (item.escalation = null), "delegation.escalation"],
  [
    "escalation-without-escalated-task",
    "failed",
    (item) => (item.escalation = structuredClone(DELEGATIONS.escalated.escalation)),
    "delegation.escalation",
  ],
  [
    "escalation-of-another-code",
    "escalated",
    (item) => (at(item, "escalation").reason = "offer_changed"),
    "delegation.escalation.reason",
  ],
  [
    "closed-grant-released",
    "revoke-receipts",
    (item) => (at(item, "receipts", 1).revoked_reason = "operator"),
    "receipts.receipts[1].effect",
  ],
  [
    "receipt-not-revoked",
    "revoke-receipts",
    (item) => (at(item, "receipts", 0).state = "active"),
    "receipts.receipts[0].state",
  ],
]

/** Cada caso falla en el campo exacto que cambió, no en otro. */
export const RejectsInvalidDelegations: Story = {
  play: async () => {
    for (const [id, fixture, mutate, path] of REJECTED) {
      await expect(failureOf(fixture, mutate), id).toBe(`Invalid delegation contract at ${path}`)
    }
    await expect(() => parseDelegationList({ delegations: [], next_cursor: null })).toThrow(
      "delegations.next_cursor",
    )
  },
}

/** Los casos válidos que las reglas entre campos no deben rechazar (§5.1 y §8.1). */
export const AcceptsClosedStatesWithoutEffect: Story = {
  play: async () => {
    // B0-HNP nunca escala: falla con el mismo código y sin EscalationView.
    const b0 = failureOf("failed", (item) => {
      at(item, "task").outcome_code = "no_candidate_in_scope"
    })
    await expect(b0).toBeNull()
    // Revocar una delegación que ya cerró el operador responde `no_effect`.
    const receipt = structuredClone(REVOKE_RECEIPTS.receipts[0])
    const closed = { ...receipt, revoked_reason: "operator" }
    await expect(parseDelegationRevokeReceipt(closed)).toEqual(closed)
  },
}
