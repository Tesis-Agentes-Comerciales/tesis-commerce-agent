import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, waitFor, within } from "storybook/test"

import { AgentPlan } from "@/components/agent/agent-plan"
import { derivePlanProgress } from "@/components/agent/agent-steps"
import type { AgentAssistantMessage, AgentToolCallPart } from "@/lib/agent-contract"
import {
  AGENT_STATE_AWAITING_APPROVAL,
  AGENT_STATE_CHECKOUT_ERROR,
} from "@/storybook/agent-fixtures"
import { runningAnimations } from "@/storybook/interactions"

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

type PlayContext = Parameters<NonNullable<Story["play"]>>[0]

function step({ canvas }: PlayContext, label: string) {
  return canvas.getByText(label).closest("li")!
}

/** La barra y el contador dicen cuántos de los seis pasos terminaron. */
async function expectProgress({ canvas }: PlayContext, done: number) {
  await expect(canvas.getByRole("progressbar")).toHaveAttribute("aria-valuenow", String(done))
  await expect(canvas.getByText(`${done} de 6`)).toBeVisible()
}

export const NotStarted: Story = {
  args: { current: null },
  play: async (context) => {
    await expectProgress(context, 0)
    await expect(context.canvasElement.querySelector("[aria-current]")).toBeNull()
  },
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
  play: async (context) => {
    await expect(step(context, "Aprobación")).toHaveAttribute("aria-current", "step")
    await expect(context.canvas.getByText("Esperando tu aprobación")).toBeVisible()
    await expectProgress(context, 4)
  },
}

export const Completing: Story = {
  args: { current: "complete" },
}

export const Done: Story = {
  args: { current: "done" },
  play: async (context) => {
    await expectProgress(context, 6)
    await expect(context.canvasElement.querySelector("[aria-current]")).toBeNull()
  },
}

/**
 * El paso detenido lleva su nota (en angosto, bajo el paso) y el estado para lectores de pantalla;
 * los siguientes quedan pendientes.
 */
export const FailedAtCheckout: Story = {
  args: { current: "checkout", failed: "checkout", failedNote: "El comercio rechazó el checkout" },
  play: async (context) => {
    await expect(step(context, "Checkout")).toHaveTextContent("(detenido)")
    await expect(within(step(context, "Checkout")).getByText("El comercio rechazó el checkout")).toBeVisible()
    await expect(step(context, "Aprobación")).toHaveTextContent("(pendiente)")
    await expectProgress(context, 3)
  },
}

export const Canceled: Story = {
  args: { current: "approval", failed: "approval", failedNote: "Cancelaste la compra" },
  play: async (context) => {
    await expect(step(context, "Aprobación")).toHaveTextContent("(detenido)")
    await expect(within(step(context, "Aprobación")).getByText("Cancelaste la compra")).toBeVisible()
  },
}

/**
 * La política de compra detuvo la compra en el checkout: el paso queda en ámbar, no en rojo, porque
 * no es un error. La tarjeta de bloqueo dice qué regla no se cumplió.
 */
export const StoppedByPolicy: Story = {
  args: { current: "checkout", stopped: "checkout" },
  play: async (context) => {
    const checkout = step(context, "Checkout")
    await expect(checkout).toHaveTextContent("(detenido por la política de compra)")
    await expect(within(checkout).getByText("La política la detuvo")).toBeVisible()
    await expect(step(context, "Aprobación")).toHaveTextContent("(pendiente)")
    await expect(context.canvasElement.querySelector(".text-red-700, .bg-red-600")).toBeNull()
    await expectProgress(context, 3)
  },
}

export const WideStoppedByPolicy: Story = {
  args: { current: "checkout", stopped: "checkout" },
  parameters: { wide: true },
}

/**
 * El turno terminó con una pregunta y el plan queda a la vista esperando a la persona: el punto no
 * late. Con la página quieta nada se anima.
 */
export const WaitingAfterTurn: Story = {
  args: { current: "search", waiting: true, still: true },
  play: async (context) => {
    await expect(step(context, "Buscar")).toHaveAttribute("aria-current", "step")
    await expect(context.canvas.getByText("Esperando tu respuesta")).toBeVisible()
    await expect(runningAnimations(context.canvasElement)).toHaveLength(0)
  },
}

/**
 * En el chat el plan avanza a ritmo (`paced`): aunque el progreso real ya vaya en Elegir, Entender y
 * Buscar salen en curso y luego hechos, un paso cada `PLAN_STEP_MIN_MS`.
 */
export const Paced: Story = {
  args: { current: "choose", paced: true },
  play: async (context) => {
    await expect(step(context, "Entender")).toHaveTextContent("(en curso)")
    await expect(step(context, "Buscar")).toHaveTextContent("(pendiente)")
    await waitFor(() => expect(step(context, "Buscar")).toHaveTextContent("(en curso)"), {
      timeout: 2000,
    })
    await expect(step(context, "Entender")).toHaveTextContent("(hecho)")
    await waitFor(() => expect(step(context, "Elegir")).toHaveTextContent("(en curso)"), {
      timeout: 2000,
    })
    await expectProgress(context, 2)
  },
}

/** A ritmo, la parada de la política se ve cuando el plan visible llega al checkout. */
export const PacedStoppedByPolicy: Story = {
  args: { current: "checkout", stopped: "checkout", paced: true },
  play: async (context) => {
    await expect(step(context, "Checkout")).toHaveTextContent("(pendiente)")
    await expect(context.canvas.queryByText("La política la detuvo")).toBeNull()
    await waitFor(
      () => expect(step(context, "Checkout")).toHaveTextContent("(detenido por la política de compra)"),
      { timeout: 4000 },
    )
    await expectProgress(context, 3)
  },
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

/** Derivado del estado del contrato con `derivePlanProgress`: la compra espera la aprobación. */
export const DerivedFromState: Story = {
  args: derivePlanProgress({
    parts: toolParts,
    purchase: AGENT_STATE_AWAITING_APPROVAL.purchase,
    running: false,
  }),
  play: async (context) => {
    await expect(step(context, "Aprobación")).toHaveAttribute("aria-current", "step")
    await expect(context.canvas.getByText("Esperando tu aprobación")).toBeVisible()
  },
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
  play: async (context) => {
    await expect(step(context, "Checkout")).toHaveTextContent("(detenido)")
    await expect(
      within(step(context, "Checkout")).getByText("No pude comunicarme con el ecommerce ACP."),
    ).toBeVisible()
  },
}

export const Mobile: Story = {
  args: { current: "checkout" },
  globals: { viewport: { value: "mobile", isRotated: false } },
}
