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
  "authorization_signed",
  "payment_authorized",
  "order_confirmed",
  "order_event",
  "canceled",
  "failed",
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

function rejects(mutate: Mutation) {
  const trace = structuredClone(TRACES.completedShadow) as unknown as Parameters<Mutation>[0]
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
