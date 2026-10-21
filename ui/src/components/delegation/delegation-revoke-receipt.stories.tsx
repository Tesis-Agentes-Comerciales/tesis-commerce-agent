import type { Meta, StoryContext, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { formatDateTime } from "@/components/agent/agent-format"
import {
  DelegationRevokeReceipt,
  type DelegationRevokeReceiptProps,
} from "@/components/delegation/delegation-revoke-receipt"
import { expectFitsPhoneColumn, PHONE_COLUMN, plain } from "@/components/delegation/story-helpers"
import { REVOKE_RECEIPTS } from "@/storybook/fixtures/delegation-fixtures"

const [NO_EFFECT, RELEASED, IN_FLIGHT] = REVOKE_RECEIPTS.receipts

const meta = {
  title: "Delegation/RevokeReceipt",
  component: DelegationRevokeReceipt,
  tags: ["autodocs"],
  args: { receipt: NO_EFFECT },
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof DelegationRevokeReceipt>

export default meta
type Story = StoryObj<typeof meta>

/** El mensaje del backend para ese efecto, tal cual, y la hora de la revocación. */
async function expectReceipt({ args, canvas }: StoryContext<DelegationRevokeReceiptProps>) {
  const receipt = canvas.getByTestId("delegation-revoke-receipt")
  await expect(receipt).toHaveAttribute("data-effect", args.receipt.effect)
  await expect(canvas.getByText(args.receipt.message)).toBeVisible()
  const revokedAt = formatDateTime(args.receipt.revoked_at) ?? ""
  await expect(canvas.getByText(plain(revokedAt))).toHaveAttribute(
    "datetime",
    args.receipt.revoked_at,
  )
}

/** Sin reserva: la delegación se cierra y no había nada en curso. */
export const NoEffect: Story = { play: expectReceipt }

/** Había una reserva sin compromiso: se liberó sin presentar nada. */
export const Released: Story = { args: { receipt: RELEASED }, play: expectReceipt }

/** El pago ya estaba comprometido: la compra sigue y no se promete cancelarla. */
export const InFlight: Story = { args: { receipt: IN_FLIGHT }, play: expectReceipt }

/** «Revocar todas» (`RevokeAllReceipt`): un recibo por delegación que estaba activa. */
export const RevokeAll: Story = {
  render: () => (
    <ul className="space-y-2">
      {REVOKE_RECEIPTS.receipts.map((receipt) => (
        <li key={receipt.grant_id}>
          <DelegationRevokeReceipt receipt={receipt} />
        </li>
      ))}
    </ul>
  ),
  play: async ({ canvas }) => {
    const receipts = canvas.getAllByTestId("delegation-revoke-receipt")
    await expect(receipts.map((receipt) => receipt.dataset.effect)).toEqual([
      "no_effect",
      "released",
      "in_flight",
    ])
  },
}

/** A 320 px el mensaje más largo (`in_flight`) cabe sin desbordar. */
export const Phone320: Story = {
  args: { receipt: IN_FLIGHT },
  render: (args) => (
    <div className={PHONE_COLUMN} data-testid="phone-column">
      <DelegationRevokeReceipt {...args} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectFitsPhoneColumn(canvasElement)
  },
}
