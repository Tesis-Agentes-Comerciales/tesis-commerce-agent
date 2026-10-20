import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, within } from "storybook/test"

import { ControlStep } from "@/components/trace/control-step"
import { lastStepOf, stepOf, TRACES } from "@/storybook/trace-fixtures"

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
    await expect(canvas.getByText("El pago sandbox fue rechazado.")).toBeVisible()
    await expect(canvas.queryByText(/payment_declined/)).toBeNull()
  },
}

// Controles de la aplicación del flujo HP gobernado: nunca se presentan como política.

const FRESH = stepOf(TRACES.staleTerms, "freshness_check")
const CHANGED = lastStepOf(TRACES.staleTerms, "freshness_check")
const RECOVERY = stepOf(TRACES.governedRecovery, "governed_recovery")
/** Ni la política ni sus códigos: la frescura y la recuperación son controles de la aplicación. */
const NOT_POLICY = /política|regla|POL-0|D1|freshness|changed|enforce|budget_exceeded/i

/** S04, al presentar los términos: la oferta viva coincide con el checkout. */
export const FreshnessConfirmed: Story = {
  args: { step: FRESH },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Condiciones confirmadas con el comercio")).toBeVisible()
    await expect(canvas.getByText("Antes de mostrarte los términos")).toBeVisible()
    await expect(canvas.queryByRole("list", { name: "Qué cambió" })).toBeNull()
    await expect(canvas.queryByText(NOT_POLICY)).toBeNull()
  },
}

/** Lo que no ocurrió, igual en las dos etapas. */
const STOPPED = "No se registró ninguna aprobación ni se firmó ninguna autorización."

/** S04, al aprobar: el precio cambió y el control detuvo la aprobación antes de firmar. */
export const FreshnessBlocked: Story = {
  args: { step: CHANGED },
  play: async ({ canvas, canvasElement }) => {
    await expect(canvas.getByText("Las condiciones del comercio cambiaron")).toBeVisible()
    await expect(canvas.getByText(STOPPED)).toBeVisible()
    // Escudo ámbar como cualquier bloqueo, pero sin atribuirlo a la política.
    await expect(canvasElement.querySelector("li")).toHaveAttribute("data-state", "blocked")
    await expect(canvas.getByText("Detenido")).toHaveClass("sr-only")
    const changes = canvas.getByRole("list", { name: "Qué cambió" })
    const total = within(changes).getByText("Total").closest("li")
    await expect(total).toHaveTextContent(/En el checkout\$\s?1\.924\.000(,00)? COP/)
    await expect(total).toHaveTextContent(/Oferta actual\$\s?2\.024\.000(,00)? COP/)
    await expect(within(changes).getByText("Precio del producto")).toBeVisible()
    // S04 también renueva la vigencia: en el mismo minuto, los segundos las distinguen.
    const expiry = within(changes).getByText("Vigencia de la oferta").closest("li")
    await expect(expiry).toHaveTextContent(/En el checkout\d{2}\/\d{2}\/\d{4},? \d{1,2}:\d{2}:12\b/)
    await expect(expiry).toHaveTextContent(/Oferta actual\d{2}\/\d{2}\/\d{4},? \d{1,2}:\d{2}:48\b/)
    await expect(canvas.queryByText(NOT_POLICY)).toBeNull()
  },
}

/**
 * S04 en el chat: al pulsar «Aprobar» la UI relee los términos (etapa `present`) y la frescura los
 * rechaza ahí, después de haberlos mostrado. El texto no dice que no se mostraron.
 */
export const FreshnessBlockedWhenRereading: Story = {
  args: { step: lastStepOf(TRACES.staleTermsReread, "freshness_check") },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Las condiciones del comercio cambiaron")).toBeVisible()
    await expect(canvas.getByText(STOPPED)).toBeVisible()
    await expect(canvas.queryByText(/No se mostraron/)).toBeNull()
    await expect(canvas.getByRole("list", { name: "Qué cambió" })).toBeVisible()
  },
}

/** No se pudo leer la oferta: falla cerrado, sin lista de cambios. */
export const FreshnessUnavailable: Story = {
  args: {
    step: { ...CHANGED, outcome: "unavailable", changes: [], read_error: "freshness_check_failed" },
  },
  play: async ({ canvas }) => {
    await expect(
      canvas.getByText("No se pudieron confirmar las condiciones con el comercio"),
    ).toBeVisible()
    await expect(canvas.getByText("Por precaución, la compra se detuvo.")).toBeVisible()
    await expect(canvas.queryByText(/freshness_check_failed/)).toBeNull()
  },
}

/** Modo observando: el cambio se registra y la compra sigue. */
export const FreshnessObserved: Story = {
  args: { step: { ...CHANGED, mode: "shadow", state: "done" } },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Las condiciones del comercio cambiaron")).toBeVisible()
    await expect(canvas.getByText("Solo observando: registra, no detiene")).toBeVisible()
    await expect(canvas.getByRole("list", { name: "Qué cambió" })).toBeVisible()
  },
}

/** Vigencia, días de entrega, disponibilidad y moneda, cada uno con su formato. */
export const FreshnessOtherFields: Story = {
  args: {
    step: {
      ...CHANGED,
      changes: [
        { field: "expires_at", presented: "2026-10-20T16:25:31.020Z", current: null },
        { field: "delivery_days", presented: 2, current: 1 },
        { field: "stock", presented: 1, current: 0 },
        { field: "currency", presented: "COP", current: "USD" },
      ],
    },
  },
  play: async ({ canvas }) => {
    const changes = canvas.getByRole("list", { name: "Qué cambió" })
    const expiry = within(changes).getByText("Vigencia de la oferta").closest("li")
    await expect(expiry).toHaveTextContent(/En el checkout\d{2}\/\d{2}\/\d{4},? \d{1,2}:\d{2}:31\b/)
    await expect(expiry).toHaveTextContent(/Oferta actualSin dato/)
    await expect(canvas.getByText("2 días")).toBeVisible()
    await expect(canvas.getByText("1 día")).toBeVisible()
    // El checkout guarda la cantidad pedida, no las existencias: cada valor dice cuál es.
    const stock = within(changes).getByText("Disponibilidad").closest("li")
    await expect(stock).toHaveTextContent(/Unidades pedidas1Unidades disponibles0/)
    await expect(stock).not.toHaveTextContent(/En el checkout|Oferta actual/)
    await expect(canvas.getByText("USD")).toBeVisible()
  },
}

/** Dos vigencias en el mismo segundo: se agregan los milisegundos para que no se lean iguales. */
export const FreshnessSameSecondExpiry: Story = {
  args: {
    step: {
      ...CHANGED,
      changes: [
        {
          field: "expires_at",
          presented: "2027-10-20T16:10:12.000Z",
          current: "2027-10-20T16:10:12.250Z",
        },
      ],
    },
  },
  play: async ({ canvas }) => {
    const expiry = canvas.getByText("Vigencia de la oferta").closest("li")
    await expect(expiry).toHaveTextContent(/En el checkout[^O]*:12,000\b/)
    await expect(expiry).toHaveTextContent(/Oferta actual.*:12,250\b/)
  },
}

/** S02 con checkout: el total superó el presupuesto, el agente lo canceló y lo explicó. */
export const GovernedRecovery: Story = {
  args: { step: RECOVERY },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("El agente explicó por qué no siguió")).toBeVisible()
    await expect(
      canvas.getByText(
        /^Superaba tu presupuesto de \$\s?1\.000\.000(,00)? COP\. Se canceló el checkout antes de cobrar\.$/,
      ),
    ).toBeVisible()
    await expect(canvas.queryByText(NOT_POLICY)).toBeNull()
  },
}

/** S02 sin checkout: el modelo eligió fuera de la lista corta. */
export const GovernedRecoveryOutsideShortlist: Story = {
  args: {
    step: {
      ...RECOVERY,
      recovery: "selection_outside_shortlist",
      cause: "not_shortlisted",
      budget_minor: null,
    },
  },
  play: async ({ canvas }) => {
    await expect(
      canvas.getByText(
        "La opción elegida no estaba entre las verificadas para tu pedido. No se creó ningún checkout.",
      ),
    ).toBeVisible()
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
