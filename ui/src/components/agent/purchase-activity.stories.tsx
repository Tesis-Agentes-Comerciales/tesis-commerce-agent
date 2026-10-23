import type { Meta, StoryObj } from "@storybook/react-vite"
import { useState } from "react"
import { expect, waitFor } from "storybook/test"

import { PolicyBlockedCard } from "@/components/agent/policy-blocked-card"
import { PurchaseActivity } from "@/components/agent/purchase-activity"
import type { PurchaseTrace, TraceStep } from "@/lib/trace-contract"
import { decisionOf, lastStepOf, stepOf, TRACES } from "@/storybook/trace-fixtures"

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

/** Filas de primer nivel de la línea de tiempo: las reglas de la política forman una lista anidada. */
function timelineRows(canvasElement: HTMLElement) {
  const timeline = canvasElement.querySelector("ol[aria-label='Controles de la compra']")
  return Array.from(timeline?.querySelectorAll(":scope > li") ?? [])
}

const PACED_ROWS = TRACES.completedShadow.steps.length

/**
 * Como en el chat: abierta y a ritmo (`paced`). Las filas entran de a una, en su orden, y al plegar
 * y volver a abrir las que ya se vieron salen enteras, sin animarse otra vez.
 */
export const Paced: Story = {
  args: { trace: TRACES.completedShadow, defaultOpen: true, paced: true },
  play: async ({ canvas, canvasElement, userEvent }) => {
    const trigger = canvas.getByRole("button", { name: /Compra completada en \d+ s/ })
    await expect(trigger).toHaveAttribute("aria-expanded", "true")
    const start = timelineRows(canvasElement).length
    await expect(start).toBeGreaterThan(0)
    await expect(start).toBeLessThan(PACED_ROWS)
    const counts: number[] = []
    const observer = new MutationObserver(() => counts.push(timelineRows(canvasElement).length))
    observer.observe(canvas.getByRole("list", { name: "Controles de la compra" }), {
      childList: true,
    })
    await waitFor(() => expect(timelineRows(canvasElement)).toHaveLength(PACED_ROWS), {
      timeout: PACED_ROWS * 350 + 2000,
    })
    observer.disconnect()
    // De a una: cada cambio agrega una sola fila.
    await expect(counts).toEqual(
      Array.from({ length: PACED_ROWS - start }, (_, index) => start + index + 1),
    )
    const titles = timelineRows(canvasElement).map((row) => row.textContent)

    await userEvent.click(trigger)
    await waitFor(() =>
      expect(canvas.queryByRole("list", { name: "Controles de la compra" })).toBeNull(),
    )
    await userEvent.click(trigger)
    await expect(timelineRows(canvasElement).map((row) => row.textContent)).toEqual(titles)
    await expect(canvasElement.querySelectorAll("ol > li.agent-enter")).toHaveLength(0)
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

const PRODUCT = stepOf(TRACES.completedShadow, "selection").product

/** La fila nombra el producto y el comercio; el origen del comercio no aparece. */
export const NamesTheMerchant: Story = {
  play: async ({ canvas }) => {
    await expect(canvas.getByText(`${PRODUCT} · Synthetic Commerce Merchant`)).toBeVisible()
    await expect(canvas.queryByText(/127\.0\.0\.1/)).toBeNull()
  },
}

/** Sin nombre (un comercio que no es el configurado): solo el producto, nunca el origen. */
export const WithoutMerchantName: Story = {
  args: { trace: { ...TRACES.completedShadow, merchant_name: null }, defaultOpen: true },
  play: async ({ canvas }) => {
    await expect(canvas.getByText(PRODUCT)).toBeVisible()
    await expect(canvas.queryByText(/Synthetic Commerce Merchant/)).toBeNull()
    await expect(canvas.queryByText(/127\.0\.0\.1/)).toBeNull()
  },
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
  render: () => <RereadTrace trace={TRACES.awaitingApproval} />,
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
      <PolicyBlockedCard decision={decisionOf(TRACES.blockedMerchant)} />
    </div>
  ),
}

/** La política permitió, pero el allow no se pudo dejar listo: la fila no culpa a la política. */
export const BlockedAllowNotApplied: Story = {
  args: {
    trace: {
      ...TRACES.blockedMerchant,
      steps: TRACES.blockedMerchant.steps.map((step) =>
        step.kind === "policy_decision"
          ? {
              ...step,
              decision: {
                ...step.decision,
                outcome: "allow" as const,
                rules: step.decision.rules.map((rule) => ({ ...rule, result: "pass" as const })),
                reason_codes: [],
              },
            }
          : step,
      ),
    },
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("button", { name: /Detenida por precaución/ })).toBeVisible()
    await expect(canvas.queryByText(/Bloqueada por la política/)).toBeNull()
  },
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

/**
 * S04: la frescura detuvo la aprobación. La fila ya no dice «Esperando tu aprobación» y, abierta,
 * muestra qué cambió; la tarjeta de términos que ya no valen va fuera, con «Cancelar compra».
 */
export const StaleTerms: Story = {
  args: { trace: TRACES.staleTerms },
  play: async ({ canvas, userEvent }) => {
    const trigger = canvas.getByRole("button", { name: /Detenida: cambiaron las condiciones/ })
    await expect(canvas.queryByText(/Esperando tu aprobación|política de compra/)).toBeNull()
    await userEvent.click(trigger)
    await waitFor(() =>
      expect(canvas.getByText("Las condiciones del comercio cambiaron")).toBeVisible(),
    )
    await expect(canvas.getByRole("list", { name: "Qué cambió" })).toBeVisible()
    await userEvent.click(canvas.getByText("Detalle técnico"))
    await expect(canvas.getByText(/^approve · changed · enforce · revisión 2$/)).toBeVisible()
  },
}

export const StaleTermsUnavailable: Story = {
  args: {
    trace: {
      ...TRACES.staleTerms,
      steps: TRACES.staleTerms.steps.map((step) =>
        step.kind === "freshness_check" && step.state === "blocked"
          ? { ...step, outcome: "unavailable", changes: [], read_error: "freshness_check_failed" }
          : step,
      ),
    },
  },
  play: async ({ canvas }) => {
    await expect(
      canvas.getByRole("button", { name: /Detenida: condiciones sin confirmar/ }),
    ).toBeVisible()
  },
}

/** La oferta venció: la fila lo dice igual que el chat. */
export const StaleTermsOfferExpired: Story = {
  args: {
    trace: {
      ...TRACES.staleTerms,
      steps: TRACES.staleTerms.steps.map((step) =>
        step.kind === "freshness_check" && step.state === "blocked"
          ? { ...step, outcome: "unavailable", changes: [], read_error: "offer_expired" }
          : step,
      ),
    },
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("button", { name: /Detenida: la oferta venció/ })).toBeVisible()
  },
}

/** S02: la compra se canceló por presupuesto; al abrirla, la explicación del agente. */
export const GovernedRecovery: Story = {
  args: { trace: TRACES.governedRecovery, defaultOpen: true },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("button", { name: /Compra cancelada/ })).toBeVisible()
    await expect(canvas.getByText("El agente explicó por qué no siguió")).toBeVisible()
  },
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
  TRACES.staleTerms,
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
    for (const summary of [
      "Bloqueada por la política de compra",
      "Esperando tu aprobación",
      "Detenida: cambiaron las condiciones",
    ]) {
      const label = canvas.getByText(summary, { selector: "button span" })
      await expect(label.scrollWidth).toBeLessThanOrEqual(label.clientWidth)
    }
  },
}

/** Nada de la fila ni de lo que hay dentro (anillos de foco incluidos) sale de la columna del hilo. */
async function expectNoHorizontalOverflow(canvasElement: HTMLElement, width: number) {
  const root = canvasElement.querySelector<HTMLElement>("[data-testid='phone-column']")
  await expect(root?.scrollWidth).toBeLessThanOrEqual(width)
  for (const element of canvasElement.querySelectorAll<HTMLElement>("[data-testid='phone-column'] *")) {
    if (element.classList.contains("sr-only")) continue
    await expect(element.getBoundingClientRect().right).toBeLessThanOrEqual(
      (root?.getBoundingClientRect().right ?? 0) + 1,
    )
  }
}

/**
 * Teléfono de 320 px: el hilo deja 288 px y el mensaje, con su `px-2`, 272. El resumen pasa a dos
 * líneas en vez de cortarse («Bloqueada por la política de co…»).
 */
export const Phone320: Story = {
  render: () => (
    <div className="w-[288px] space-y-1 px-2" data-testid="phone-column">
      {ALL_STATUSES.map((trace) => (
        <PurchaseActivity key={trace.run_id} trace={trace} />
      ))}
    </div>
  ),
  play: async ({ canvas, canvasElement }) => {
    for (const summary of [
      "Bloqueada por la política de compra",
      "Detenida: cambiaron las condiciones",
      "Compra por confirmar con el comercio",
    ]) {
      const label = canvas.getByText(summary, { selector: "button span" })
      await expect(label.scrollWidth).toBeLessThanOrEqual(label.clientWidth)
      await expect(label.scrollHeight).toBeLessThanOrEqual(label.clientHeight)
    }
    await expectNoHorizontalOverflow(canvasElement, 288)
  },
}

/**
 * A 320 px, abierta y con el detalle técnico: los identificadores se leen enteros, debajo de su
 * rótulo, en lugar de quedar en «sh…».
 */
export const Phone320Open: Story = {
  args: { trace: TRACES.staleTerms, defaultOpen: true },
  render: (args) => (
    <div className="w-[288px] px-2" data-testid="phone-column">
      <PurchaseActivity {...args} />
    </div>
  ),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await userEvent.click(canvas.getByText("Detalle técnico"))
    const digest = lastStepOf(TRACES.staleTerms, "freshness_check").evidence_digest
    const value = canvas.getByText(digest)
    await expect(value).toBeVisible()
    await expect(value.scrollWidth).toBeLessThanOrEqual(value.clientWidth)
    await expectNoHorizontalOverflow(canvasElement, 288)
  },
}

export const Mobile: Story = {
  args: { trace: TRACES.completedShadow, defaultOpen: true },
  globals: { viewport: { value: "mobile", isRotated: false } },
}

/**
 * Antes de la traza: la fila sale con lo que dice el stream, a su altura final, y no se abre
 * todavía. El comercio llega con la traza.
 */
export const FromStream: Story = {
  args: {
    trace: null,
    stream: { status: "in_progress", summary: "Comprando…", product: "Sony WH-1000XM6 negro" },
  },
  play: async ({ canvas }) => {
    const trigger = canvas.getByRole("button", { name: /Comprando…/ })
    await expect(trigger).toBeDisabled()
    await expect(canvas.getByText("Sony WH-1000XM6 negro")).toBeVisible()
  },
}

export const FromStreamBlocked: Story = {
  args: {
    trace: null,
    stream: {
      status: "blocked",
      summary: "Bloqueada por la política de compra",
      product: "Sony WH-1000XM6 negro",
    },
  },
}

function TraceArrives() {
  const [trace, setTrace] = useState<PurchaseTrace | null>(null)
  return (
    <div className="space-y-3">
      <PurchaseActivity
        trace={trace}
        stream={{
          status: "blocked",
          summary: "Bloqueada por la política de compra",
          product: "Sony WH-1000XM6 negro",
        }}
      />
      <button type="button" onClick={() => setTrace(TRACES.blockedMerchant)}>
        Llega la traza
      </button>
    </div>
  )
}

/** Al llegar la traza, la fila se completa en su lugar: la misma altura, ahora se puede abrir. */
export const TraceArrivesInPlace: Story = {
  render: () => <TraceArrives />,
  play: async ({ canvas, userEvent }) => {
    const row = canvas.getByTestId("purchase-activity")
    const before = row.getBoundingClientRect().height
    await expect(row).toHaveAttribute("data-source", "stream")
    await userEvent.click(canvas.getByRole("button", { name: "Llega la traza" }))
    await expect(row).toHaveAttribute("data-source", "trace")
    await expect(row.getBoundingClientRect().height).toBe(before)
    await expect(
      canvas.getByRole("button", { name: /Bloqueada por la política de compra/ }),
    ).toBeEnabled()
  },
}

export const FromStreamPhone320: Story = {
  args: FromStreamBlocked.args,
  render: (args) => (
    <div className="w-[288px] px-2" data-testid="phone-column">
      <PurchaseActivity {...args} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectNoHorizontalOverflow(canvasElement, 288)
  },
}
