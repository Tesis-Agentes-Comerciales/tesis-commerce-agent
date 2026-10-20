import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, within } from "storybook/test"

import { scenarioBadge, type Scenario } from "./adapter.ts"
import { OPEN_GATES } from "./gates.ts"
import { ScenarioDetails } from "./GovernanceMonitor.tsx"
import { scenario } from "@/storybook/governance"

const meta = {
  title: "Governance/ScenarioDetails",
  component: ScenarioDetails,
  parameters: { layout: "padded" },
  decorators: [
    (Story) => (
      <main className="mx-auto max-w-6xl">
        <Story />
      </main>
    ),
  ],
} satisfies Meta<typeof ScenarioDetails>

export default meta
type Story = StoryObj<typeof meta>

type PlayContext = Parameters<NonNullable<Story["play"]>>[0]

/** Tarjeta de una decisión (Q shadow o D1 enforce) por su título. */
function decision({ canvas }: PlayContext, title: string) {
  const heading = canvas.getByText(title, { selector: "[data-slot=card-title]" })
  return within(heading.closest<HTMLElement>("[data-slot=card]")!)
}

/** El chip de estado sale de `scenarioBadge` y nunca dice «permitido» sin un allow D1 aplicado. */
async function expectBadge({ canvas, canvasElement }: PlayContext, s: Scenario) {
  await expect(canvas.getByText(scenarioBadge(s))).toBeVisible()
  if (s.enforce?.outcome !== "allow") await expect(canvasElement).not.toHaveTextContent(/permitid/i)
}

/** HP con un POST D17 histórico: creación y recuperación found; el veredicto D1 no se infiere. */
export const HpCreated: Story = {
  args: { scenario: scenario("hp-created") },
  play: async (context) => {
    await expectBadge(context, context.args.scenario)
    await expect(decision(context, "Q · shadow").getByText("allow")).toBeVisible()
    await expect(decision(context, "D1 · enforce").getAllByText("No disponible en la fuente")[0]).toBeVisible()
    await expect(context.canvas.getByText("HTTP/PostgreSQL real histórico · replay")).toBeVisible()
  },
}

/** Q allow en shadow y D1 deny en enforce: cero POST, creación y recuperación no aplicables. */
export const HpDenied: Story = {
  args: { scenario: scenario("hp-denied") },
  play: async (context) => {
    await expectBadge(context, context.args.scenario)
    await expect(decision(context, "Q · shadow").getByText("allow")).toBeVisible()
    await expect(decision(context, "D1 · enforce").getByText("deny")).toBeVisible()
    await expect(context.canvas.getByText("Bloqueada antes del envío")).toBeVisible()
    await expect(context.canvas.getAllByText("No aplicable")).toHaveLength(2)
  },
}

/** HNP-01 scripted offline: el accept erróneo del validador no supera el BACKEND_DENIED por budget. */
export const HnpFalseAccept: Story = {
  args: { scenario: scenario("hnp-false-accept") },
  play: async (context) => {
    const { canvas } = context
    await expectBadge(context, context.args.scenario)
    await expect(canvas.getByText("Validador HNP · scripted offline")).toBeVisible()
    await expect(canvas.getByText("budget: pass (validador)")).toBeVisible()
    const note = canvas.getByRole("note")
    await expect(note).toHaveTextContent("Discrepancia documentada entre validador y backend")
    await expect(note).toHaveTextContent("13.000")
    await expect(note).toHaveTextContent("12.500")
    await expect(canvas.getAllByText("BACKEND_DENIED").length).toBeGreaterThan(0)
    await expect(canvas.getByText("Ausencia de efectos observada en la fuente")).toBeVisible()
  },
}

/** Respuesta perdida: la creación inicial queda incierta y la recuperación posterior la confirma. */
export const HpLostResponse: Story = {
  args: { scenario: scenario("hp-uncertain") },
  play: async (context) => {
    await expectBadge(context, context.args.scenario)
    await expect(context.canvas.getByText("Incierta")).toBeVisible()
    await expect(context.canvas.getByText("Confirmada · found")).toBeVisible()
  },
}

/**
 * Render directo de un registro contradictorio (deny con efecto), la misma mutación en memoria que
 * los tests: la vista falla visible, sin chip ni creación confirmada. El parser nunca lo admite.
 */
export const Inconsistent: Story = {
  args: { scenario: { ...scenario("hp-denied"), effectOccurred: true } },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("alert")).toHaveTextContent("Evidencia inconsistente")
    await expect(canvas.getByText(OPEN_GATES)).toBeVisible()
    await expect(canvas.queryByText(/Creación histórica confirmada/)).toBeNull()
    await expect(canvas.queryByText("Operación bloqueada · sin creación")).toBeNull()
  },
}
