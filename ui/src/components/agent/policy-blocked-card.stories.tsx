import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { PolicyBlockedCard } from "@/components/agent/policy-blocked-card"
import { decisionOf, TRACES } from "@/storybook/trace-fixtures"

const NOTHING_HAPPENED = "No se creó ningún checkout ni se hizo ningún cobro."

const meta = {
  title: "Agent/PolicyBlockedCard",
  component: PolicyBlockedCard,
  tags: ["autodocs"],
  args: { decision: decisionOf(TRACES.blockedMerchant) },
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof PolicyBlockedCard>

export default meta
type Story = StoryObj<typeof meta>

/** Modo activo con una lista de comercios que excluye la tienda. */
export const MerchantNotAllowed: Story = {
  play: async ({ canvas }) => {
    await expect(
      canvas.getByRole("heading", { name: "La política de compra detuvo esta compra" }),
    ).toBeVisible()
    await expect(canvas.getByText("Comercio permitido")).toBeVisible()
    await expect(canvas.getByText("http://127.0.0.1:4120")).toBeVisible()
    await expect(canvas.getByText("https://tienda-aliada.example.test")).toBeVisible()
    await expect(canvas.getByText(NOTHING_HAPPENED)).toBeVisible()
    // Solo la regla que falló, y sin jerga ni «error inesperado» fuera del detalle técnico.
    await expect(canvas.queryByText("Producto dentro de lo que pediste")).toBeNull()
    await expect(canvas.queryByText(/error inesperado/i)).toBeNull()
    await expect(canvas.getByText("ACTION_OR_MERCHANT_NOT_ALLOWED")).not.toBeVisible()
  },
}

export const ProductOutsideRequest: Story = {
  args: { decision: decisionOf(TRACES.blockedProduct) },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Producto dentro de lo que pediste")).toBeVisible()
    await expect(canvas.getByText("SON-XM6-SLV")).toBeVisible()
    await expect(canvas.getByText(NOTHING_HAPPENED)).toBeVisible()
  },
}

/** La política no respondió: se detiene por precaución, sin reglas que mostrar. */
export const PolicyUnavailable: Story = {
  args: { decision: decisionOf(TRACES.policyUnavailable) },
  play: async ({ canvas }) => {
    await expect(
      canvas.getByRole("heading", { name: "No se pudo verificar la política de compra" }),
    ).toBeVisible()
    await expect(canvas.queryByRole("list", { name: "Reglas de la política de compra" })).toBeNull()
    await expect(canvas.getByText(NOTHING_HAPPENED)).toBeVisible()
  },
}

/** El detalle técnico lleva los códigos y la versión de la política. */
export const TechnicalDetail: Story = {
  play: async ({ args, canvas, userEvent }) => {
    await userEvent.click(canvas.getByText("Detalle técnico"))
    await expect(canvas.getByText(args.decision.evidence_id)).toBeVisible()
    await expect(canvas.getByText("POL-02")).toBeVisible()
    await expect(canvas.getByText("d1-demo-policy · revisión 3")).toBeVisible()
  },
}

export const Mobile: Story = {
  globals: { viewport: { value: "mobile", isRotated: false } },
}
