import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, fn } from "storybook/test"

import { DelegationSearchFirst } from "@/components/delegation/delegation-search-first"
import {
  expectFitsPhoneColumn,
  expectKeyboardFocus,
  PHONE_DIALOG_COLUMN,
} from "@/components/delegation/story-helpers"

const meta = {
  title: "Delegation/SearchFirst",
  component: DelegationSearchFirst,
  args: { onGoToChat: fn() },
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof DelegationSearchFirst>

export default meta
type Story = StoryObj<typeof meta>

/** «Delegar» sin una búsqueda previa: qué hacer primero y el camino de vuelta al chat. */
export const Default: Story = {
  play: async ({ args, canvas, userEvent }) => {
    await expect(canvas.getByRole("heading", { name: "Primero busca en el chat" })).toBeVisible()
    await expect(canvas.getByText(/Las opciones que te muestre aparecen aquí/)).toBeVisible()
    await userEvent.tab()
    const toChat = canvas.getByRole("button", { name: "Buscar en el chat" })
    await expectKeyboardFocus(toChat)
    await userEvent.keyboard("{Enter}")
    await expect(args.onGoToChat).toHaveBeenCalledOnce()
  },
}

export const Phone320: Story = {
  render: (args) => (
    <div className={PHONE_DIALOG_COLUMN} data-testid="phone-column">
      <DelegationSearchFirst {...args} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectFitsPhoneColumn(canvasElement)
  },
}
