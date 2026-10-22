import type { Meta, StoryObj } from "@storybook/react-vite"
import { useState } from "react"
import { expect, fn } from "storybook/test"

import { toolStepLabel, type StepStatus } from "@/components/agent/agent-steps"
import { ProductCarousel } from "@/components/agent/product-carousel"
import { usePhase, useStreamingText } from "@/components/agent/story-helpers"
import { ThinkingGroup } from "@/components/agent/thinking-group"
import { ThinkingIndicator } from "@/components/agent/thinking-indicator"
import {
  InterpretRequestStep,
  PrepareCheckoutStep,
  ReasoningStep,
  SearchCatalogStep,
  StepRow,
} from "@/components/agent/tool-steps"
import { useElapsedMs } from "@/components/agent/use-elapsed"
import type { AgentToolName } from "@/lib/agent-contract"
import {
  INTERPRET_PURCHASE,
  PREPARE_PRICE_CHANGED,
  PREPARE_RESULT,
  REASONING_LONG,
  REASONING_TEXT,
  SEARCH_RESULT,
  SELECT_RESULT,
  SONORA,
  TOOL_ERROR,
} from "@/storybook/agent-fixtures"

const meta = {
  title: "Agent/ThinkingGroup",
  component: ThinkingGroup,
  tags: ["autodocs"],
  args: {
    active: false,
    elapsedMs: 6400,
    stepCount: 4,
    onOpenChange: fn(),
    children: null,
  },
  argTypes: { children: { control: false } },
  decorators: [
    (Story) => (
      <div className="w-[min(40rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ThinkingGroup>

export default meta
type Story = StoryObj<typeof meta>

function CompletedSteps({ priceChanged = false }: { priceChanged?: boolean }) {
  return (
    <>
      <ReasoningStep text={REASONING_TEXT} elapsedMs={2100} />
      <InterpretRequestStep status="complete" result={INTERPRET_PURCHASE} elapsedMs={1200} />
      <SearchCatalogStep status="complete" result={SEARCH_RESULT} elapsedMs={800} />
      <PrepareCheckoutStep
        status="complete"
        result={priceChanged ? PREPARE_PRICE_CHANGED : PREPARE_RESULT}
        elapsedMs={2300}
      />
    </>
  )
}

/** Plegado al terminar: resume el tiempo y la cantidad de pasos. */
export const Completed: Story = {
  render: (args) => (
    <ThinkingGroup {...args}>
      <CompletedSteps />
    </ThinkingGroup>
  ),
  play: async ({ canvas, userEvent }) => {
    const trigger = canvas.getByRole("button", { name: /Pensó durante/ })
    await expect(trigger).toHaveAttribute("aria-expanded", "false")
    await userEvent.click(trigger)
    await expect(trigger).toHaveAttribute("aria-expanded", "true")
  },
}

export const CompletedOpen: Story = {
  args: { defaultOpen: true },
  render: (args) => (
    <ThinkingGroup {...args}>
      <CompletedSteps priceChanged />
    </ThinkingGroup>
  ),
}

function StreamingReasoning() {
  const { text, done } = useStreamingText(REASONING_TEXT, 3, 40)
  return <ReasoningStep text={text} streaming={!done} />
}

/**
 * Mientras corre, el grupo no se abre solo: el título brilla y cuenta los pasos, y abrirlo muestra
 * cada paso a medida que llega.
 */
export const Running: Story = {
  args: { active: true, elapsedMs: 3200, stepCount: 2 },
  render: (args) => (
    <ThinkingGroup {...args}>
      <InterpretRequestStep status="complete" result={INTERPRET_PURCHASE} elapsedMs={1100} />
      <SearchCatalogStep status="running" />
    </ThinkingGroup>
  ),
  play: async ({ canvas, userEvent }) => {
    const trigger = canvas.getByRole("button", { name: /Pensando…/ })
    await expect(trigger).toHaveAttribute("aria-expanded", "false")
    await expect(trigger).toHaveTextContent("2 pasos")
    await userEvent.click(trigger)
    await expect(trigger).toHaveAttribute("aria-expanded", "true")
  },
}

/** Antes del primer paso: el mismo encabezado, sin nada que abrir todavía. */
export const WaitingFirstStep: Story = {
  args: { active: true, stepCount: 0 },
  render: (args) => <ThinkingGroup {...args}>{null}</ThinkingGroup>,
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("button", { name: /Pensando…/ })).toBeDisabled()
  },
}

/** Razonamiento transmitiéndose con `append-text`, con el grupo abierto por la persona. */
export const Streaming: Story = {
  args: { active: true, defaultOpen: true, elapsedMs: 3200, stepCount: undefined },
  render: (args) => (
    <ThinkingGroup {...args}>
      <StreamingReasoning />
      <InterpretRequestStep status="running" />
    </ThinkingGroup>
  ),
}

export const Failed: Story = {
  args: { failed: true, defaultOpen: true, elapsedMs: 4100, stepCount: 2 },
  render: (args) => (
    <ThinkingGroup {...args}>
      <InterpretRequestStep status="complete" result={INTERPRET_PURCHASE} elapsedMs={1100} />
      <SearchCatalogStep status="error" error={TOOL_ERROR} elapsedMs={3000} />
    </ThinkingGroup>
  ),
}

/** Turno interrumpido: los pasos sin `result` quedan en error, pero no hubo fallo del agente. */
export const Stopped: Story = {
  args: { stopped: true, failed: true, defaultOpen: true, elapsedMs: 2600, stepCount: 2 },
  render: (args) => (
    <ThinkingGroup {...args}>
      <InterpretRequestStep status="complete" result={INTERPRET_PURCHASE} elapsedMs={1100} />
      <SearchCatalogStep status="error" error="El paso no se pudo completar." />
    </ThinkingGroup>
  ),
}

export const WithoutReasoning: Story = {
  args: { defaultOpen: true, elapsedMs: undefined, stepCount: 3 },
  render: (args) => (
    <ThinkingGroup {...args}>
      <InterpretRequestStep status="complete" result={INTERPRET_PURCHASE} />
      <SearchCatalogStep status="complete" result={SEARCH_RESULT} />
      <PrepareCheckoutStep status="complete" result={PREPARE_RESULT} />
    </ThinkingGroup>
  ),
}

export const LongReasoning: Story = {
  args: { defaultOpen: true, elapsedMs: 18400, stepCount: 1 },
  render: (args) => (
    <ThinkingGroup {...args}>
      <ReasoningStep text={REASONING_LONG} elapsedMs={18400} />
    </ThinkingGroup>
  ),
}

/** Los resultados de la búsqueda pueden ir dentro del paso como carrusel. */
export const WithSearchResults: Story = {
  args: { defaultOpen: true, stepCount: 2 },
  render: (args) => (
    <ThinkingGroup {...args}>
      <InterpretRequestStep status="complete" result={INTERPRET_PURCHASE} elapsedMs={1200} />
      <SearchCatalogStep status="complete" result={SEARCH_RESULT} elapsedMs={800}>
        <ProductCarousel
          products={SEARCH_RESULT.products}
          matches={SEARCH_RESULT.matches}
          selectedVariantId={SONORA.variant_id}
          className="mt-1"
        />
      </SearchCatalogStep>
    </ThinkingGroup>
  ),
}

const LIVE_TOOLS: AgentToolName[] = [
  "interpret_request",
  "search_catalog",
  "select_product",
  "prepare_checkout",
]

/** Turno completo simulado: cada fase termina un paso y abre el siguiente. */
function LiveRun() {
  const phase = usePhase(LIVE_TOOLS.length + 1, 1800)
  const { text, done: reasoningDone } = useStreamingText(REASONING_TEXT, 4, 35)
  const running = phase <= LIVE_TOOLS.length
  // Fijo por montaje: la simulación se reinicia al recargar la historia.
  const [startedAt] = useState(() => Date.now())
  const elapsedMs = useElapsedMs(startedAt, running)
  const [interpret, search, select, prepare] = LIVE_TOOLS.map((_, index): StepStatus | null =>
    phase < index + 1 ? null : phase === index + 1 ? "running" : "complete",
  )
  const currentTool = LIVE_TOOLS[phase - 1]
  const label = currentTool ? toolStepLabel(currentTool, "running") : "Pensando…"

  return (
    <div className="space-y-3">
      {running && <ThinkingIndicator label={label} elapsedMs={elapsedMs} />}
      <ThinkingGroup active={running} elapsedMs={elapsedMs} stepCount={LIVE_TOOLS.length + 1}>
        <ReasoningStep text={text} streaming={!reasoningDone} />
        {interpret && <InterpretRequestStep status={interpret} result={INTERPRET_PURCHASE} />}
        {search && <SearchCatalogStep status={search} result={SEARCH_RESULT} />}
        {select && (
          <StepRow
            status={select}
            title={toolStepLabel("select_product", select)}
            summary={select === "complete" ? SELECT_RESULT.product?.title : undefined}
          />
        )}
        {prepare && <PrepareCheckoutStep status={prepare} result={PREPARE_RESULT} />}
      </ThinkingGroup>
    </div>
  )
}

export const LiveRunSimulation: Story = {
  args: { active: true },
  render: () => <LiveRun />,
}

export const Mobile: Story = {
  args: { defaultOpen: true },
  globals: { viewport: { value: "mobile", isRotated: false } },
  render: (args) => (
    <ThinkingGroup {...args}>
      <CompletedSteps priceChanged />
    </ThinkingGroup>
  ),
}
