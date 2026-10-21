import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { PolicyBlockedCard } from "@/components/agent/policy-blocked-card"
import { traceMerchant } from "@/components/trace/trace-copy"
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

/** Modo activo con una lista de comercios que excluye la tienda, nombrada como en el chat. */
export const MerchantNotAllowed: Story = {
  args: { merchant: traceMerchant(TRACES.blockedMerchant) },
  play: async ({ canvas }) => {
    await expect(
      canvas.getByRole("heading", { name: "La política de compra detuvo esta compra" }),
    ).toBeVisible()
    await expect(canvas.getByText("Comercio permitido")).toBeVisible()
    await expect(canvas.getByText("Synthetic Commerce Merchant")).toBeVisible()
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

/**
 * S02 con `b2-hp-v2`: el precio del catálogo ya supera el presupuesto total, así que la política se
 * detiene antes del checkout. Muestra el precio y el presupuesto, como la regla de comercios.
 */
export const BudgetNotSatisfiable: Story = {
  args: {
    decision: decisionOf(TRACES.blockedBudget),
    merchant: traceMerchant(TRACES.blockedBudget),
  },
  play: async ({ canvas, userEvent }) => {
    await expect(
      canvas.getByRole("heading", { name: "La política de compra detuvo esta compra" }),
    ).toBeVisible()
    await expect(canvas.getByText("No se cumplió una regla.")).toBeVisible()
    await expect(canvas.getByText("Precio dentro de tu presupuesto")).toBeVisible()
    await expect(canvas.getByText("Precio en el catálogo")).toBeVisible()
    await expect(canvas.getByText(/^\$\s?1\.540\.000(,00)? COP$/)).toBeVisible()
    await expect(canvas.getByText("Tu presupuesto total")).toBeVisible()
    await expect(canvas.getByText(/^\$\s?1\.000\.000(,00)? COP$/)).toBeVisible()
    await expect(canvas.getByText(NOTHING_HAPPENED)).toBeVisible()
    // Solo la regla que falló: las que cumplen no se listan en la tarjeta.
    await expect(canvas.queryByText("Comercio permitido")).toBeNull()
    await expect(canvas.getByText("BUDGET_NOT_SATISFIABLE")).not.toBeVisible()
    await userEvent.click(canvas.getByText("Detalle técnico"))
    await expect(canvas.getByText("POL-11")).toBeVisible()
    await expect(canvas.getByText("b2-hp-v2 · revisión 1")).toBeVisible()
  },
}

export const BudgetNotSatisfiableMobile: Story = {
  args: BudgetNotSatisfiable.args,
  globals: { viewport: { value: "mobile", isRotated: false } },
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

const MERCHANT_DECISION = decisionOf(TRACES.blockedMerchant)

/** Deniega sin ninguna regla fallida: una se cumplió y la otra no llegó a evaluarse. */
export const DeniedWithoutFailedRule: Story = {
  args: {
    decision: {
      ...MERCHANT_DECISION,
      rules: [
        MERCHANT_DECISION.rules[0],
        { id: "POL-02", result: "not_evaluated", observed: null, expected: null },
      ],
      reason_codes: ["GOV_EVIDENCE_UNAVAILABLE"],
    },
  },
  play: async ({ canvas }) => {
    await expect(
      canvas.getByRole("heading", { name: "No se pudo verificar la política de compra" }),
    ).toBeVisible()
    await expect(canvas.getByText(/La revisión de la política no se completó\./)).toBeVisible()
    await expect(canvas.queryByText(/0 reglas|no estaba disponible/)).toBeNull()
    await expect(canvas.getByText(NOTHING_HAPPENED)).toBeVisible()
  },
}

/** Una decisión sin reglas no prueba que la política faltara: la revisión no se completó. */
export const DeniedWithoutRules: Story = {
  args: { decision: { ...MERCHANT_DECISION, rules: [], reason_codes: [] } },
  play: async ({ canvas }) => {
    await expect(canvas.getByText(/La revisión de la política no se completó\./)).toBeVisible()
    await expect(canvas.queryByText(/no estaba disponible/)).toBeNull()
  },
}

/**
 * La política permitió, pero el allow no se pudo dejar listo (falló al guardarlo) y quedó bloqueado.
 * La tarjeta cuenta lo mismo que el chat (`d1_allow_unapplied`): permitió y se detuvo por precaución.
 */
export const AllowNotApplied: Story = {
  args: {
    decision: {
      ...MERCHANT_DECISION,
      outcome: "allow",
      rules: MERCHANT_DECISION.rules.map((rule) => ({ ...rule, result: "pass" as const })),
      reason_codes: [],
    },
  },
  play: async ({ canvas }) => {
    await expect(
      canvas.getByRole("heading", { name: "La política de compra permitió esta compra" }),
    ).toBeVisible()
    await expect(canvas.getByText("Por precaución, la compra se detuvo.")).toBeVisible()
    await expect(canvas.getByText(/No se pudo dejar lista para crear el checkout\./)).toBeVisible()
    await expect(canvas.getByText(NOTHING_HAPPENED)).toBeVisible()
    await expect(canvas.queryByText(/No se pudo verificar la política/)).toBeNull()
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
