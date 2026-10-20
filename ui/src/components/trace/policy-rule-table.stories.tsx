import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { PolicyRuleTable } from "@/components/trace/policy-rule-table"
import { decisionOf, TRACES } from "@/storybook/trace-fixtures"

const meta = {
  title: "Trace/PolicyRuleTable",
  component: PolicyRuleTable,
  tags: ["autodocs"],
  args: { rules: decisionOf(TRACES.completedShadow).rules },
  decorators: [
    (Story) => (
      <div className="w-[min(36rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof PolicyRuleTable>

export default meta
type Story = StoryObj<typeof meta>

export const AllPass: Story = {
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Producto dentro de lo que pediste")).toBeVisible()
    await expect(canvas.getByText("Comercio permitido")).toBeVisible()
    await expect(canvas.getAllByText("Cumple")).toHaveLength(2)
    // Los códigos de regla son internos: no aparecen en la UI.
    await expect(canvas.queryByText(/POL-0/)).toBeNull()
  },
}

/** El comercio no está en la lista: se ve lo observado frente a lo esperado. */
export const OneFails: Story = {
  args: { rules: decisionOf(TRACES.blockedMerchant).rules },
  play: async ({ canvas }) => {
    const failed = canvas.getByText("Comercio permitido").closest("li")
    await expect(failed).toHaveAttribute("data-result", "fail")
    await expect(canvas.getByText("No cumple")).toBeVisible()
    await expect(canvas.getByText("Comercios permitidos")).toBeVisible()
    await expect(canvas.getByText("https://tienda-aliada.example.test")).toBeVisible()
  },
}

/** Como en la línea de tiempo del chat: solo la regla que falla muestra sus valores. */
export const FailedValuesOnly: Story = {
  args: { rules: decisionOf(TRACES.blockedMerchant).rules, values: "failed" },
  play: async ({ canvas }) => {
    await expect(canvas.queryByText("Productos que cumplen tu pedido")).toBeNull()
    await expect(canvas.getByText("Comercios permitidos")).toBeVisible()
  },
}

export const ProductOutsideRequest: Story = {
  args: { rules: decisionOf(TRACES.blockedProduct).rules },
}

/** La política no respondió: ninguna regla se evaluó y no hay valores que mostrar. */
export const NotEvaluated: Story = {
  args: { rules: decisionOf(TRACES.policyUnavailable).rules },
  play: async ({ canvas }) => {
    await expect(canvas.getAllByText("Sin evaluar")).toHaveLength(2)
    await expect(canvas.queryByRole("term")).toBeNull()
  },
}

/** Teléfono de 320 px: término y valor se apilan y la URL usa el ancho de la fila. */
export const Phone320: Story = {
  args: { rules: decisionOf(TRACES.blockedMerchant).rules },
  decorators: [
    (Story) => (
      <div className="w-[288px]">
        <Story />
      </div>
    ),
  ],
  play: async ({ canvas }) => {
    const term = canvas.getByText("Comercios permitidos")
    const value = canvas.getByText("https://tienda-aliada.example.test")
    await expect(value.getBoundingClientRect().top).toBeGreaterThan(term.getBoundingClientRect().bottom - 1)
    await expect(value.getBoundingClientRect().left).toBe(term.getBoundingClientRect().left)
  },
}
