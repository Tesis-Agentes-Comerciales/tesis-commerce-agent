import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { ControlTimeline } from "@/components/trace/control-timeline"
import { TRACES } from "@/storybook/trace-fixtures"

/** Filas de primer nivel: las reglas de la política forman una lista anidada. */
function timelineRows(canvasElement: HTMLElement) {
  const timeline = canvasElement.querySelector("ol[aria-label='Controles de la compra']")
  return Array.from(timeline?.querySelectorAll(":scope > li") ?? [])
}

const meta = {
  title: "Trace/ControlTimeline",
  component: ControlTimeline,
  tags: ["autodocs"],
  args: { trace: TRACES.completedShadow },
  decorators: [
    (Story) => (
      <div className="w-[min(40rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ControlTimeline>

export default meta
type Story = StoryObj<typeof meta>

/** Modo observando: todos los controles, sin filas omitidas. */
export const Completed: Story = {
  play: async ({ canvas, canvasElement }) => {
    await expect(timelineRows(canvasElement)).toHaveLength(TRACES.completedShadow.steps.length)
    await expect(canvas.getByText("Orden confirmada")).toBeVisible()
    await expect(canvas.queryByText("No se ejecutó")).toBeNull()
  },
}

/** Modo activo y la política permitió seguir. */
export const CompletedEnforce: Story = {
  args: { trace: TRACES.completedEnforce },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Activa: detiene la compra si no cumple")).toBeVisible()
    await expect(canvas.getByText("Política de compra: permitida")).toBeVisible()
  },
}

/** Tras el bloqueo se listan los controles que no ocurrieron: ni checkout ni cobro. */
export const Blocked: Story = {
  args: { trace: TRACES.blockedMerchant },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("La política detuvo la compra")).toBeVisible()
    for (const control of ["Crear el checkout", "Autorizar el pago", "Confirmar la orden"]) {
      const row = canvas.getByText(control).closest("li")
      await expect(row).toHaveAttribute("data-state", "skipped")
    }
  },
}

export const BlockedProduct: Story = {
  args: { trace: TRACES.blockedProduct },
}

export const PolicyUnavailable: Story = {
  args: { trace: TRACES.policyUnavailable },
}

export const InProgress: Story = {
  args: { trace: TRACES.inProgress },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Autorizando el pago…")).toBeVisible()
    await expect(canvas.queryByText("Confirmar la orden")).toBeNull()
  },
}

export const AwaitingApproval: Story = {
  args: { trace: TRACES.awaitingApproval },
}

/** Sin política: el control figura como desactivado entre la elección y el checkout. */
export const PolicyOff: Story = {
  args: { trace: TRACES.policyOff },
  play: async ({ canvasElement }) => {
    const rows = timelineRows(canvasElement).map((row) => row.textContent ?? "")
    const policy = rows.findIndex((text) => text.includes("Política desactivada"))
    await expect(rows[policy - 1]).toContain("Producto elegido")
    await expect(rows[policy + 1]).toContain("Checkout creado")
  },
}

export const Canceled: Story = {
  args: { trace: TRACES.canceled },
}

export const Failed: Story = {
  args: { trace: TRACES.failed },
}

/** No se sabe si la orden se creó: no se agregan controles omitidos. */
export const Uncertain: Story = {
  args: { trace: TRACES.uncertain },
  play: async ({ canvas }) => {
    await expect(canvas.queryByText("No se ejecutó")).toBeNull()
  },
}
