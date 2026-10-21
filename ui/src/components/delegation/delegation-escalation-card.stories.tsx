import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, fn } from "storybook/test"

import { DelegationEscalationCard } from "@/components/delegation/delegation-escalation-card"
import {
  escalationOf,
  expectFitsPhoneColumn,
  expectKeyboardFocus,
  PHONE_DIALOG_COLUMN,
  plain,
} from "@/components/delegation/story-helpers"
import { DELEGATIONS } from "@/storybook/fixtures/delegation-fixtures"

const ESCALATION = escalationOf(DELEGATIONS.escalated)

const meta = {
  title: "Delegation/EscalationCard",
  component: DelegationEscalationCard,
  tags: ["autodocs"],
  args: { escalation: ESCALATION, onBuyFromChat: fn() },
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof DelegationEscalationCard>

export default meta
type Story = StoryObj<typeof meta>

/**
 * D1 POL-11 escaló: «Necesito tu decisión» con el texto canónico del backend (motivo y sufijo) y
 * una sola salida, que prellena la compra HP en el chat.
 */
export const BudgetNotSatisfiable: Story = {
  play: async ({ args, canvas, canvasElement, userEvent }) => {
    await expect(canvas.getByRole("heading", { name: "Necesito tu decisión" })).toBeVisible()
    await expect(canvas.getByText(ESCALATION.message)).toBeVisible()
    await expect(
      canvasElement.querySelector("[data-testid=delegation-escalation-card]"),
    ).toHaveAttribute("data-reason", "budget_not_satisfiable")
    // El prellenado va al compositor, no a la tarjeta. Su monto lleva un espacio duro (K16).
    await expect(canvas.queryByText(plain(ESCALATION.prefill_request))).toBeNull()
    await expect(canvas.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Comprar desde el chat",
    ])
    await userEvent.click(canvas.getByRole("button", { name: "Comprar desde el chat" }))
    await expect(args.onBuyFromChat).toHaveBeenCalledWith(ESCALATION.prefill_request)
  },
}

/** Con el teclado el CTA muestra su anillo y prellena igual. */
export const KeyboardBuyFromChat: Story = {
  play: async ({ args, canvas, userEvent }) => {
    await userEvent.tab()
    await expectKeyboardFocus(canvas.getByRole("button", { name: "Comprar desde el chat" }))
    await userEvent.keyboard("{Enter}")
    await expect(args.onBuyFromChat).toHaveBeenCalledWith(ESCALATION.prefill_request)
  },
}

/** A 320 px el texto canónico completo y el CTA caben sin desbordar. */
export const Phone320: Story = {
  render: (args) => (
    <div className={PHONE_DIALOG_COLUMN} data-testid="phone-column">
      <DelegationEscalationCard {...args} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectFitsPhoneColumn(canvasElement)
  },
}
