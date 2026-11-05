import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { parsePurchaseTrace, type PurchaseTrace, type StepKind } from "@/lib/trace-contract"
import { TRACES } from "@/storybook/trace-fixtures"
import { HNP_TRACES } from "@/storybook/trace-fixtures/hnp"

// Prueba del contrato HNP del lado UI: valida cada JSON de la carpeta y rechaza los mismos casos
// que tests/test_hnp_trace_contract.py. Las fixtures HP no traen `purchase_mode` y siguen valiendo.

/** Lo que solo existe en una compra delegada, más los pasos HP que reutiliza. */
const HNP_KINDS: StepKind[] = [
  "delegation_consented",
  "task_activated",
  "choice_reviewed",
  "policy_decision",
  "checkout_created",
  "freshness_check",
  "authorization_decision",
  "reservation",
  "mandates_closed",
  "dispatch_decision",
  "payment_authorized",
  "order_confirmed",
  "revoked",
  "escalated",
  "failed",
]

const FILES = import.meta.glob<unknown>("./*.json", { eager: true, import: "default" })

function FixtureCatalog() {
  return (
    <table className="text-left text-sm">
      <caption className="pb-2 text-left font-semibold">Fixtures de PurchaseTrace HNP</caption>
      <thead className="text-xs text-[#6b6b6b]">
        <tr>
          <th className="pr-6 font-medium">Fixture</th>
          <th className="pr-6 font-medium">status</th>
          <th className="pr-6 font-medium">policy_mode</th>
          <th className="font-medium">Pasos</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(HNP_TRACES).map(([name, trace]) => (
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
  title: "Trace/Contract HNP",
  component: FixtureCatalog,
} satisfies Meta<typeof FixtureCatalog>

export default meta
type Story = StoryObj<typeof meta>

/** Cada archivo cumple el contrato, es HNP y, entre todos, cubren los pasos de una compra delegada. */
export const Fixtures: Story = {
  play: async () => {
    const files = Object.entries(FILES)
    await expect(files).toHaveLength(Object.keys(HNP_TRACES).length)
    for (const [file, json] of files) {
      await expect(() => parsePurchaseTrace(json), file).not.toThrow()
    }
    const traces = Object.values(HNP_TRACES)
    await expect(traces.every((trace) => trace.purchase_mode === "HNP")).toBe(true)
    const kinds = new Set(traces.flatMap((trace) => trace.steps.map((step) => step.kind)))
    await expect([...kinds].sort()).toEqual(HNP_KINDS.toSorted())
    const stages = traces.flatMap((trace) =>
      trace.steps.flatMap((step) => (step.kind === "freshness_check" ? [step.stage] : [])),
    )
    await expect(new Set(stages)).toEqual(new Set(["close"]))
  },
}

type Mutable = Record<string, unknown> & { steps: Record<string, unknown>[] }

function rejects(fixture: PurchaseTrace, mutate: (trace: Mutable) => void) {
  const trace = structuredClone(fixture) as unknown as Mutable
  mutate(trace)
  return () => parsePurchaseTrace(trace)
}

function index(trace: PurchaseTrace, kind: StepKind) {
  return trace.steps.findIndex((step) => step.kind === kind)
}

export const RejectsInvalidDelegatedSteps: Story = {
  play: async () => {
    const happy = HNP_TRACES.enforceCompleted
    const at = (kind: StepKind) => `trace.steps[${index(happy, kind)}]`
    const step = (trace: Mutable, kind: StepKind) => trace.steps[index(happy, kind)]
    await expect(rejects(happy, (trace) => (trace.purchase_mode = "AI"))).toThrow(
      "trace.purchase_mode",
    )
    await expect(rejects(happy, (trace) => (step(trace, "task_activated").trigger = "user"))).toThrow(
      `${at("task_activated")}.trigger`,
    )
    await expect(
      rejects(happy, (trace) => (step(trace, "delegation_consented").total_max = 200000000)),
    ).toThrow(`${at("delegation_consented")}.total_max`)
    await expect(
      rejects(happy, (trace) => (step(trace, "reservation").outcome = "committed")),
    ).toThrow(`${at("reservation")}.outcome`)
    await expect(rejects(happy, (trace) => (step(trace, "mandates_closed").mandates = []))).toThrow(
      `${at("mandates_closed")}.mandates`,
    )
    await expect(rejects(happy, (trace) => (step(trace, "freshness_check").stage = "dispatch"))).toThrow(
      `${at("freshness_check")}.stage`,
    )
    await expect(rejects(happy, (trace) => delete step(trace, "dispatch_decision").decision)).toThrow(
      `${at("dispatch_decision")}.decision`,
    )
    await expect(rejects(happy, (trace) => (step(trace, "payment_authorized").max_minor = -1))).toThrow(
      `${at("payment_authorized")}.max_minor`,
    )

    const revoked = HNP_TRACES.revokedInFlight
    const last = revoked.steps.length - 1
    await expect(rejects(revoked, (trace) => (trace.steps[last].effect = "canceled"))).toThrow(
      `trace.steps[${last}].effect`,
    )
    await expect(rejects(revoked, (trace) => (trace.steps[last].actor = "admin"))).toThrow(
      `trace.steps[${last}].actor`,
    )
    const escalated = HNP_TRACES.d2Denied
    const final = escalated.steps.length - 1
    await expect(rejects(escalated, (trace) => (trace.steps[final].reason = "policy_denied"))).toThrow(
      `trace.steps[${final}].reason`,
    )
    const review = HNP_TRACES.enforceReviewNotConfirmed
    const reviewed = index(review, "choice_reviewed")
    await expect(rejects(review, (trace) => (trace.steps[reviewed].decision = "rejected"))).toThrow(
      `trace.steps[${reviewed}].decision`,
    )
    await expect(rejects(review, (trace) => (trace.steps[reviewed].reason = "model_said_no"))).toThrow(
      `trace.steps[${reviewed}].reason`,
    )
    await expect(rejects(review, (trace) => (trace.steps[reviewed].rationale = "otra"))).toThrow(
      `trace.steps[${reviewed}].rationale`,
    )
    const refused = HNP_TRACES.checkoutRefused
    const task = refused.steps.length - 1
    await expect(
      rejects(refused, (trace) => (trace.steps[task].merchant_refusal = "payment_declined")),
    ).toThrow(`trace.steps[${task}].merchant_refusal`)
    // Una traza HP no trae `purchase_mode`.
    await expect(() => parsePurchaseTrace(TRACES.completedShadow)).not.toThrow()
    await expect(TRACES.completedShadow.purchase_mode).toBeUndefined()
  },
}
