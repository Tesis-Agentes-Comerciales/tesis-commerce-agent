import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { PurchaseStatusLabel } from "@/components/trace/purchase-status-label"
import { PURCHASE_STATUS_LABEL } from "@/components/trace/trace-copy"
import type { PurchaseStatus } from "@/lib/trace-contract"

const meta = {
  title: "Trace/PurchaseStatusLabel",
  component: PurchaseStatusLabel,
  tags: ["autodocs"],
  args: { status: "completed" },
} satisfies Meta<typeof PurchaseStatusLabel>

export default meta
type Story = StoryObj<typeof meta>

function statusStory(status: PurchaseStatus): Story {
  return {
    args: { status },
    play: async ({ canvas }) => {
      await expect(canvas.getByText(PURCHASE_STATUS_LABEL[status])).toBeVisible()
    },
  }
}

export const InProgress = statusStory("in_progress")
/** Esperar a una persona no es un error: borde neutro, sin tono. */
export const AwaitingApproval = statusStory("awaiting_approval")
export const Completed = statusStory("completed")
export const Blocked = statusStory("blocked")
/** La frescura de la oferta detuvo la compra: control de la aplicación, no de la política. */
export const StaleTerms = statusStory("stale_terms")
export const Canceled = statusStory("canceled")
export const Failed = statusStory("failed")
export const Uncertain = statusStory("uncertain")

const STATUSES = Object.keys(PURCHASE_STATUS_LABEL) as PurchaseStatus[]

export const AllStatuses: Story = {
  render: () => (
    <div className="flex max-w-md flex-wrap gap-2">
      {STATUSES.map((status) => (
        <PurchaseStatusLabel key={status} status={status} />
      ))}
    </div>
  ),
}
