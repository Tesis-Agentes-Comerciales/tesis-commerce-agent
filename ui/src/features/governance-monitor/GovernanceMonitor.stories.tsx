import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, within } from "storybook/test"

import { EXPERIMENT_NOTICE, SIMULATED_TRANSPORT_NOTICE, scenarioBadge } from "./adapter.ts"
import { GovernanceMonitor } from "./GovernanceMonitor.tsx"
import { HNP02_DRY_RUN, MONITOR_BUNDLE } from "@/storybook/governance"

const meta = {
  title: "Governance/GovernanceMonitor",
  component: GovernanceMonitor,
  args: { bundle: MONITOR_BUNDLE },
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof GovernanceMonitor>

export default meta
type Story = StoryObj<typeof meta>

/** Bundle versionado (governance-monitor/3): abre en el primer escenario, sin red ni acciones. */
export const Offline: Story = {
  play: async ({ canvas }) => {
    // Chips de la cabecera: solo lectura y el estado global del corte de evidencia.
    await expect(canvas.getByText("Solo lectura")).toBeVisible()
    await expect(canvas.getByText(MONITOR_BUNDLE.status)).toBeVisible()
    await expect(canvas.getByText("Sin LLM en vivo · sin ejecución comercial")).toBeVisible()
    const scenarios = canvas.getByRole("navigation", { name: "Escenarios de evidencia" })
    await expect(within(scenarios).getAllByRole("button")).toHaveLength(MONITOR_BUNDLE.scenarios.length)
    const experiments = canvas.getByRole("navigation", { name: "Experimentos HNP-02" })
    await expect(within(experiments).getAllByRole("button")).toHaveLength(MONITOR_BUNDLE.experiments.length)
    const first = MONITOR_BUNDLE.scenarios[0]
    await expect(within(scenarios).getByRole("button", { name: first.label })).toHaveAttribute("aria-pressed", "true")
    await expect(canvas.queryByRole("textbox")).toBeNull()
  },
}

/**
 * Recorre los cuatro escenarios y el experimento HNP-02. En cada vista revisa el chip de estado y
 * que «permitido» no aparezca: el corte versionado no tiene ningún allow D1 aplicado.
 */
export const Navigation: Story = {
  play: async ({ canvas, canvasElement, step, userEvent }) => {
    for (const s of MONITOR_BUNDLE.scenarios) {
      await step(s.label, async () => {
        await userEvent.click(canvas.getByRole("button", { name: s.label }))
        await expect(canvas.getByRole("button", { name: s.label })).toHaveAttribute("aria-pressed", "true")
        const detail = canvas.getByRole("region", { name: "Detalle del escenario" })
        await expect(within(detail).getByRole("heading", { level: 2 })).toHaveTextContent(s.label)
        await expect(within(detail).getByText(scenarioBadge(s))).toBeVisible()
        if (s.enforce?.outcome !== "allow") await expect(canvasElement).not.toHaveTextContent(/permitid/i)
      })
    }
    await step(HNP02_DRY_RUN.label, async () => {
      await userEvent.click(canvas.getByRole("button", { name: HNP02_DRY_RUN.label }))
      await expect(canvas.getByRole("button", { name: HNP02_DRY_RUN.label })).toHaveAttribute("aria-pressed", "true")
      for (const s of MONITOR_BUNDLE.scenarios) {
        await expect(canvas.getByRole("button", { name: s.label })).toHaveAttribute("aria-pressed", "false")
      }
      await expect(canvas.queryByRole("region", { name: "Detalle del escenario" })).toBeNull()
      const detail = canvas.getByRole("region", { name: "Detalle del experimento" })
      await expect(within(detail).getByText(EXPERIMENT_NOTICE)).toBeVisible()
      await expect(within(detail).getByText(SIMULATED_TRANSPORT_NOTICE)).toBeVisible()
      await expect(canvasElement).not.toHaveTextContent(/permitid/i)
    })
    await step("Volver al primer escenario", async () => {
      const first = MONITOR_BUNDLE.scenarios[0]
      await userEvent.click(canvas.getByRole("button", { name: first.label }))
      await expect(canvas.getByRole("region", { name: "Detalle del escenario" })).toBeVisible()
      await expect(canvas.queryByRole("region", { name: "Detalle del experimento" })).toBeNull()
    })
  },
}

export const Mobile: Story = {
  globals: { viewport: { value: "mobile", isRotated: false } },
}
