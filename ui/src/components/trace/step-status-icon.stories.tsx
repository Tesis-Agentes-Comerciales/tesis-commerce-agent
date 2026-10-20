import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { StepStatusIcon } from "@/components/trace/step-status-icon"
import { STEP_STATE_LABEL } from "@/components/trace/trace-copy"
import type { StepState } from "@/lib/trace-contract"

const meta = {
  title: "Trace/StepStatusIcon",
  component: StepStatusIcon,
  tags: ["autodocs"],
  args: { state: "done" },
} satisfies Meta<typeof StepStatusIcon>

export default meta
type Story = StoryObj<typeof meta>

function stateStory(state: StepState): Story {
  return {
    args: { state },
    play: async ({ canvas }) => {
      await expect(canvas.getByText(STEP_STATE_LABEL[state])).toBeVisible()
    },
  }
}

export const Done = stateStory("done")
export const Running = stateStory("running")
export const Waiting = stateStory("waiting")
/** La política detuvo la compra a propósito: escudo en ámbar. */
export const Blocked = stateStory("blocked")
/** Algo falló: equis en rojo, distinta del bloqueo. */
export const ErrorState = stateStory("error")
export const Skipped = stateStory("skipped")

/** Con `hideLabel` el texto sigue para lectores de pantalla. */
export const LabelHidden: Story = {
  args: { state: "blocked", hideLabel: true },
  play: async ({ canvas }) => {
    await expect(canvas.getByText(STEP_STATE_LABEL.blocked)).toHaveClass("sr-only")
  },
}

const STATES = Object.keys(STEP_STATE_LABEL) as StepState[]

export const AllStates: Story = {
  render: () => (
    <ul className="space-y-2">
      {STATES.map((state) => (
        <li key={state}>
          <StepStatusIcon state={state} />
        </li>
      ))}
    </ul>
  ),
}
