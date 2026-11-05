import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { PurchaseActivity } from "@/components/agent/purchase-activity"
import { ControlTimeline } from "@/components/trace/control-timeline"
import type { PurchaseTrace, TraceStep } from "@/lib/trace-contract"
import { stepOf } from "@/storybook/trace-fixtures"
import { HNP_TRACES } from "@/storybook/trace-fixtures/hnp"

// Compra delegada (HNP): el consentimiento reemplaza al pedido y la aprobación; D2 va antes de
// reservar el uso único y D3 antes de enviar el pago (docs/design/hnp-governed.md §10).

function rows(canvasElement: HTMLElement) {
  const timeline = canvasElement.querySelector("ol[aria-label='Controles de la compra']")
  return Array.from(timeline?.querySelectorAll(":scope > li") ?? [])
}

function titles(canvasElement: HTMLElement) {
  return rows(canvasElement).map((row) => row.querySelector("p")?.textContent)
}

function states(canvasElement: HTMLElement) {
  return rows(canvasElement).map((row) => row.getAttribute("data-state"))
}

const HP_ONLY = ["Pedido recibido", "Producto elegido", "Tu aprobación", "Autorización firmada"]
const NOT_REACHED = ["Reservar el uso único", "Cerrar la autorización", "Autorizar el pago", "Confirmar la orden"]
const NO_CHARGE = "No se hizo ningún cobro."
const PRECAUTION = "Por precaución, la compra se detuvo."

const meta = {
  title: "Trace/ControlTimeline HNP",
  component: ControlTimeline,
  args: { trace: HNP_TRACES.enforceCompleted },
  decorators: [
    (Story) => (
      <div className="w-[min(40rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ControlTimeline>

export default meta
type Story = StoryObj<typeof meta>

/** B2-HNP: tres decisiones de política, el uso único y ningún paso del comprador después de irse. */
export const EnforceCompleted: Story = {
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement)).toEqual([
      "Delegación autorizada",
      "Compra delegada en marcha",
      "Política de compra: permitida",
      "Checkout creado en el comercio",
      "Oferta vigente al cerrar",
      "Autorización delegada verificada",
      "Uso único reservado",
      "Autorización cerrada por el agente",
      "Despacho autorizado",
      "Pago autorizado",
      "Orden confirmada",
      "Uso único consumido",
    ])
    await expect(canvas.getByText(/^Hasta \$\s?2\.000\.000(,00)? COP · vence /)).toBeVisible()
    // El recibo de pago del PSP: el monto del uso único, como el pago de HP.
    await expect(rows(canvasElement)[9]).toHaveTextContent(/Hasta \$\s?1\.924\.000(,00)? COP/)
    await expect(canvas.getByText("Antes de cerrar la compra delegada")).toBeVisible()
    await expect(canvas.getByText("Antes de reservar el uso único")).toBeVisible()
    await expect(canvas.getByText("Antes de enviar el pago")).toBeVisible()
    for (const title of HP_ONLY) await expect(canvas.queryByText(title)).toBeNull()
    await expect(canvas.queryByText("No se ejecutó")).toBeNull()
  },
}

/** B0-HNP: la política apagada figura una vez, antes del checkout; D2 y D3 no existen sin ella. */
export const OffCompleted: Story = {
  args: { trace: HNP_TRACES.offCompleted },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement)).toEqual([
      "Delegación autorizada",
      "Compra delegada en marcha",
      "Política desactivada",
      "Checkout creado en el comercio",
      "Uso único reservado",
      "Autorización cerrada por el agente",
      "Pago autorizado",
      "Orden confirmada",
      "Uso único consumido",
    ])
    await expect(canvas.queryByText("Despacho autorizado")).toBeNull()
  },
}

/** D2 niega por POL-08: la decisión vuelve al comprador sin reservar ni cobrar. */
export const D2Denied: Story = {
  args: { trace: HNP_TRACES.d2Denied },
  play: async ({ canvas, canvasElement }) => {
    const all = titles(canvasElement)
    await expect(all.slice(5)).toEqual([
      "La política no autorizó la compra",
      "Necesito tu decisión",
      ...NOT_REACHED,
    ])
    await expect(states(canvasElement).slice(5)).toEqual([
      "blocked",
      "blocked",
      "skipped",
      "skipped",
      "skipped",
      "skipped",
    ])
    await expect(rows(canvasElement)[5]).toHaveTextContent(`No se reservó el uso único. ${NO_CHARGE}`)
    await expect(canvas.getByText("Compra dentro de lo que autorizaste")).toBeVisible()
    await expect(rows(canvasElement)[6]).toHaveTextContent(
      "El total del comercio, con envío, o algún término quedó fuera de lo que autorizaste.",
    )
  },
}

/** La prueba del mandato abierto quedó sin resolver: no hay regla que falle y se escala. */
export const Escalated: Story = {
  args: { trace: HNP_TRACES.escalated },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement).slice(5, 7)).toEqual([
      "No se pudo verificar la autorización",
      "Necesito tu decisión",
    ])
    await expect(
      canvas.getByText(`No pude comprobar que la compra encaje en tu autorización. ${NO_CHARGE}`),
    ).toBeVisible()
  },
}

// El revisor de intención (docs/design/hnp-intent-review.md): compara la elección con lo que pidió
// el comprador después de activar la tarea y antes de la política.

const CONFIRMED: TraceStep = {
  ...stepOf(HNP_TRACES.enforceReviewNotConfirmed, "choice_reviewed"),
  at: "2026-10-21T15:00:01.390Z",
  state: "done",
  decision: "confirmed",
  reason: "matches_request",
}

/** El revisor confirmó la elección: la compra sigue igual, con una fila más antes de la política. */
export const ReviewConfirmed: Story = {
  args: {
    trace: {
      ...HNP_TRACES.enforceCompleted,
      steps: HNP_TRACES.enforceCompleted.steps.flatMap((step): TraceStep[] =>
        step.kind === "task_activated" ? [step, CONFIRMED] : [step],
      ),
    },
  },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement).slice(1, 4)).toEqual([
      "Compra delegada en marcha",
      "Revisión de la elección",
      "Política de compra: permitida",
    ])
    await expect(rows(canvasElement)[2]).toHaveAttribute("data-state", "done")
    await expect(canvas.getByText("Confirmada: coincide con lo que pediste.")).toBeVisible()
    await expect(titles(canvasElement).at(-1)).toBe("Uso único consumido")
  },
}

/** El revisor no confirmó la elección: escala antes de la política, sin checkout ni cobro. */
export const ReviewNotConfirmed: Story = {
  args: { trace: HNP_TRACES.enforceReviewNotConfirmed },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement)).toEqual([
      "Delegación autorizada",
      "Compra delegada en marcha",
      "Revisión de la elección",
      "Necesito tu decisión",
      "Revisar la política de compra",
      "Crear el checkout",
      ...NOT_REACHED,
    ])
    await expect(states(canvasElement).slice(2)).toEqual([
      "blocked",
      "blocked",
      "skipped",
      "skipped",
      "skipped",
      "skipped",
      "skipped",
      "skipped",
    ])
    await expect(canvas.getByText("No confirmada: contradice tu indicación.")).toBeVisible()
    await expect(
      canvas.getByText(`No pude confirmar que la opción elegida sea la que pediste. ${NO_CHARGE}`),
    ).toBeVisible()
    // Con el revisor encendido la política está activa: no aparece como desactivada.
    await expect(canvas.queryByText("Política desactivada")).toBeNull()
  },
}

/** La frescura al cerrar detuvo la compra: qué cambió y el escalamiento, sin llegar a D2. */
export const OfferChangedAtClose: Story = {
  args: { trace: HNP_TRACES.offerChanged },
  play: async ({ canvas, canvasElement }) => {
    // El ejecutor se detiene en la frescura: D2 no llega a correr.
    await expect(titles(canvasElement).slice(4)).toEqual([
      "Las condiciones del comercio cambiaron",
      "Necesito tu decisión",
      ...NOT_REACHED,
    ])
    await expect(rows(canvasElement)[4]).toHaveAttribute("data-state", "blocked")
    await expect(rows(canvasElement)[4]).toHaveTextContent(`No se reservó el uso único. ${NO_CHARGE}`)
    await expect(canvas.getByRole("list", { name: "Qué cambió" })).toBeVisible()
    await expect(
      canvas.getByText(`La oferta del comercio cambió antes de que cerrara la compra. ${NO_CHARGE}`),
    ).toBeVisible()
  },
}

/** El dueño revocó con el pago ya comprometido: la compra sigue en curso, sin controles omitidos. */
export const RevokedInFlight: Story = {
  args: { trace: HNP_TRACES.revokedInFlight },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement).at(-1)).toBe("Delegación revocada")
    await expect(
      canvas.getByText(
        "Revocaste la delegación, pero el agente ya había comprometido el pago: esa compra sigue en curso.",
      ),
    ).toBeVisible()
    await expect(canvas.queryByText("No se ejecutó")).toBeNull()
  },
}

/** Kill switch del operador antes de despachar: D3 no se aplicó y el uso único se liberó. */
export const KillSwitch: Story = {
  args: { trace: HNP_TRACES.killSwitch },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement).slice(8)).toEqual([
      "La política autorizó el despacho; se detuvo por precaución",
      "Delegación revocada",
      "Uso único liberado",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
    await expect(
      canvas.getByText(
        `El operador detuvo las compras delegadas antes de que el agente comprometiera el pago. ${NO_CHARGE}`,
      ),
    ).toBeVisible()
    await expect(canvas.getByText(/^Se liberaron \$\s?1\.924\.000(,00)? COP\. No se hizo/)).toBeVisible()
  },
}

const ACTIVATED = stepOf(HNP_TRACES.killSwitch, "task_activated")

/** La delegación venció antes de comprar: no hubo checkout ni cobro. */
export const Expired: Story = {
  args: {
    trace: {
      ...HNP_TRACES.killSwitch,
      total: null,
      steps: [
        stepOf(HNP_TRACES.killSwitch, "delegation_consented"),
        ACTIVATED,
        {
          id: "canceled",
          at: "2026-10-22T23:00:00.000Z",
          duration_ms: null,
          actor: "agent",
          state: "done",
          kind: "canceled",
          by: "expiry",
        },
      ],
    },
  },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement).slice(2, 4)).toEqual([
      "La delegación venció",
      "Revisar la política de compra",
    ])
    await expect(canvas.getByText(`Venció antes de que el agente comprara. ${NO_CHARGE}`)).toBeVisible()
  },
}

/** El servicio retomó la tarea: el segundo claim se lee como reanudación. */
export const Resumed: Story = {
  args: {
    trace: {
      ...HNP_TRACES.revokedInFlight,
      steps: [...HNP_TRACES.revokedInFlight.steps.slice(0, 2), { ...ACTIVATED, id: "task_activated:2" }],
    },
  },
  play: async ({ canvasElement }) => {
    await expect(titles(canvasElement)).toEqual([
      "Delegación autorizada",
      "Compra delegada en marcha",
      "Compra delegada reanudada",
    ])
  },
}

/** K15: D1 permitió pero falta su resultado; la compra espera, por confirmar y sin cobro. */
export const WaitingForPolicy: Story = {
  args: { trace: HNP_TRACES.waitingForPolicy },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement).at(-1)).toBe(
      "La compra espera la recuperación de la política de compra",
    )
    await expect(canvas.getByText(`La delegación quedó cerrada. ${NO_CHARGE}`)).toBeVisible()
    await expect(canvas.queryByText("No se ejecutó")).toBeNull()
  },
}

/**
 * #93 y CT13 en B0-HNP: el comercio se negó a crear el checkout porque el producto se agotó. La
 * compra conserva su código y la tarea, que terminó `checkout_failed`, dice la misma causa: el
 * backend solo la manda cuando el comercio rechazó la creación y no quedó ningún checkout.
 */
export const CheckoutRefused: Story = {
  args: { trace: HNP_TRACES.checkoutRefused },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement).slice(2)).toEqual([
      "La compra no se pudo completar",
      "La compra delegada se detuvo",
      "Política desactivada",
      "Crear el checkout",
      ...NOT_REACHED,
    ])
    const refused = `El producto se agotó antes de completar la compra. ${NO_CHARGE}`
    await expect(rows(canvasElement)[2]).toHaveTextContent(refused)
    await expect(rows(canvasElement)[3]).toHaveTextContent(refused)
    await expect(canvas.queryByText(new RegExp(PRECAUTION))).toBeNull()
  },
}

/** CT13 en B2-HNP: D1 permitió y el comercio se negó a crear el checkout porque la oferta venció. */
export const CheckoutRefusedOfferExpired: Story = {
  args: { trace: HNP_TRACES.checkoutRefusedOfferExpired },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement)).toEqual([
      "Delegación autorizada",
      "Compra delegada en marcha",
      "Política de compra: permitida",
      "La compra no se pudo completar",
      "La compra delegada se detuvo",
      "Crear el checkout",
      ...NOT_REACHED,
    ])
    const refused = `La oferta del comercio venció. ${NO_CHARGE}`
    await expect(rows(canvasElement)[3]).toHaveTextContent(refused)
    await expect(rows(canvasElement)[4]).toHaveTextContent(refused)
    await expect(canvas.queryByText(new RegExp(PRECAUTION))).toBeNull()
  },
}

/**
 * El comercio creó el checkout y rechazó el update con `out_of_stock`. La compra guarda ese código,
 * pero la tarea no trae causa: `checkout_failed` también sale con el checkout ya creado, así que
 * conserva el texto de precaución.
 */
export const UpdateRefusedWithoutCause: Story = {
  args: { trace: HNP_TRACES.updateRefused },
  play: async ({ canvasElement }) => {
    await expect(titles(canvasElement)).toEqual([
      "Delegación autorizada",
      "Compra delegada en marcha",
      "Política desactivada",
      "Checkout creado en el comercio",
      "La compra no se pudo completar",
      "La compra delegada se detuvo",
      ...NOT_REACHED,
    ])
    await expect(rows(canvasElement)[4]).toHaveTextContent(
      `El producto se agotó antes de completar la compra. ${NO_CHARGE}`,
    )
    await expect(rows(canvasElement)[5]).toHaveTextContent(`${PRECAUTION} ${NO_CHARGE}`)
  },
}

/** Una causa fuera de `checkout_failed` no se lee: la regla es solo para el rechazo de la creación. */
export const RefusalOnlyExplainsCheckoutFailed: Story = {
  args: {
    trace: {
      ...HNP_TRACES.checkoutRefused,
      steps: HNP_TRACES.checkoutRefused.steps.map((step) =>
        step.id === "failed:task" ? { ...step, code: "state_conflict" } : step,
      ),
    },
  },
  play: async ({ canvasElement }) => {
    await expect(rows(canvasElement)[3]).toHaveTextContent(`${PRECAUTION} ${NO_CHARGE}`)
  },
}

const HAPPY = HNP_TRACES.enforceCompleted.steps
const RESERVED = stepOf(HNP_TRACES.enforceCompleted, "reservation")

/**
 * El cierre con la Agent Key falló con el checkout creado y el uso único reservado: la tarea termina
 * `checkout_failed`, la reserva se libera y la traza no culpa al comercio ni a la política.
 */
export const FailedAfterCheckout: Story = {
  args: {
    trace: {
      ...HNP_TRACES.enforceCompleted,
      status: "failed",
      steps: [
        ...HAPPY.slice(0, HAPPY.indexOf(RESERVED) + 1),
        { ...RESERVED, id: "reservation:2", at: "2026-10-21T15:00:03.281Z", outcome: "released" },
        {
          id: "failed",
          at: "2026-10-21T15:00:03.281Z",
          duration_ms: null,
          actor: "agent",
          state: "error",
          kind: "failed",
          code: "checkout_failed",
        },
      ],
    },
  },
  play: async ({ canvas, canvasElement }) => {
    await expect(titles(canvasElement).slice(3)).toEqual([
      "Checkout creado en el comercio",
      "Oferta vigente al cerrar",
      "Autorización delegada verificada",
      "Uso único reservado",
      "Uso único liberado",
      "La compra no se pudo completar",
      "Cerrar la autorización",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
    await expect(rows(canvasElement)[8]).toHaveTextContent(`${PRECAUTION} ${NO_CHARGE}`)
    await expect(canvas.queryByText(/no pudo crear el checkout|no estaba disponible/)).toBeNull()
  },
}

const PHONE = 288

/** Ningún elemento de la columna sobresale de ella. */
async function expectFitsColumn(canvasElement: HTMLElement) {
  const column = canvasElement.querySelector<HTMLElement>("[data-testid='phone-column']")
  await expect(column?.scrollWidth).toBeLessThanOrEqual(PHONE)
  const right = (column?.getBoundingClientRect().right ?? 0) + 1
  for (const element of column?.querySelectorAll<HTMLElement>("*") ?? []) {
    if (element.classList.contains("sr-only")) continue
    await expect(element.getBoundingClientRect().right).toBeLessThanOrEqual(right)
  }
}

const SUMMARIES: [PurchaseTrace, string][] = [
  [HNP_TRACES.waitingForPolicy, "Por confirmar: recuperación pendiente"],
  [HNP_TRACES.d2Denied, "Bloqueada por la política de compra"],
  [HNP_TRACES.killSwitch, "Compra cancelada"],
  [HNP_TRACES.enforceReviewNotConfirmed, "Detenida: elección sin confirmar"],
]

/**
 * Teléfono de 320 px: el hilo deja 288 px. Cada compra delegada, abierta, cabe sin desbordar, con
 * sus textos más largos (la revocación en vuelo, los escalamientos) en varias líneas.
 */
export const Phone288: Story = {
  render: () => (
    <div className="w-[288px] space-y-1 px-2" data-testid="phone-column">
      {Object.values(HNP_TRACES).map((trace) => (
        <PurchaseActivity key={trace.run_id} trace={trace} defaultOpen announce={false} />
      ))}
    </div>
  ),
  play: async ({ canvas, canvasElement }) => {
    for (const [trace, summary] of SUMMARIES) {
      // Dos compras pueden compartir estado (`canceled`): cuenta la que trae ese resumen.
      const activities = canvasElement.querySelectorAll(`[data-status='${trace.status}']`)
      const texts = Array.from(activities, (activity) => activity.textContent ?? "")
      await expect(texts.some((text) => text.startsWith(summary)), summary).toBe(true)
    }
    await expect(canvas.getAllByText("Delegación revocada")).toHaveLength(2)
    await expectFitsColumn(canvasElement)
  },
}
