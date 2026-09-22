import type { Meta, StoryObj } from "@storybook/react-vite"
import { useEffect, useRef, useState } from "react"
import { expect, fn, waitFor } from "storybook/test"

import {
  PurchaseApprovalCard,
  PurchaseApprovalCardSkeleton,
  type PurchaseApprovalCardProps,
} from "@/components/agent/purchase-approval-card"
import {
  ESTUDIO,
  ORDER,
  SONORA,
  SONORA_LONG,
  TERMS_MULTIPLE_LINES,
  TERMS_NO_ADDRESS,
  TERMS_PRICE_CHANGED,
} from "@/storybook/agent-fixtures"
import { TERMS } from "@/storybook/fixtures"

type CardState = Pick<PurchaseApprovalCardProps, "status" | "approvalRecorded" | "error" | "order">

/**
 * Simula la secuencia REST con temporizadores locales: aprobar pasa por "approving" y
 * "completing"; cancelar por "canceling". En la app los estados vienen del flujo real.
 */
function InteractiveApprovalCard(props: PurchaseApprovalCardProps) {
  const [state, setState] = useState<CardState>({
    status: props.status,
    approvalRecorded: props.approvalRecorded,
    error: props.error,
    order: props.order,
  })
  const timers = useRef<number[]>([])
  useEffect(() => () => timers.current.forEach((timer) => clearTimeout(timer)), [])

  const later = (ms: number, next: Partial<CardState>) => {
    timers.current.push(window.setTimeout(() => setState((current) => ({ ...current, ...next })), ms))
  }

  return (
    <PurchaseApprovalCard
      {...props}
      {...state}
      onApprove={() => {
        props.onApprove?.()
        const start = state.approvalRecorded ? 0 : 700
        if (!state.approvalRecorded) setState((current) => ({ ...current, status: "approving" }))
        later(start, { status: "completing", approvalRecorded: true, error: null })
        later(start + 900, { status: "completed", order: ORDER })
      }}
      onCancel={() => {
        props.onCancel?.()
        setState((current) => ({ ...current, status: "canceling" }))
        later(600, { status: "canceled" })
      }}
      onNewPurchase={() => {
        props.onNewPurchase?.()
        setState({ status: "awaiting_approval", approvalRecorded: false, error: null, order: null })
      }}
    />
  )
}

const meta = {
  title: "Agent/PurchaseApprovalCard",
  component: PurchaseApprovalCard,
  tags: ["autodocs"],
  args: {
    terms: TERMS,
    product: SONORA,
    status: "awaiting_approval",
    onApprove: fn(),
    onCancel: fn(),
    onNewPurchase: fn(),
  },
  render: (args) => <InteractiveApprovalCard key={JSON.stringify(args)} {...args} />,
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof PurchaseApprovalCard>

export default meta
type Story = StoryObj<typeof meta>

/** Aprobar recorre los estados con temporizadores locales hasta "completed". */
export const AwaitingApproval: Story = {
  play: async ({ args, canvas, userEvent }) => {
    // Las filas etiqueta/valor son un <dl> real (el recibo anterior dejaba dt/dd sueltos).
    await expect(canvas.getAllByRole("term").length).toBeGreaterThan(0)
    await userEvent.click(canvas.getByRole("button", { name: "Aprobar y comprar" }))
    await expect(args.onApprove).toHaveBeenCalledOnce()
    await waitFor(() => expect(canvas.getByText("Compra completada")).toBeVisible(), {
      timeout: 4000,
    })
  },
}

export const Approving: Story = {
  args: { status: "approving" },
}

export const Completing: Story = {
  args: { status: "completing", approvalRecorded: true },
}

/** La aprobación quedó registrada pero completar falló: solo se puede reintentar. */
export const RetryAfterRecordedApproval: Story = {
  args: {
    status: "error",
    approvalRecorded: true,
    error: "El comercio no pudo procesar la solicitud. La aprobación sigue vigente.",
  },
  play: async ({ canvas }) => {
    await expect(canvas.queryByRole("button", { name: /Cancelar/ })).toBeNull()
    await expect(canvas.getByRole("button", { name: /Reintentar compra/ })).toBeEnabled()
  },
}

export const ApprovalError: Story = {
  args: {
    status: "error",
    error: "Los términos cambiaron. Vuelve a revisarlos antes de aprobar.",
  },
}

export const Canceling: Story = {
  args: { status: "canceling" },
}

export const Canceled: Story = {
  args: { status: "canceled" },
}

export const Completed: Story = {
  args: { status: "completed", approvalRecorded: true, order: ORDER },
  play: async ({ canvas }) => {
    const link = canvas.getByRole("link", { name: /Ver orden/ })
    await expect(link).toHaveAttribute("rel", "noopener noreferrer")
  },
}

export const CompletedWithoutOrderLink: Story = {
  args: {
    status: "completed",
    approvalRecorded: true,
    order: { id: "ord_storybook_2", order_number: null, status: null },
  },
}

export const PriceChanged: Story = {
  args: { terms: TERMS_PRICE_CHANGED, priceChanged: true },
}

export const MultipleLines: Story = {
  args: { terms: TERMS_MULTIPLE_LINES },
}

export const LongProductName: Story = {
  args: { product: SONORA_LONG },
}

/** Sin `ProductCard` se usa el nombre de la primera línea y el placeholder. */
export const WithoutProduct: Story = {
  args: { product: null },
}

export const ProductWithoutImage: Story = {
  args: { product: ESTUDIO },
}

export const WithoutAddress: Story = {
  args: { terms: TERMS_NO_ADDRESS },
}

export const Loading: Story = {
  render: () => <PurchaseApprovalCardSkeleton />,
}

export const Mobile: Story = {
  globals: { viewport: { value: "mobile", isRotated: false } },
}

export const MobileCompleted: Story = {
  args: { status: "completed", approvalRecorded: true, order: ORDER },
  globals: { viewport: { value: "mobile", isRotated: false } },
}
