import type { Meta, StoryObj } from "@storybook/react-vite"

import { ReasoningText } from "@/components/agent/reasoning-text"
import { useStreamingText } from "@/components/agent/story-helpers"
import { REASONING_LONG, REASONING_TEXT } from "@/storybook/agent-fixtures"

const meta = {
  title: "Agent/ReasoningText",
  component: ReasoningText,
  tags: ["autodocs"],
  args: { text: REASONING_TEXT, streaming: false },
  decorators: [
    (Story) => (
      <div className="w-[min(36rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ReasoningText>

export default meta
type Story = StoryObj<typeof meta>

export const Complete: Story = {}

function StreamingDemo({ text }: { text: string }) {
  const streamed = useStreamingText(text, 4, 40)
  return <ReasoningText text={streamed.text} streaming={!streamed.done} />
}

/** Crece como con `append-text` y se mantiene fijo al final mientras llega texto. */
export const Streaming: Story = {
  args: { text: REASONING_LONG },
  render: (args) => <StreamingDemo key={args.text} text={args.text} />,
}

export const StreamingEmpty: Story = {
  args: { text: "", streaming: true },
}

export const LongScrollable: Story = {
  args: { text: REASONING_LONG },
}

export const PlainText: Story = {
  args: {
    text: "Resumen sin formato: comparo precio de catálogo, disponibilidad y variante antes de elegir.",
  },
}

export const Mobile: Story = {
  args: { text: REASONING_LONG },
  globals: { viewport: { value: "mobile", isRotated: false } },
}
