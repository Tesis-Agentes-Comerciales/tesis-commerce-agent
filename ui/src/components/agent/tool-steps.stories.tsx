import type { Meta, StoryObj } from "@storybook/react-vite"
import type { ReactNode } from "react"

import {
  InterpretRequestStep,
  PrepareCheckoutStep,
  PurchaseUpdateStep,
  ReasoningStep,
  SearchCatalogStep,
  StepRow,
} from "@/components/agent/tool-steps"
import {
  INTERPRET_CLARIFY,
  INTERPRET_PURCHASE,
  INTERPRET_RESPOND,
  PREPARE_NOT_READY,
  PREPARE_PRICE_CHANGED,
  PREPARE_RESULT,
  PURCHASE_CANCELED,
  PURCHASE_COMPLETED,
  PURCHASE_FAILED,
  REASONING_TEXT,
  SEARCH_RESULT,
  SEARCH_RESULT_EMPTY,
  SEARCH_RESULT_MANY,
  TOOL_ERROR,
} from "@/storybook/agent-fixtures"

/** Las filas son `<li>`; fuera de `ThinkingGroup` necesitan su propia lista. */
function Timeline({ children }: { children: ReactNode }) {
  return <ol className="w-[min(36rem,calc(100vw-2rem))]">{children}</ol>
}

const meta = {
  title: "Agent/ToolSteps",
  component: StepRow,
  tags: ["autodocs"],
  args: {
    status: "complete",
    title: "Paso genérico",
    summary: "Resumen breve del resultado",
    elapsedMs: 1250,
  },
  decorators: [
    (Story) => (
      <Timeline>
        <Story />
      </Timeline>
    ),
  ],
} satisfies Meta<typeof StepRow>

export default meta
type Story = StoryObj<typeof meta>

export const GenericStates: Story = {
  render: (args) => (
    <>
      <StepRow {...args} status="running" title="Paso en curso…" summary={undefined} />
      <StepRow {...args} />
      <StepRow {...args} status="error" title="Paso con error" summary={undefined} error={TOOL_ERROR} />
    </>
  ),
}

export const InterpretRequest: Story = {
  render: () => (
    <>
      <InterpretRequestStep status="running" />
      <InterpretRequestStep status="complete" result={INTERPRET_PURCHASE} elapsedMs={1180} />
      <InterpretRequestStep status="complete" result={INTERPRET_CLARIFY} elapsedMs={640} />
      <InterpretRequestStep status="complete" result={INTERPRET_RESPOND} elapsedMs={410} />
      <InterpretRequestStep
        status="error"
        error="No pude interpretar la solicitud con el modelo en este momento."
      />
    </>
  ),
}

/** Solo presupuesto total y requisitos de texto, sin preferencias. */
export const InterpretBudgetOnly: Story = {
  render: () => (
    <InterpretRequestStep
      status="complete"
      result={{
        action: "purchase",
        constraints: {
          currency: "COP",
          max_unit_amount_minor: null,
          max_total_amount_minor: 150000,
          categories: [],
          conditions: [],
          required_options: [],
          required_terms: ["bluetooth 5.3"],
        },
        preferences: null,
      }}
    />
  ),
}

export const SearchCatalog: Story = {
  render: () => (
    <>
      <SearchCatalogStep status="running" />
      <SearchCatalogStep status="complete" result={SEARCH_RESULT} elapsedMs={820} />
      <SearchCatalogStep status="complete" result={SEARCH_RESULT_MANY} elapsedMs={950} />
      <SearchCatalogStep status="complete" result={SEARCH_RESULT_EMPTY} elapsedMs={700} />
      <SearchCatalogStep status="error" error={TOOL_ERROR} elapsedMs={30000} />
    </>
  ),
}

export const PrepareCheckout: Story = {
  render: () => (
    <>
      <PrepareCheckoutStep status="running" />
      <PrepareCheckoutStep status="complete" result={PREPARE_RESULT} elapsedMs={2310} />
      <PrepareCheckoutStep status="complete" result={PREPARE_PRICE_CHANGED} elapsedMs={2480} />
      <PrepareCheckoutStep status="complete" result={PREPARE_NOT_READY} elapsedMs={1900} />
      <PrepareCheckoutStep
        status="error"
        error="El comercio rechazó la dirección de entrega."
        elapsedMs={1500}
      />
    </>
  ),
}

export const PurchaseUpdate: Story = {
  render: () => (
    <>
      <PurchaseUpdateStep status="running" />
      <PurchaseUpdateStep status="complete" result={PURCHASE_COMPLETED} elapsedMs={90} />
      <PurchaseUpdateStep status="complete" result={PURCHASE_CANCELED} elapsedMs={70} />
      <PurchaseUpdateStep status="complete" result={PURCHASE_FAILED} elapsedMs={80} />
      <PurchaseUpdateStep status="error" error="No encontré esa compra." />
    </>
  ),
}

export const Reasoning: Story = {
  render: () => (
    <>
      <ReasoningStep text={REASONING_TEXT.slice(0, 140)} streaming />
      <ReasoningStep text={REASONING_TEXT} elapsedMs={2100} />
    </>
  ),
}

/** Tiempos largos y textos de error extensos. */
export const LongText: Story = {
  render: () => (
    <>
      <StepRow
        status="complete"
        title="Paso con un título muy largo que describe con detalle lo que hizo el agente en este punto"
        summary="Un resumen igualmente largo para comprobar que el texto se ajusta sin desbordar la línea de tiempo en pantallas angostas."
        elapsedMs={754000}
      />
      <SearchCatalogStep
        status="error"
        error={`${TOOL_ERROR} ${TOOL_ERROR} ${TOOL_ERROR}`}
        elapsedMs={61000}
      />
    </>
  ),
}

export const Mobile: Story = {
  globals: { viewport: { value: "mobile", isRotated: false } },
  render: () => (
    <>
      <InterpretRequestStep status="complete" result={INTERPRET_PURCHASE} elapsedMs={1180} />
      <SearchCatalogStep status="complete" result={SEARCH_RESULT_MANY} elapsedMs={950} />
      <PrepareCheckoutStep status="complete" result={PREPARE_PRICE_CHANGED} elapsedMs={2480} />
      <PurchaseUpdateStep status="complete" result={PURCHASE_COMPLETED} elapsedMs={90} />
    </>
  ),
}
