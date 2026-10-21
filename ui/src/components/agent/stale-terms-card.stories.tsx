import type { Meta, StoryObj } from "@storybook/react-vite"
import { useState } from "react"
import { expect, fn, waitFor } from "storybook/test"

import {
  StaleTermsCard,
  type StaleTermsCardProps,
  type StaleTermsCardStatus,
} from "@/components/agent/stale-terms-card"
import { SONORA } from "@/storybook/agent-fixtures"

const NO_CHARGE = "No se hizo ningún cobro."

/** Simula cancelar con un temporizador local; en la app el estado viene de la llamada REST. */
function InteractiveStaleTermsCard(props: StaleTermsCardProps & { failFirst?: boolean }) {
  const { failFirst, ...card } = props
  const [status, setStatus] = useState<StaleTermsCardStatus>(card.status)
  const [error, setError] = useState<string | null>(null)
  const [attempts, setAttempts] = useState(0)

  const onCancel = () => {
    card.onCancel?.()
    setStatus("canceling")
    setError(null)
    setAttempts((current) => current + 1)
    setTimeout(() => {
      if (failFirst && attempts === 0) {
        setStatus("stale")
        setError("No pude comunicarme con el agente.")
      } else {
        setStatus("canceled")
      }
    }, 300)
  }

  return <StaleTermsCard {...card} status={status} error={error} onCancel={onCancel} />
}

const meta = {
  title: "Agent/StaleTermsCard",
  component: StaleTermsCard,
  tags: ["autodocs"],
  args: {
    terms: "changed",
    status: "stale",
    productTitle: SONORA.title,
    onCancel: fn(),
  },
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof StaleTermsCard>

export default meta
type Story = StoryObj<typeof meta>

/** Aprobar respondió 409 `checkout_terms_changed`, o la compra pendiente llegó con `terms_status: "changed"`. */
export const TermsChanged: Story = {
  play: async ({ canvas }) => {
    await expect(
      canvas.getByRole("heading", { name: "Las condiciones del comercio cambiaron" }),
    ).toBeVisible()
    await expect(canvas.getByText(NO_CHARGE)).toBeVisible()
    await expect(canvas.getByText(SONORA.title)).toBeVisible()
    // Solo una salida: cancelar. No hay aprobar ni «recarga la página».
    const buttons = canvas.getAllByRole("button")
    await expect(buttons).toHaveLength(1)
    await expect(buttons[0]).toHaveAccessibleName("Cancelar compra")
    await expect(canvas.queryByText(/recarga/i)).toBeNull()
  },
}

/** Aprobar respondió 503 `offer_freshness_unavailable`, o `terms_status: "unavailable"`. */
export const TermsUnavailable: Story = {
  args: { terms: "unavailable" },
  play: async ({ canvas }) => {
    await expect(
      canvas.getByRole("heading", { name: "No pude confirmar las condiciones con el comercio" }),
    ).toBeVisible()
    await expect(canvas.getByText(NO_CHARGE)).toBeVisible()
    await expect(canvas.getByRole("button", { name: "Cancelar compra" })).toBeEnabled()
  },
}

/** Compra restaurada tras recargar: `GET /me/pending-purchase` no trae el producto. */
export const RestoredWithoutProduct: Story = {
  args: { productTitle: null },
  play: async ({ canvas }) => {
    await expect(canvas.queryByText(SONORA.title)).toBeNull()
    await expect(canvas.getByRole("button", { name: "Cancelar compra" })).toBeVisible()
  },
}

export const Canceling: Story = {
  args: { status: "canceling" },
  play: async ({ args, canvas, userEvent }) => {
    const button = canvas.getByRole("button", { name: "Cancelando…" })
    // `aria-disabled` y no `disabled`: el botón que se pulsó conserva el foco mientras cancela.
    await expect(button).toHaveAttribute("aria-disabled", "true")
    button.focus()
    await userEvent.keyboard("{Enter}")
    await expect(args.onCancel).not.toHaveBeenCalled()
    await expect(canvas.getByRole("status")).toHaveTextContent("Cancelando la compra…")
  },
}

/** Después de cancelar no quedan botones: el comprador pide la compra de nuevo en el chat. */
export const Canceled: Story = {
  args: { status: "canceled" },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("heading", { name: "Compra cancelada" })).toBeVisible()
    await expect(canvas.getByText("Ya puedes pedirla de nuevo en el chat.")).toBeVisible()
    await expect(canvas.getByText(NO_CHARGE)).toBeVisible()
    await expect(canvas.queryByRole("button")).toBeNull()
  },
}

/** Cancelar falló: se muestra el error y el mismo botón permite reintentar. */
export const CancelFailed: Story = {
  args: { error: "No pude comunicarme con el agente." },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("alert")).toHaveTextContent("No pude comunicarme con el agente.")
    await expect(canvas.getByRole("button", { name: "Cancelar compra" })).toBeEnabled()
  },
}

/**
 * Cancelar con el teclado: un error, el reintento y la compra cancelada. El error lo anuncia su
 * aviso y el botón conserva el foco para reintentar; cancelada, el botón ya no está y el foco pasa al
 * título, que el lector lee al llegar (la región de estado no lo repite).
 */
export const CancelAndRetry: Story = {
  render: (args) => <InteractiveStaleTermsCard {...args} failFirst />,
  play: async ({ args, canvas, userEvent }) => {
    const cancel = canvas.getByRole("button", { name: "Cancelar compra" })
    await userEvent.tab()
    await expect(cancel).toHaveFocus()
    await userEvent.keyboard("{Enter}")
    await expect(await canvas.findByRole("alert")).toHaveTextContent("No pude comunicarme")
    await expect(cancel).toHaveFocus()
    await expect(canvas.getByRole("status")).toBeEmptyDOMElement()
    await userEvent.keyboard("{Enter}")
    const title = await canvas.findByRole("heading", { name: "Compra cancelada" })
    await waitFor(() => expect(title).toHaveFocus())
    await expect(canvas.getByRole("status")).toBeEmptyDOMElement()
    await expect(canvas.queryByRole("alert")).toBeNull()
    await expect(args.onCancel).toHaveBeenCalledTimes(2)
  },
}

/** Cancelada mientras la persona estaba en otro control: no se le quita el foco y se anuncia. */
export const CanceledWhileFocusElsewhere: Story = {
  render: (args) => (
    <div className="space-y-3">
      <InteractiveStaleTermsCard {...args} />
      <button type="button">Otro control</button>
    </div>
  ),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Cancelar compra" }))
    const elsewhere = canvas.getByRole("button", { name: "Otro control" })
    await userEvent.click(elsewhere)
    await waitFor(() =>
      expect(canvas.getByRole("status")).toHaveTextContent(`Compra cancelada. ${NO_CHARGE}`),
    )
    await expect(elsewhere).toHaveFocus()
  },
}

/** Aprobar respondió que los términos cambiaron: esta tarjeta reemplaza a la de aprobación. */
function ReplacesApproval(props: StaleTermsCardProps) {
  const [stale, setStale] = useState(false)
  return stale ? (
    <StaleTermsCard {...props} autoFocus />
  ) : (
    <button type="button" onClick={() => setStale(true)}>
      Aprobar y comprar
    </button>
  )
}

/** El botón pulsado desaparece con la tarjeta de aprobación: el foco pasa al título de esta. */
export const ReplacesApprovalCard: Story = {
  render: (args) => <ReplacesApproval {...args} />,
  play: async ({ canvas, userEvent }) => {
    await userEvent.tab()
    await userEvent.keyboard("{Enter}")
    const title = await canvas.findByRole("heading", {
      name: "Las condiciones del comercio cambiaron",
    })
    await expect(title).toHaveFocus()
    await userEvent.tab()
    await expect(canvas.getByRole("button", { name: "Cancelar compra" })).toHaveFocus()
  },
}

/** A 320 px el título largo y el botón caben sin desbordar. */
export const Phone320: Story = {
  args: { terms: "unavailable" },
  decorators: [
    (Story) => (
      <div className="w-[288px]">
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement }) => {
    const card = canvasElement.querySelector<HTMLElement>("[data-testid='stale-terms-card']")
    await expect(card?.scrollWidth).toBeLessThanOrEqual(card?.clientWidth ?? 0)
  },
}
