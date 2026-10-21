import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { PolicyRuleTable } from "@/components/trace/policy-rule-table"
import { traceMerchant } from "@/components/trace/trace-copy"
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

/**
 * Con el comercio de la compra, el valor observado lleva su nombre y, debajo, el origen que evaluó
 * la política. La lista esperada sigue en orígenes: es lo que dice la política.
 */
export const NamesTheMerchant: Story = {
  args: {
    rules: decisionOf(TRACES.blockedMerchant).rules,
    merchant: traceMerchant(TRACES.blockedMerchant),
  },
  play: async ({ canvas }) => {
    const name = canvas.getByText("Synthetic Commerce Merchant")
    const origin = canvas.getByText("http://127.0.0.1:4120")
    await expect(name.tagName).toBe("DD")
    await expect(origin.getBoundingClientRect().top).toBeGreaterThan(name.getBoundingClientRect().top)
    await expect(origin).toHaveClass("font-mono")
    await expect(canvas.getByText("https://tienda-aliada.example.test")).toBeVisible()
  },
}

/** Otro comercio que el de la compra: no se le pone un nombre que no es suyo. */
export const OtherMerchantKeepsOrigin: Story = {
  args: {
    rules: decisionOf(TRACES.blockedMerchant).rules,
    merchant: { origin: "https://otro.example.test", name: "Otro comercio" },
  },
  play: async ({ canvas }) => {
    await expect(canvas.queryByText("Otro comercio")).toBeNull()
    await expect(canvas.getByText("http://127.0.0.1:4120")).toBeVisible()
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

// Como `formatMoney`: el separador entre el símbolo y el monto es un espacio duro.
const CATALOG_PRICE = /^\$\s?1\.540\.000(,00)? COP$/
const TOTAL_BUDGET = /^\$\s?1\.000\.000(,00)? COP$/

/**
 * S02 con `b2-hp-v2`: el precio del catálogo ya supera el presupuesto total. Los dos valores son
 * montos y se escriben como el resto de los montos de la traza, no como un valor técnico.
 */
export const BudgetFails: Story = {
  args: { rules: decisionOf(TRACES.blockedBudget).rules },
  play: async ({ canvas }) => {
    const failed = canvas.getByText("Precio dentro de tu presupuesto").closest("li")
    await expect(failed).toHaveAttribute("data-result", "fail")
    await expect(canvas.getAllByText("Cumple")).toHaveLength(2)
    await expect(canvas.getByText("Precio en el catálogo")).toBeVisible()
    await expect(canvas.getByText("Tu presupuesto total")).toBeVisible()
    const price = canvas.getByText(CATALOG_PRICE)
    await expect(price).toBeVisible()
    await expect(price).not.toHaveClass("font-mono")
    await expect(canvas.getByText(TOTAL_BUDGET)).toBeVisible()
    await expect(canvas.queryByText(/POL-11|BUDGET_NOT_SATISFIABLE/)).toBeNull()
  },
}

/** En la línea de tiempo del chat, solo la regla de presupuesto muestra sus montos. */
export const BudgetFailedValuesOnly: Story = {
  args: { rules: decisionOf(TRACES.blockedBudget).rules, values: "failed" },
  play: async ({ canvas }) => {
    await expect(canvas.queryByText("Comercios permitidos")).toBeNull()
    await expect(canvas.getByText(CATALOG_PRICE)).toBeVisible()
    await expect(canvas.getByText(TOTAL_BUDGET)).toBeVisible()
  },
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
