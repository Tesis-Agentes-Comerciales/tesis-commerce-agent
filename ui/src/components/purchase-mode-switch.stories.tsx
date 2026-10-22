import type { Meta, StoryObj } from "@storybook/react-vite"
import { useState } from "react"
import { expect, fn } from "storybook/test"

import {
  PurchaseModeSwitch,
  type PurchaseMode,
  type PurchaseModeSwitchProps,
} from "@/components/purchase-mode-switch"

const meta = {
  title: "App/PurchaseModeSwitch",
  component: PurchaseModeSwitch,
  args: { mode: "present", onModeChange: fn() },
} satisfies Meta<typeof PurchaseModeSwitch>

export default meta
type Story = StoryObj<typeof meta>

/** Lo que hace la app: el modo elegido es el estado; el selector solo avisa el cambio. */
function Stateful(props: PurchaseModeSwitchProps) {
  const [mode, setMode] = useState<PurchaseMode>(props.mode)
  return (
    <PurchaseModeSwitch
      {...props}
      mode={mode}
      onModeChange={(next) => {
        props.onModeChange?.(next)
        setMode(next)
      }}
    />
  )
}

/** Comprar con la persona en el chat es el modo de partida; un grupo de radios con nombre. */
export const Present: Story = {
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("group", { name: "Modo de compra" })).toBeVisible()
    await expect(canvas.getByRole("radio", { name: "Comprar conmigo" })).toBeChecked()
    await expect(canvas.getByRole("radio", { name: "Delegar" })).not.toBeChecked()
  },
}

export const Delegated: Story = {
  args: { mode: "delegated" },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("radio", { name: "Delegar" })).toBeChecked()
  },
}

/** Con el mouse: el clic en la etiqueta elige el modo. */
export const ChooseWithMouse: Story = {
  render: (args) => <Stateful {...args} />,
  play: async ({ args, canvas, userEvent }) => {
    await userEvent.click(canvas.getByText("Delegar"))
    await expect(args.onModeChange).toHaveBeenCalledWith("delegated")
    await expect(canvas.getByRole("radio", { name: "Delegar" })).toBeChecked()
  },
}

/**
 * Con el teclado: Tab entra al modo elegido, con el anillo de la tinta del chat en su segmento, y
 * las flechas cambian de modo en los dos sentidos.
 */
export const ChooseWithKeyboard: Story = {
  render: (args) => <Stateful {...args} />,
  play: async ({ args, canvas, userEvent }) => {
    await userEvent.tab()
    const present = canvas.getByRole("radio", { name: "Comprar conmigo" })
    await expect(present).toHaveFocus()
    await expect(getComputedStyle(present.parentElement!).boxShadow).toContain("rgb(13, 13, 13)")
    await userEvent.keyboard("{ArrowRight}")
    const delegated = canvas.getByRole("radio", { name: "Delegar" })
    await expect(delegated).toHaveFocus()
    await expect(delegated).toBeChecked()
    await userEvent.keyboard("{ArrowLeft}")
    await expect(present).toBeChecked()
    await expect(args.onModeChange).toHaveBeenNthCalledWith(1, "delegated")
    await expect(args.onModeChange).toHaveBeenNthCalledWith(2, "present")
  },
}
