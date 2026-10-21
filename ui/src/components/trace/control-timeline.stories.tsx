import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { ControlTimeline } from "@/components/trace/control-timeline"
import type { TraceStep } from "@/lib/trace-contract"
import { stepOf, TRACES } from "@/storybook/trace-fixtures"

/** Filas de primer nivel: las reglas de la política forman una lista anidada. */
function timelineRows(canvasElement: HTMLElement) {
  const timeline = canvasElement.querySelector("ol[aria-label='Controles de la compra']")
  return Array.from(timeline?.querySelectorAll(":scope > li") ?? [])
}

const meta = {
  title: "Trace/ControlTimeline",
  component: ControlTimeline,
  tags: ["autodocs"],
  args: { trace: TRACES.completedShadow },
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

/** Modo observando: todos los controles, sin filas omitidas. */
export const Completed: Story = {
  play: async ({ canvas, canvasElement }) => {
    await expect(timelineRows(canvasElement)).toHaveLength(TRACES.completedShadow.steps.length)
    await expect(canvas.getByText("Orden confirmada")).toBeVisible()
    await expect(canvas.queryByText("No se ejecutó")).toBeNull()
    // El checkout nombra el comercio; su origen no aparece.
    await expect(canvas.getByText("Synthetic Commerce Merchant")).toBeVisible()
    await expect(canvas.queryByText(/127\.0\.0\.1/)).toBeNull()
  },
}

/** Modo activo y la política permitió seguir. */
export const CompletedEnforce: Story = {
  args: { trace: TRACES.completedEnforce },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Activa: detiene la compra si no cumple")).toBeVisible()
    await expect(canvas.getByText("Política de compra: permitida")).toBeVisible()
  },
}

/** Tras el bloqueo se listan los controles que no ocurrieron: ni checkout ni cobro. */
export const Blocked: Story = {
  args: { trace: TRACES.blockedMerchant },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("La política detuvo la compra")).toBeVisible()
    // La regla de comercios nombra el comercio y conserva el origen que evaluó.
    await expect(canvas.getByText("Synthetic Commerce Merchant")).toBeVisible()
    await expect(canvas.getByText("http://127.0.0.1:4120")).toBeVisible()
    for (const control of ["Crear el checkout", "Autorizar el pago", "Confirmar la orden"]) {
      const row = canvas.getByText(control).closest("li")
      await expect(row).toHaveAttribute("data-state", "skipped")
    }
  },
}

export const BlockedProduct: Story = {
  args: { trace: TRACES.blockedProduct },
}

/**
 * S02 con `b2-hp-v2`: las tres reglas de D1 en la tabla y la de presupuesto con sus montos. La
 * compra se detiene antes del checkout, así que no hay compra cancelada ni recuperación.
 */
export const BlockedByBudget: Story = {
  args: { trace: TRACES.blockedBudget },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("La política detuvo la compra")).toBeVisible()
    const rules = canvas.getByRole("list", { name: "Reglas de la política de compra" })
    await expect(rules.querySelectorAll(":scope > li")).toHaveLength(3)
    await expect(canvas.getByText("Precio dentro de tu presupuesto")).toBeVisible()
    await expect(canvas.getByText(/^\$\s?1\.540\.000(,00)? COP$/)).toBeVisible()
    await expect(canvas.getByText(/^\$\s?1\.000\.000(,00)? COP$/)).toBeVisible()
    await expect(canvas.queryByText("Compra cancelada: superaba tu presupuesto")).toBeNull()
    const checkout = canvas.getByText("Crear el checkout").closest("li")
    await expect(checkout).toHaveAttribute("data-state", "skipped")
  },
}

export const PolicyUnavailable: Story = {
  args: { trace: TRACES.policyUnavailable },
}

export const InProgress: Story = {
  args: { trace: TRACES.inProgress },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Autorizando el pago…")).toBeVisible()
    await expect(canvas.queryByText("Confirmar la orden")).toBeNull()
  },
}

export const AwaitingApproval: Story = {
  args: { trace: TRACES.awaitingApproval },
}

/** Sin política: el control figura como desactivado entre la elección y el checkout. */
export const PolicyOff: Story = {
  args: { trace: TRACES.policyOff },
  play: async ({ canvasElement }) => {
    const rows = timelineRows(canvasElement).map((row) => row.textContent ?? "")
    const policy = rows.findIndex((text) => text.includes("Política desactivada"))
    await expect(rows[policy - 1]).toContain("Producto elegido")
    await expect(rows[policy + 1]).toContain("Checkout creado")
  },
}

/** Sin política y recién recibido el pedido: la fila desactivada espera a que haya elección. */
export const PolicyOffBeforeSelection: Story = {
  args: {
    trace: {
      ...TRACES.policyOff,
      status: "in_progress",
      total: null,
      steps: TRACES.policyOff.steps.filter((step) => step.kind === "request"),
    },
  },
  play: async ({ canvas, canvasElement }) => {
    await expect(timelineRows(canvasElement)).toHaveLength(1)
    await expect(canvas.queryByText("Política desactivada")).toBeNull()
  },
}

const POLICY_OFF_REQUEST = stepOf(TRACES.policyOff, "request")
const POLICY_OFF_SELECTION = stepOf(TRACES.policyOff, "selection")

/** `failed` como lo escribe el backend cuando el checkout no se pudo crear: lo registra el agente. */
const CHECKOUT_NOT_CREATED: TraceStep = {
  id: "failed",
  at: "2026-10-20T16:33:31.020Z",
  duration_ms: null,
  actor: "agent",
  state: "error",
  kind: "failed",
  code: "purchase_failed",
}

/**
 * Sin política, el checkout no se pudo crear (la etapa pasa a `failed` después de elegir). La fila
 * desactivada va justo después de la elección, antes del fallo: así ocurrió.
 */
export const PolicyOffCheckoutFailed: Story = {
  args: {
    trace: {
      ...TRACES.policyOff,
      status: "failed",
      total: null,
      steps: [POLICY_OFF_REQUEST, POLICY_OFF_SELECTION, CHECKOUT_NOT_CREATED],
    },
  },
  play: async ({ canvasElement }) => {
    const titles = timelineRows(canvasElement).map((row) => row.querySelector("p")?.textContent)
    await expect(titles).toEqual([
      "Pedido recibido",
      "Producto elegido",
      "Política desactivada",
      "La compra no se pudo completar",
      "Crear el checkout",
      "Tu aprobación",
      "Firmar la autorización",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
  },
}

/**
 * Sin política y sin producto elegido: la compra terminó antes. Entre lo que no ocurrió, la política
 * figura como desactivada (no como un control pendiente) y después de la elección.
 */
export const PolicyOffEndedBeforeSelection: Story = {
  args: {
    trace: {
      ...TRACES.policyOff,
      status: "failed",
      total: null,
      steps: [POLICY_OFF_REQUEST, { ...CHECKOUT_NOT_CREATED, at: "2026-10-20T16:33:29.130Z" }],
    },
  },
  play: async ({ canvas, canvasElement }) => {
    const titles = timelineRows(canvasElement).map((row) => row.querySelector("p")?.textContent)
    await expect(titles.slice(0, 4)).toEqual([
      "Pedido recibido",
      "La compra no se pudo completar",
      "Elegir el producto",
      "Política desactivada",
    ])
    await expect(canvas.queryByText("Revisar la política de compra")).toBeNull()
  },
}

/** Teléfono de 320 px: la regla que falló, con la URL entera y sin desbordar la columna. */
export const Phone320: Story = {
  args: { trace: TRACES.blockedMerchant },
  decorators: [
    (Story) => (
      <div className="w-[288px]">
        <Story />
      </div>
    ),
  ],
  play: async ({ canvas, canvasElement }) => {
    const value = canvas.getByText("https://tienda-aliada.example.test")
    await expect(value).toBeVisible()
    await expect(value.scrollWidth).toBeLessThanOrEqual(value.clientWidth)
    const timeline = canvasElement.querySelector("ol")
    await expect(timeline?.scrollWidth).toBeLessThanOrEqual(288)
  },
}

export const Canceled: Story = {
  args: { trace: TRACES.canceled },
}

export const Failed: Story = {
  args: { trace: TRACES.failed },
}

/**
 * S04: la política permitió la compra; la frescura confirmó los términos al mostrarlos y detuvo la
 * aprobación cuando el precio cambió. Después: ni firma, ni pago, ni orden.
 */
export const StaleTerms: Story = {
  args: { trace: TRACES.staleTerms },
  play: async ({ canvas, canvasElement }) => {
    const rows = timelineRows(canvasElement)
    const titles = rows.map((row) => row.querySelector("p")?.textContent)
    await expect(titles).toEqual([
      "Pedido recibido",
      "Producto elegido",
      "Política de compra: permitida",
      "Checkout creado en el comercio",
      "Condiciones confirmadas con el comercio",
      "Tu aprobación",
      "Las condiciones del comercio cambiaron",
      "Firmar la autorización",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
    await expect(rows.map((row) => row.getAttribute("data-state"))).toEqual([
      "done",
      "done",
      "done",
      "done",
      "done",
      "skipped",
      "blocked",
      "skipped",
      "skipped",
      "skipped",
    ])
    await expect(canvas.getByRole("list", { name: "Qué cambió" })).toBeVisible()
    // El único bloqueo es de un control de la aplicación: nada lo atribuye a la política.
    await expect(canvas.queryByText("La política detuvo la compra")).toBeNull()
  },
}

/**
 * S04 en el chat de la demo: la aprobación nunca se envió. Al pulsar «Aprobar», releer los términos
 * los rechazó; la fila de frescura bloqueada es de la etapa `present`, después de la confirmada.
 */
export const StaleTermsOnReread: Story = {
  args: { trace: TRACES.staleTermsReread },
  play: async ({ canvas, canvasElement }) => {
    const rows = timelineRows(canvasElement)
    const titles = rows.map((row) => row.querySelector("p")?.textContent)
    await expect(titles.slice(4)).toEqual([
      "Condiciones confirmadas con el comercio",
      "Tu aprobación",
      "Las condiciones del comercio cambiaron",
      "Firmar la autorización",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
    await expect(rows[4]).toHaveTextContent("Antes de mostrarte los términos")
    await expect(rows[5]).toHaveTextContent("No se ejecutó")
    await expect(rows[6]).toHaveAttribute("data-state", "blocked")
    await expect(rows[6]).toHaveTextContent(
      "No se registró ninguna aprobación ni se firmó ninguna autorización.",
    )
    await expect(canvas.queryByText(/No se mostraron/)).toBeNull()
    await expect(canvas.queryByText("La política detuvo la compra")).toBeNull()
  },
}

/** S02: el checkout se canceló por presupuesto y el agente explicó por qué, sin error técnico. */
export const GovernedRecovery: Story = {
  args: { trace: TRACES.governedRecovery },
  play: async ({ canvas, canvasElement }) => {
    const titles = timelineRows(canvasElement).map((row) => row.querySelector("p")?.textContent)
    const recovery = titles.indexOf("El agente explicó por qué no siguió")
    await expect(titles[recovery - 1]).toBe("Compra cancelada: superaba tu presupuesto")
    await expect(titles.slice(recovery + 1)).toEqual([
      "Tu aprobación",
      "Firmar la autorización",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
    await expect(canvas.queryByText(/error/i)).toBeNull()
  },
}

/**
 * S01 con `b2-hp-v2`: al aprobar, la política revisa la compra otra vez (D2) y después se firma. El
 * paso va entre «Aprobaste la compra» y «Autorización firmada».
 */
export const AuthorizedOnApprove: Story = {
  args: { trace: TRACES.authorizedEnforce },
  play: async ({ canvas, canvasElement }) => {
    const titles = timelineRows(canvasElement).map((row) => row.querySelector("p")?.textContent)
    const verified = titles.indexOf("Autorización verificada")
    await expect(titles[verified - 1]).toBe("Aprobaste la compra")
    await expect(titles[verified + 1]).toBe("Autorización firmada")
    await expect(canvas.getByText("Antes de firmar la autorización")).toBeVisible()
    await expect(canvas.queryByText("No se ejecutó")).toBeNull()
  },
}

/**
 * La política no autorizó la aprobación (POL-06): la regla que falló con sus valores, nada firmado
 * y los controles que ya no ocurrieron. No es la frescura: las condiciones se confirmaron.
 */
export const AuthorizationDenied: Story = {
  args: { trace: TRACES.authorizationDenied },
  play: async ({ canvas, canvasElement }) => {
    const rows = timelineRows(canvasElement)
    const titles = rows.map((row) => row.querySelector("p")?.textContent)
    await expect(titles.slice(4)).toEqual([
      "Condiciones confirmadas con el comercio",
      "Tu aprobación",
      "Condiciones confirmadas con el comercio",
      "La política no autorizó la compra",
      "Firmar la autorización",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
    const denied = rows[7]
    await expect(denied).toHaveAttribute("data-state", "blocked")
    await expect(denied).toHaveTextContent(
      "No se firmó ninguna autorización ni se hizo ningún cobro.",
    )
    await expect(canvas.getByText("La política no cambió desde que eligió el producto")).toBeVisible()
    await expect(canvas.getByText("b2-hp-v2 · revisión 2")).toBeVisible()
    await expect(rows[5]).toHaveTextContent("No se ejecutó")
  },
}

/** No se sabe si la orden se creó: no se agregan controles omitidos. */
export const Uncertain: Story = {
  args: { trace: TRACES.uncertain },
  play: async ({ canvas }) => {
    await expect(canvas.queryByText("No se ejecutó")).toBeNull()
  },
}
