import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, within } from "storybook/test"

import { EXPERIMENT_NOTICE, SIMULATED_TRANSPORT_NOTICE } from "./adapter.ts"
import { ExperimentDetails } from "./ExperimentDetails.tsx"
import { HNP02_DRY_RUN } from "@/storybook/governance"

const meta = {
  title: "Governance/ExperimentDetails",
  component: ExperimentDetails,
  args: { experiment: HNP02_DRY_RUN },
  parameters: { layout: "padded" },
  decorators: [
    (Story) => (
      <main className="mx-auto max-w-6xl">
        <Story />
      </main>
    ),
  ],
} satisfies Meta<typeof ExperimentDetails>

export default meta
type Story = StoryObj<typeof meta>

type PlayContext = Parameters<NonNullable<Story["play"]>>[0]

/** Los dos avisos obligatorios abren la vista, antes del título del experimento. */
async function expectNotices({ canvas }: PlayContext) {
  const banner = canvas.getAllByRole("note")[0]
  await expect(banner).toHaveTextContent(EXPERIMENT_NOTICE)
  await expect(banner).toHaveTextContent(SIMULATED_TRANSPORT_NOTICE)
  const heading = canvas.getByRole("heading", { level: 2 })
  await expect(banner.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
}

/**
 * Corrida HNP-02 versionada en dry-run (FakeTransport): brazo A guionado frente a B con validador,
 * sobre un sink ficticio. Abre un caso donde el validador declaró pass y el backend denegó.
 */
export const DryRun: Story = {
  play: async (context) => {
    const { canvas, canvasElement, userEvent } = context
    await expectNotices(context)
    await expect(canvas.getByText("Experimento · sin operación real")).toBeVisible()
    await expect(canvas.getByText(/transporte simulado \(FakeTransport\) · sin proveedor/)).toBeVisible()
    await expect(canvasElement).not.toHaveTextContent(/proveedor real/)
    await expect(canvasElement).not.toHaveTextContent(/permitid/i)
    await userEvent.click(canvas.getByText(/^over-total ·/))
    const item = within(canvas.getByText(/^over-total ·/).closest("details")!)
    await expect(item.getByText("budget: pass (validador)")).toBeVisible()
    await expect(item.getByRole("note")).toHaveTextContent(
      "budget: pass declarado por el validador; denegado por el backend del par",
    )
  },
}

/**
 * Render directo con un tipo de ejecución que el parser no admite: la vista no muestra cifras, pero
 * conserva ambos avisos. El parser rechaza la corrida entera antes de llegar aquí.
 */
export const Inconsistent: Story = {
  args: { experiment: { ...HNP02_DRY_RUN, executionKind: "llm validator live" as never } },
  play: async (context) => {
    await expectNotices(context)
    await expect(context.canvas.getByRole("alert")).toHaveTextContent("Evidencia inconsistente")
    await expect(context.canvas.queryByText("Brazos A/B por episodio")).toBeNull()
  },
}

export const Mobile: Story = {
  globals: { viewport: { value: "mobile", isRotated: false } },
}
