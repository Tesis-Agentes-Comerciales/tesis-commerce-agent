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

/** Sin política y recién recibido el pedido: la fila desactivada espera a que haya elección. */
export const PolicyOffBeforeSelection: Story = {
  args: {
    trace: {
      ...TRACES.policyOff,
      status: "in_progress",
      total: null,
      steps: TRACES.policyOff.steps.filter((step) => step.kind === "request"),
    },
  },
  play: async ({ canvas, canvasElement }) => {
    await expect(timelineRows(canvasElement)).toHaveLength(1)
    await expect(canvas.queryByText("Política desactivada")).toBeNull()
  },
}

export const Canceled: Story = {
  args: { trace: TRACES.canceled },
}

export const Failed: Story = {
  args: { trace: TRACES.failed },
}

/**
 * S04: la política permitió la compra; la frescura confirmó los términos al mostrarlos y detuvo la
 * aprobación cuando el precio cambió. Después: ni firma, ni pago, ni orden.
 */
export const StaleTerms: Story = {
  args: { trace: TRACES.staleTerms },
  play: async ({ canvas, canvasElement }) => {
    const rows = timelineRows(canvasElement)
    const titles = rows.map((row) => row.querySelector("p")?.textContent)
    await expect(titles).toEqual([
      "Pedido recibido",
      "Producto elegido",
      "Política de compra: permitida",
      "Checkout creado en el comercio",
      "Condiciones confirmadas con el comercio",
      "Tu aprobación",
      "Las condiciones del comercio cambiaron",
      "Firmar la autorización",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
    await expect(rows.map((row) => row.getAttribute("data-state"))).toEqual([
      "done",
      "done",
      "done",
      "done",
      "done",
      "skipped",
      "blocked",
      "skipped",
      "skipped",
      "skipped",
    ])
    await expect(canvas.getByRole("list", { name: "Qué cambió" })).toBeVisible()
    // El único bloqueo es de un control de la aplicación: nada lo atribuye a la política.
    await expect(canvas.queryByText("La política detuvo la compra")).toBeNull()
  },
}

/**
 * S04 en el chat de la demo: la aprobación nunca se envió. Al pulsar «Aprobar», releer los términos
 * los rechazó; la fila de frescura bloqueada es de la etapa `present`, después de la confirmada.
 */
export const StaleTermsOnReread: Story = {
  args: { trace: TRACES.staleTermsReread },
  play: async ({ canvas, canvasElement }) => {
    const rows = timelineRows(canvasElement)
    const titles = rows.map((row) => row.querySelector("p")?.textContent)
    await expect(titles.slice(4)).toEqual([
      "Condiciones confirmadas con el comercio",
      "Tu aprobación",
      "Las condiciones del comercio cambiaron",
      "Firmar la autorización",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
    await expect(rows[4]).toHaveTextContent("Antes de mostrarte los términos")
    await expect(rows[5]).toHaveTextContent("No se ejecutó")
    await expect(rows[6]).toHaveAttribute("data-state", "blocked")
    await expect(rows[6]).toHaveTextContent(
      "No se registró ninguna aprobación ni se firmó ninguna autorización.",
    )
    await expect(canvas.queryByText(/No se mostraron/)).toBeNull()
    await expect(canvas.queryByText("La política detuvo la compra")).toBeNull()
  },
}

/** S02: el checkout se canceló por presupuesto y el agente explicó por qué, sin error técnico. */
export const GovernedRecovery: Story = {
  args: { trace: TRACES.governedRecovery },
  play: async ({ canvas, canvasElement }) => {
    const titles = timelineRows(canvasElement).map((row) => row.querySelector("p")?.textContent)
    const recovery = titles.indexOf("El agente explicó por qué no siguió")
    await expect(titles[recovery - 1]).toBe("Compra cancelada: superaba tu presupuesto")
    await expect(titles.slice(recovery + 1)).toEqual([
      "Tu aprobación",
      "Firmar la autorización",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
    await expect(canvas.queryByText(/error/i)).toBeNull()
  },
}

/** No se sabe si la orden se creó: no se agregan controles omitidos. */
export const Uncertain: Story = {
  args: { trace: TRACES.uncertain },
  play: async ({ canvas }) => {
    await expect(canvas.queryByText("No se ejecutó")).toBeNull()
  },
}
