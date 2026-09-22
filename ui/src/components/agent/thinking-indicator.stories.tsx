import type { Meta, StoryObj } from "@storybook/react-vite"
import { useState } from "react"
import { expect } from "storybook/test"

import { toolStepLabel } from "@/components/agent/agent-steps"
import { usePhase } from "@/components/agent/story-helpers"
import { ThinkingIndicator } from "@/components/agent/thinking-indicator"
import { useElapsedMs } from "@/components/agent/use-elapsed"
import { AGENT_TOOL_NAMES } from "@/lib/agent-contract"

const meta = {
  title: "Agent/ThinkingIndicator",
  component: ThinkingIndicator,
  tags: ["autodocs"],
  args: { label: "Pensando…" },
  decorators: [
    (Story) => (
      <div className="w-[min(32rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ThinkingIndicator>

export default meta
type Story = StoryObj<typeof meta>

export const Thinking: Story = {
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("status")).toHaveTextContent("Pensando…")
  },
}

export const NamedStep: Story = {
  args: { label: toolStepLabel("search_catalog", "running"), elapsedMs: 3400 },
}

export const LongElapsed: Story = {
  args: { label: toolStepLabel("prepare_checkout", "running"), elapsedMs: 83000 },
}

/** La etiqueta cambia con cada paso y entra con un fundido; el tiempo avanza solo. */
function CyclingIndicator() {
  const phase = usePhase(AGENT_TOOL_NAMES.length - 1, 1600, true)
  const [startedAt] = useState(() => Date.now())
  const elapsedMs = useElapsedMs(startedAt, true)
  const tool = AGENT_TOOL_NAMES[phase] ?? "interpret_request"
  return <ThinkingIndicator label={toolStepLabel(tool, "running")} elapsedMs={elapsedMs} />
}

export const CyclingSteps: Story = {
  render: () => <CyclingIndicator />,
}

export const Mobile: Story = {
  args: { label: toolStepLabel("request_approval", "running"), elapsedMs: 12000 },
  globals: { viewport: { value: "mobile", isRotated: false } },
}
