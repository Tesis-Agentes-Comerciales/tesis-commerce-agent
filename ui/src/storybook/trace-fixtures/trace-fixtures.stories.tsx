import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import {
  parsePurchaseTrace,
  type PurchaseStatus,
  type PurchaseTrace,
  type StepKind,
} from "@/lib/trace-contract"
import { TRACES } from "@/storybook/trace-fixtures"

// Prueba del contrato del lado UI: valida cada JSON de la carpeta (también los que nadie importa
// todavía) y comprueba que el validador rechaza trazas mal formadas con la ruta del campo. El
// catálogo es para desarrollo: muestra los valores crudos del contrato.

const STATUSES: PurchaseStatus[] = [
  "in_progress",
  "awaiting_approval",
  "completed",
  "blocked",
  "stale_terms",
  "canceled",
  "failed",
  "uncertain",
]

const STEP_KINDS: StepKind[] = [
  "request",
  "selection",
  "policy_decision",
  "checkout_created",
  "approval",
  "authorization_decision",
  "authorization_signed",
  "payment_authorized",
  "order_confirmed",
  "order_event",
  "canceled",
  "failed",
  "freshness_check",
  "governed_recovery",
]

const FILES = import.meta.glob<unknown>("./*.json", { eager: true, import: "default" })

function FixtureCatalog() {
  return (
    <table className="text-left text-sm">
      <caption className="pb-2 text-left font-semibold">Fixtures de PurchaseTrace</caption>
      <thead className="text-xs text-[#6b6b6b]">
        <tr>
          <th className="pr-6 font-medium">Fixture</th>
          <th className="pr-6 font-medium">status</th>
          <th className="pr-6 font-medium">policy_mode</th>
          <th className="font-medium">Pasos</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(TRACES).map(([name, trace]) => (
          <tr key={name} className="border-t border-[#ececec]">
            <td className="py-2 pr-6 font-mono text-xs">{name}</td>
            <td className="py-2 pr-6 font-mono text-xs">{trace.status}</td>
            <td className="py-2 pr-6 font-mono text-xs">{trace.policy_mode}</td>
            <td className="py-2 tabular-nums">{trace.steps.length}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

const meta = {
  title: "Trace/Contract",
  component: FixtureCatalog,
} satisfies Meta<typeof FixtureCatalog>

export default meta
type Story = StoryObj<typeof meta>

/** Cada archivo cumple el contrato y, entre todos, cubren cada estado y cada tipo de paso. */
export const Fixtures: Story = {
  play: async () => {
    const files = Object.entries(FILES)
    await expect(files).toHaveLength(Object.keys(TRACES).length)
    for (const [file, json] of files) {
      await expect(() => parsePurchaseTrace(json), file).not.toThrow()
    }
    const traces = Object.values(TRACES)
    const statuses = new Set(traces.map((trace) => trace.status))
    await expect([...statuses].sort()).toEqual(STATUSES.toSorted())
    const kinds = new Set(traces.flatMap((trace) => trace.steps.map((step) => step.kind)))
    await expect([...kinds].sort()).toEqual(STEP_KINDS.toSorted())
  },
}

type Mutation = (trace: Record<string, unknown> & { steps: Record<string, unknown>[] }) => void

function rejects(mutate: Mutation, fixture: PurchaseTrace = TRACES.completedShadow) {
  const trace = structuredClone(fixture) as unknown as Parameters<Mutation>[0]
  mutate(trace)
  return () => parsePurchaseTrace(trace as unknown as PurchaseTrace)
}

export const RejectsInvalidTraces: Story = {
  play: async () => {
    await expect(rejects((trace) => (trace.status = "done"))).toThrow("trace.status")
    await expect(rejects((trace) => (trace.owner_id = "u-1"))).toThrow("trace.owner_id")
    await expect(rejects((trace) => delete trace.merchant)).toThrow("trace.merchant")
    await expect(rejects((trace) => (trace.started_at = "2026-10-20 15:41"))).toThrow(
      "trace.started_at",
    )
    await expect(rejects((trace) => (trace.steps[0].kind = "refund"))).toThrow("trace.steps[0].kind")
    await expect(rejects((trace) => (trace.steps[1].total_minor = 12.5))).toThrow(
      "trace.steps[1].total_minor",
    )
    await expect(
      rejects((trace) => ((trace.steps[2].decision as { rules: unknown[] }).rules = [{}])),
    ).toThrow("trace.steps[2].decision.rules[0].id")
  },
}

/** El paso de la autorización al aprobar (D2): los mismos casos que tests/test_trace_contract.py. */
export const RejectsInvalidAuthorization: Story = {
  play: async () => {
    const denied = (mutate: Mutation) => rejects(mutate, TRACES.authorizationDenied)
    const index = TRACES.authorizationDenied.steps.findIndex(
      (step) => step.kind === "authorization_decision",
    )
    const path = `trace.steps[${index}]`
    const step = (trace: Parameters<Mutation>[0]) => trace.steps[index]
    const decision = (trace: Parameters<Mutation>[0]) =>
      step(trace).decision as { effect: string; rules: Record<string, unknown>[] }
    await expect(denied((trace) => (step(trace).evidence = "x"))).toThrow(`${path}.evidence`)
    await expect(denied((trace) => delete step(trace).decision)).toThrow(`${path}.decision`)
    await expect(denied((trace) => (decision(trace).effect = "denied"))).toThrow(
      `${path}.decision.effect`,
    )
    await expect(denied((trace) => (decision(trace).rules[3].observed = ""))).toThrow(
      `${path}.decision.rules[3].observed`,
    )
  },
}

type Change = Record<string, unknown>

/** Último paso de la fixture S04: la comprobación de frescura que bloqueó. */
const blocking = (trace: Parameters<Mutation>[0]) => trace.steps[trace.steps.length - 1]
const firstChange = (trace: Parameters<Mutation>[0]) => (blocking(trace).changes as Change[])[0]
const recovery = (trace: Parameters<Mutation>[0]) => trace.steps[trace.steps.length - 1]

/** Los mismos casos que tests/test_trace_contract.py rechaza con Pydantic. */
export const RejectsInvalidApplicationControls: Story = {
  play: async () => {
    const stale = (mutate: Mutation) => rejects(mutate, TRACES.staleTerms)
    const last = TRACES.staleTerms.steps.length - 1
    const path = `trace.steps[${last}]`
    await expect(stale((trace) => (blocking(trace).stage = "complete"))).toThrow(`${path}.stage`)
    await expect(stale((trace) => (blocking(trace).outcome = "stale"))).toThrow(`${path}.outcome`)
    await expect(stale((trace) => (blocking(trace).decision = "block"))).toThrow(`${path}.decision`)
    await expect(stale((trace) => (blocking(trace).evidence_digest = ""))).toThrow(
      `${path}.evidence_digest`,
    )
    await expect(stale((trace) => (blocking(trace).offer_revision = -1))).toThrow(
      `${path}.offer_revision`,
    )
    await expect(stale((trace) => (firstChange(trace).field = "price"))).toThrow(
      `${path}.changes[0].field`,
    )
    await expect(stale((trace) => (firstChange(trace).presented = "1899000"))).toThrow(
      `${path}.changes[0].presented`,
    )
    await expect(stale((trace) => (firstChange(trace).field = "currency"))).toThrow(
      `${path}.changes[0].presented`,
    )
    await expect(
      stale((trace) => {
        Object.assign(firstChange(trace), {
          field: "expires_at",
          presented: "2026-10-20T16:25:31+00:00",
          current: null,
        })
      }),
    ).toThrow(`${path}.changes[0].presented`)
    await expect(stale((trace) => (firstChange(trace).compared = { checkout: 1 }))).toThrow(
      `${path}.changes[0].compared`,
    )

    const governed = (mutate: Mutation) => rejects(mutate, TRACES.governedRecovery)
    const step = `trace.steps[${TRACES.governedRecovery.steps.length - 1}]`
    await expect(governed((trace) => (recovery(trace).cause = "policy"))).toThrow(`${step}.cause`)
    await expect(governed((trace) => (recovery(trace).recovery = "retry"))).toThrow(
      `${step}.recovery`,
    )
    await expect(
      governed((trace) => (recovery(trace).explanation_sha256 = "a".repeat(64))),
    ).toThrow(`${step}.explanation_sha256`)
    await expect(governed((trace) => (recovery(trace).budget_minor = -1))).toThrow(
      `${step}.budget_minor`,
    )
  },
}
