import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { ControlStep } from "@/components/trace/control-step"
import { stepOf, TRACES } from "@/storybook/trace-fixtures"

const COMPLETED = TRACES.completedShadow

const meta = {
  title: "Trace/ControlStep",
  component: ControlStep,
  tags: ["autodocs"],
  args: { step: stepOf(COMPLETED, "request") },
  decorators: [
    (Story) => (
      <ol className="w-[min(40rem,calc(100vw-2rem))]">
        <Story />
      </ol>
    ),
  ],
} satisfies Meta<typeof ControlStep>

export default meta
type Story = StoryObj<typeof meta>

// Una historia por `kind`.

export const Request: Story = {}

export const Selection: Story = {
  args: { step: stepOf(COMPLETED, "selection") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Producto elegido")).toBeVisible()
    await expect(canvas.getByText(/Sony WH-1000XM6 negro · \$/)).toBeVisible()
  },
}

/** Modo observando: permitida, con sus dos reglas y sin códigos internos. */
export const PolicyDecision: Story = {
  args: { step: stepOf(COMPLETED, "policy_decision") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Política de compra: permitida")).toBeVisible()
    await expect(canvas.getByText("Solo observando: registra, no detiene")).toBeVisible()
    await expect(canvas.getByRole("list", { name: "Reglas de la política de compra" })).toBeVisible()
    await expect(canvas.getByText("4 ms")).toBeVisible()
    await expect(canvas.queryByText(/POL-0|shadow|D1/)).toBeNull()
  },
}

export const CheckoutCreated: Story = {
  args: { step: stepOf(COMPLETED, "checkout_created") },
}

export const Approval: Story = {
  args: { step: stepOf(COMPLETED, "approval") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Aprobaste la compra")).toBeVisible()
    await expect(canvas.getByText(/^A las \d{2}:\d{2}$/)).toBeVisible()
  },
}

export const AuthorizationSigned: Story = {
  args: { step: stepOf(COMPLETED, "authorization_signed") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Para la compra y el pago")).toBeVisible()
  },
}

export const PaymentAuthorized: Story = {
  args: { step: stepOf(COMPLETED, "payment_authorized") },
}

export const OrderConfirmed: Story = {
  args: { step: stepOf(COMPLETED, "order_confirmed") },
}

export const OrderEvent: Story = {
  args: { step: stepOf(COMPLETED, "order_event") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("El comercio registró la orden")).toBeVisible()
  },
}

export const Canceled: Story = {
  args: { step: stepOf(TRACES.canceled, "canceled") },
}

/** El código (`payment_declined`) se traduce; el texto crudo nunca aparece. */
export const Failed: Story = {
  args: { step: stepOf(TRACES.failed, "failed") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("El comercio rechazó el pago.")).toBeVisible()
    await expect(canvas.queryByText(/payment_declined/)).toBeNull()
  },
}

// Una historia por `state`.

export const StateDone: Story = {
  args: { step: stepOf(COMPLETED, "payment_authorized") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Hecho")).toHaveClass("sr-only")
  },
}

export const StateRunning: Story = {
  args: { step: stepOf(TRACES.inProgress, "payment_authorized") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Autorizando el pago…")).toBeVisible()
  },
}

export const StateWaiting: Story = {
  args: { step: stepOf(TRACES.awaitingApproval, "approval") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Esperando tu aprobación")).toBeVisible()
    await expect(canvas.getByText(/^Vence a las \d{2}:\d{2}$/)).toBeVisible()
  },
}

/** Bloqueo por política: escudo ámbar y título en tinta, no en rojo. */
export const StateBlocked: Story = {
  args: { step: stepOf(TRACES.blockedMerchant, "policy_decision") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("La política detuvo la compra")).not.toHaveClass("text-red-700")
    await expect(canvas.getByText("Activa: detiene la compra si no cumple")).toBeVisible()
    await expect(canvas.getByText("No cumple")).toBeVisible()
  },
}

export const StateBlockedPolicyUnavailable: Story = {
  args: { step: stepOf(TRACES.policyUnavailable, "policy_decision") },
}

/** Error técnico: título en rojo. */
export const StateError: Story = {
  args: { step: stepOf(TRACES.failed, "failed") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("La compra no se pudo completar")).toHaveClass("text-red-700")
  },
}

/** La persona canceló antes de aprobar. */
export const StateSkipped: Story = {
  args: { step: stepOf(TRACES.canceled, "approval") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Tu aprobación")).toBeVisible()
    await expect(canvas.getAllByText("No se ejecutó")).toHaveLength(2)
  },
}

/** Control que la traza no trae porque la compra no lo alcanzó. */
export const NotReached: Story = {
  args: { step: { kind: "checkout_created", reason: "not_reached" } },
}

export const PolicyOff: Story = {
  args: { step: { kind: "policy_decision", reason: "policy_off" } },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Política desactivada")).toBeVisible()
  },
}
