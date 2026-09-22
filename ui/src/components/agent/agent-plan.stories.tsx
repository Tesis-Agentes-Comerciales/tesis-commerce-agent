import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { AgentPlan } from "@/components/agent/agent-plan"
import { derivePlanProgress } from "@/components/agent/agent-steps"
import type { AgentAssistantMessage, AgentToolCallPart } from "@/lib/agent-contract"
import {
  AGENT_STATE_AWAITING_APPROVAL,
  AGENT_STATE_CHECKOUT_ERROR,
} from "@/storybook/agent-fixtures"

const meta = {
  title: "Agent/AgentPlan",
  component: AgentPlan,
  tags: ["autodocs"],
  args: { current: "search" },
  argTypes: {
    current: {
      control: "select",
      options: [null, "understand", "search", "choose", "checkout", "approval", "complete", "done"],
    },
    failed: {
      control: "select",
      options: [null, "understand", "search", "choose", "checkout", "approval", "complete"],
    },
  },
  decorators: [
    (Story, { parameters }) => (
      <div
        className={
          parameters.wide ? "w-[min(46rem,calc(100vw-2rem))]" : "w-[min(24rem,calc(100vw-2rem))]"
        }
      >
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof AgentPlan>

export default meta
type Story = StoryObj<typeof meta>

export const NotStarted: Story = {
  args: { current: null },
}

export const Understanding: Story = {
  args: { current: "understand" },
}

export const Searching: Story = {
  play: async ({ canvas }) => {
    const active = canvas.getByText("Buscar").closest("li")
    await expect(active).toHaveAttribute("aria-current", "step")
    await expect(canvas.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "1")
  },
}

export const PreparingCheckout: Story = {
  args: { current: "checkout" },
}

/** La aprobación espera a la persona: sin spinner, con un punto que late. */
export const AwaitingApproval: Story = {
  args: { current: "approval", waiting: true },
}

export const Completing: Story = {
  args: { current: "complete" },
}

export const Done: Story = {
  args: { current: "done" },
}

export const FailedAtCheckout: Story = {
  args: { current: "checkout", failed: "checkout", failedNote: "El comercio rechazó el checkout" },
}

export const Canceled: Story = {
  args: { current: "approval", failed: "approval", failedNote: "Cancelaste la compra" },
}

/** Con espacio suficiente el plan se vuelve horizontal (consulta de contenedor). */
export const Wide: Story = {
  args: { current: "approval", waiting: true },
  parameters: { wide: true },
}

export const WideCanceled: Story = {
  args: { current: "approval", failed: "approval", failedNote: "Cancelaste la compra" },
  parameters: { wide: true },
}

function toolPartsOf(message: AgentAssistantMessage) {
  return message.content.filter((part): part is AgentToolCallPart => part.type === "tool-call")
}

const toolParts = toolPartsOf(AGENT_STATE_AWAITING_APPROVAL.messages[1] as AgentAssistantMessage)

/** Derivado del estado del contrato con `derivePlanProgress`. */
export const DerivedFromState: Story = {
  args: derivePlanProgress({
    parts: toolParts,
    purchase: AGENT_STATE_AWAITING_APPROVAL.purchase,
    running: false,
  }),
}

/** A mitad de turno: la búsqueda terminó y el mensaje sigue generándose. */
export const DerivedWhileRunning: Story = {
  args: derivePlanProgress({
    parts: toolParts.slice(0, 2),
    purchase: null,
    running: true,
  }),
}

/** `prepare_checkout` falló (`isError`): el plan se detiene en Checkout. */
export const DerivedFromError: Story = {
  args: {
    ...derivePlanProgress({
      parts: toolPartsOf(AGENT_STATE_CHECKOUT_ERROR.messages[1] as AgentAssistantMessage),
      purchase: AGENT_STATE_CHECKOUT_ERROR.purchase,
      running: false,
    }),
    failedNote: "No pude comunicarme con el ecommerce ACP.",
  },
}

export const Mobile: Story = {
  args: { current: "checkout" },
  globals: { viewport: { value: "mobile", isRotated: false } },
}
