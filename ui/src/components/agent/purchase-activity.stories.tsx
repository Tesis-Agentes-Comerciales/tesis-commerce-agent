import type { Meta, StoryObj } from "@storybook/react-vite"
import { useState } from "react"
import { expect, waitFor } from "storybook/test"

import { PolicyBlockedCard } from "@/components/agent/policy-blocked-card"
import { PurchaseActivity } from "@/components/agent/purchase-activity"
import type { PurchaseTrace, TraceStep } from "@/lib/trace-contract"
import { decisionOf, stepOf, TRACES } from "@/storybook/trace-fixtures"

const meta = {
  title: "Agent/PurchaseActivity",
  component: PurchaseActivity,
  tags: ["autodocs"],
  args: { trace: TRACES.completedShadow },
  decorators: [
    (Story) => (
      <div className="w-[min(44rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof PurchaseActivity>

export default meta
type Story = StoryObj<typeof meta>

/** Plegada al terminar; abrirla muestra la política permitida con sus dos reglas. */
export const Completed: Story = {
  play: async ({ canvas, userEvent }) => {
    const trigger = canvas.getByRole("button", { name: /Compra completada en \d+ s/ })
    await expect(trigger).toHaveAttribute("aria-expanded", "false")
    await expect(canvas.queryByText("Política de compra: permitida")).toBeNull()

    await userEvent.click(trigger)
    await expect(trigger).toHaveAttribute("aria-expanded", "true")
    // El contenido entra con un fundido corto.
    await waitFor(() => expect(canvas.getByText("Política de compra: permitida")).toBeVisible())
    await expect(canvas.getByText("Producto dentro de lo que pediste")).toBeVisible()
    await expect(canvas.getByText("Comercio permitido")).toBeVisible()

    await userEvent.click(trigger)
    await expect(trigger).toHaveAttribute("aria-expanded", "false")
  },
}

const LATE_WEBHOOK_AT = "2026-10-20T16:30:00.000Z"

/** Un aviso del comercio que llega 48 min después no alarga la duración de la compra. */
export const CompletedLateWebhook: Story = {
  args: {
    trace: {
      ...TRACES.completedShadow,
      updated_at: LATE_WEBHOOK_AT,
      steps: TRACES.completedShadow.steps.map((step) =>
        step.kind === "order_event" ? { ...step, at: LATE_WEBHOOK_AT } : step,
      ),
    },
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("button", { name: /Compra completada en 47 s/ })).toBeVisible()
  },
}

export const CompletedOpen: Story = {
  args: { defaultOpen: true },
}

/** El teclado abre la fila y el foco queda visible. */
export const KeyboardToggle: Story = {
  play: async ({ canvas, userEvent }) => {
    const trigger = canvas.getByRole("button", { name: /Compra completada/ })
    const resting = getComputedStyle(trigger).boxShadow
    await userEvent.tab()
    await expect(trigger).toHaveFocus()
    await expect(trigger.matches(":focus-visible")).toBe(true)
    // El anillo se pinta de verdad: la sombra cambia y lleva la tinta del chat.
    const focused = getComputedStyle(trigger).boxShadow
    await expect(focused).not.toBe(resting)
    await expect(focused).toContain("rgb(13, 13, 13)")
    await userEvent.keyboard("{Enter}")
    await expect(trigger).toHaveAttribute("aria-expanded", "true")
    await userEvent.keyboard(" ")
    await expect(trigger).toHaveAttribute("aria-expanded", "false")
  },
}

export const CompletedEnforce: Story = {
  args: { trace: TRACES.completedEnforce, defaultOpen: true },
}

export const CompletedPolicyOff: Story = {
  args: { trace: TRACES.policyOff, defaultOpen: true },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Política desactivada")).toBeVisible()
  },
}

export const InProgress: Story = {
  args: { trace: TRACES.inProgress },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("button", { name: /Comprando…/ })).toHaveAttribute(
      "aria-busy",
      "true",
    )
  },
}

export const AwaitingApproval: Story = {
  args: { trace: TRACES.awaitingApproval },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("button", { name: /Esperando tu aprobación/ })).toBeVisible()
  },
}

/** Simula la relectura de la traza después de completar la compra. */
function RereadTrace({ trace: initial }: { trace: PurchaseTrace }) {
  const [trace, setTrace] = useState(initial)
  return (
    <div className="space-y-3">
      <PurchaseActivity trace={trace} />
      <button type="button" onClick={() => setTrace(TRACES.completedShadow)}>
        Releer la traza
      </button>
    </div>
  )
}

/** Al releer la traza cambia el estado: un lector de pantalla lo oye sin mover el foco. */
export const AnnouncesStatusChanges: Story = {
  args: { trace: TRACES.awaitingApproval },
  render: (args) => <RereadTrace trace={args.trace} />,
  play: async ({ canvas, userEvent }) => {
    const status = canvas.getByRole("status")
    await expect(status).toHaveTextContent("Esperando tu aprobación")
    await userEvent.click(canvas.getByRole("button", { name: "Releer la traza" }))
    await expect(status).toHaveTextContent(/Compra completada en \d+ s/)
  },
}

export const Blocked: Story = {
  args: { trace: TRACES.blockedMerchant },
  play: async ({ canvas }) => {
    await expect(
      canvas.getByRole("button", { name: /Bloqueada por la política de compra/ }),
    ).toBeVisible()
    await expect(canvas.queryByText(/error inesperado/i)).toBeNull()
  },
}

/** Como se ve en el chat: la fila plegada y, fuera de ella, la tarjeta del bloqueo. */
export const BlockedWithCard: Story = {
  args: { trace: TRACES.blockedMerchant },
  render: (args) => (
    <div className="space-y-3">
      <PurchaseActivity {...args} />
      <PolicyBlockedCard decision={decisionOf(args.trace)} />
    </div>
  ),
}

export const BlockedProduct: Story = {
  args: { trace: TRACES.blockedProduct },
}

export const PolicyUnavailable: Story = {
  args: { trace: TRACES.policyUnavailable },
}

export const Canceled: Story = {
  args: { trace: TRACES.canceled },
}

/** Un fallo técnico se ve en rojo, distinto del bloqueo por política. */
export const Failed: Story = {
  args: { trace: TRACES.failed, defaultOpen: true },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("La compra no se pudo completar", { selector: "button span" }))
      .toHaveClass("text-red-700")
  },
}

export const Uncertain: Story = {
  args: { trace: TRACES.uncertain },
}

const SHADOW_DECISION = stepOf(TRACES.completedShadow, "policy_decision")
const FIRST_DECISION_ID = "decision:0f1e2d3c4b5a69788796a5b4c3d2e1f0"

/** La compra pasó dos veces por la política: el detalle técnico solo lista la última decisión. */
export const RepeatedPolicyDecision: Story = {
  args: {
    defaultOpen: true,
    trace: {
      ...TRACES.completedShadow,
      steps: TRACES.completedShadow.steps.flatMap((step): TraceStep[] =>
        step.kind === "policy_decision"
          ? [
              {
                ...SHADOW_DECISION,
                id: "policy_decision:1",
                decision: {
                  ...SHADOW_DECISION.decision,
                  decision_id: FIRST_DECISION_ID,
                  evidence_id: "evidence:0f1e2d3c4b5a69788796a5b4c3d2e1f1",
                },
              },
              step,
            ]
          : [step],
      ),
    },
  },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByText("Detalle técnico"))
    await expect(canvas.getByText(SHADOW_DECISION.decision.decision_id)).toBeVisible()
    await expect(canvas.queryByText(FIRST_DECISION_ID)).toBeNull()
    await expect(canvas.getAllByText("Decisión")).toHaveLength(1)
  },
}

/** Los identificadores internos quedan en el detalle técnico, cerrado por defecto. */
export const TechnicalDetail: Story = {
  args: { trace: TRACES.completedShadow, defaultOpen: true },
  play: async ({ canvas, userEvent }) => {
    const decisionId = decisionOf(TRACES.completedShadow).decision_id
    await expect(canvas.getByText(decisionId)).not.toBeVisible()
    await userEvent.click(canvas.getByText("Detalle técnico"))
    await expect(canvas.getByText(decisionId)).toBeVisible()
    await expect(canvas.getByText("ALL_APPLICABLE_POLICIES_SATISFIED")).toBeVisible()
  },
}

const ALL_STATUSES = [
  TRACES.inProgress,
  TRACES.awaitingApproval,
  TRACES.completedShadow,
  TRACES.blockedMerchant,
  TRACES.canceled,
  TRACES.failed,
  TRACES.uncertain,
]

/**
 * Teléfono de 375 px: la columna del chat deja 343 px. El resumen del estado nunca se corta; el
 * nombre del producto cede el espacio.
 */
export const Phone375: Story = {
  render: () => (
    <div className="w-[343px] space-y-1">
      {ALL_STATUSES.map((trace) => (
        <PurchaseActivity key={trace.run_id} trace={trace} />
      ))}
    </div>
  ),
  play: async ({ canvas }) => {
    for (const summary of ["Bloqueada por la política de compra", "Esperando tu aprobación"]) {
      const label = canvas.getByText(summary, { selector: "button span" })
      await expect(label.scrollWidth).toBeLessThanOrEqual(label.clientWidth)
    }
  },
}

export const Mobile: Story = {
  args: { trace: TRACES.completedShadow, defaultOpen: true },
  globals: { viewport: { value: "mobile", isRotated: false } },
}
