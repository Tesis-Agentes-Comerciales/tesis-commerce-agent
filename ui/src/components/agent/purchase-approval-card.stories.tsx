import type { Meta, StoryObj } from "@storybook/react-vite"
import { useEffect, useRef, useState } from "react"
import { expect, fn, waitFor } from "storybook/test"

import { formatMoneyParts } from "@/components/agent/agent-format"
import { expectFitsPhoneColumn, PHONE_COLUMN } from "@/components/delegation/story-helpers"
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
  TERMS_MILLIONS,
  TERMS_MULTIPLE_LINES,
  TERMS_NO_ADDRESS,
  TERMS_PRICE_CHANGED,
} from "@/storybook/agent-fixtures"
import { TERMS } from "@/storybook/fixtures"
import { mouseClick } from "@/storybook/interactions"
import { stepOf, TRACES } from "@/storybook/trace-fixtures"

type CardState = Pick<
  PurchaseApprovalCardProps,
  "status" | "approvalRecorded" | "error" | "order" | "receipt"
>

/** Recibo de la fixture completada: aprobada a las 15:42 UTC (10:42 en Bogotá), con firma. */
const RECEIPT = {
  approvedAt: stepOf(TRACES.completedShadow, "approval").approved_at ?? "",
  authorizationSigned: true,
}

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
    receipt: props.receipt,
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
        later(start, {
          status: "completing",
          approvalRecorded: true,
          error: null,
          receipt: { approvedAt: new Date().toISOString(), authorizationSigned: true },
        })
        later(start + 900, { status: "completed", order: ORDER })
      }}
      onCancel={() => {
        props.onCancel?.()
        setState((current) => ({ ...current, status: "canceling" }))
        later(600, { status: "canceled" })
      }}
      onNewPurchase={() => {
        props.onNewPurchase?.()
        setState({
          status: "awaiting_approval",
          approvalRecorded: false,
          error: null,
          order: null,
          receipt: null,
        })
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

type PlayContext = Parameters<NonNullable<Story["play"]>>[0]

const SETTLE = { timeout: 4000 }

/** El total sale solo de los términos autoritativos, nunca del catálogo. */
async function expectTermsTotal({ args, canvas }: PlayContext) {
  const total = formatMoneyParts(args.terms.total_minor, args.terms.currency)
  // Intl separa el símbolo con un espacio duro; Testing Library normaliza el texto del DOM, no el buscado.
  await expect(canvas.getByText(total.amount.replace(/\s+/g, " "))).toBeVisible()
  await expect(canvas.getByText(total.code)).toBeVisible()
}

/**
 * Mientras hay una petición en curso nada se puede pulsar dos veces. Los botones van con
 * `aria-disabled` (no `disabled`) para que el que se pulsó conserve el foco: Enter no hace nada.
 */
async function expectBusy({ args, canvas, canvasElement, userEvent }: PlayContext, live: string) {
  await expect(canvasElement.querySelector("[data-testid=purchase-approval-card]")).toHaveAttribute(
    "aria-busy",
    "true",
  )
  await expect(canvas.getByRole("status")).toHaveTextContent(live)
  for (const button of canvas.getAllByRole("button")) {
    await expect(button).toHaveAttribute("aria-disabled", "true")
    button.focus()
    await userEvent.keyboard("{Enter}")
  }
  await expect(args.onApprove).not.toHaveBeenCalled()
  await expect(args.onCancel).not.toHaveBeenCalled()
}

/** Tabula hasta el control, como lo haría alguien con teclado. */
async function tabTo({ userEvent }: PlayContext, target: HTMLElement) {
  for (let step = 0; step < 10 && document.activeElement !== target; step++) await userEvent.tab()
  await expect(target).toHaveFocus()
}

/** Aprobar recorre los estados con temporizadores locales hasta "completed". */
export const AwaitingApproval: Story = {
  play: async (context) => {
    const { args, canvas, userEvent } = context
    await expect(canvas.getByRole("heading", { name: "Aprueba la compra" })).toBeVisible()
    await expectTermsTotal(context)
    await expect(canvas.getByText(/No mueve dinero real/)).toBeVisible()
    // Las filas etiqueta/valor son un <dl> real (el recibo anterior dejaba dt/dd sueltos).
    await expect(canvas.getAllByRole("term").length).toBeGreaterThan(0)
    await userEvent.click(canvas.getByRole("button", { name: "Aprobar y comprar" }))
    await expect(args.onApprove).toHaveBeenCalledOnce()
    await waitFor(() => expect(canvas.getByText("Compra completada")).toBeVisible(), SETTLE)
    await expect(args.onCancel).not.toHaveBeenCalled()
    await expect(canvas.getByRole("link", { name: /Ver orden/ })).toBeVisible()
  },
}

/** Cancelar antes de aprobar libera el checkout y no registra ninguna aprobación. */
export const CancelBeforeApproval: Story = {
  play: async ({ args, canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Cancelar" }))
    await expect(args.onCancel).toHaveBeenCalledOnce()
    await waitFor(() => expect(canvas.getByText("Compra cancelada")).toBeVisible(), SETTLE)
    await expect(args.onApprove).not.toHaveBeenCalled()
    await expect(canvas.queryByTestId("approve-purchase")).toBeNull()
    await expect(canvas.getByText("No se registró ninguna aprobación ni cargo.")).toBeVisible()
  },
}

/**
 * Con el teclado: «Aprobar y comprar» conserva el foco mientras registra y completa; al terminar, el
 * botón ya no está y el foco pasa al título «Compra completada». El lector lo lee al llegar, así que
 * la región de estado no lo repite.
 */
export const KeyboardApproveFocus: Story = {
  play: async (context) => {
    const { canvas, userEvent } = context
    const approve = canvas.getByRole("button", { name: "Aprobar y comprar" })
    await tabTo(context, approve)
    await userEvent.keyboard("{Enter}")
    await expect(canvas.getByRole("status")).toHaveTextContent("Registrando aprobación…")
    await expect(approve).toHaveFocus()
    await waitFor(
      () => expect(canvas.getByRole("status")).toHaveTextContent("Completando compra…"),
      SETTLE,
    )
    await expect(approve).toHaveFocus()
    const title = await canvas.findByRole("heading", { name: "Compra completada" }, SETTLE)
    await expect(title).toHaveFocus()
    await expect(canvas.getByRole("status")).toBeEmptyDOMElement()
    // Desde el título, el siguiente control es el de la tarjeta.
    await userEvent.tab()
    await expect(canvas.getByRole("button", { name: "Nueva compra" })).toHaveFocus()
  },
}

/** Con el teclado: cancelar deja el foco en «Cancelar» y, cancelada, en el título. */
export const KeyboardCancelFocus: Story = {
  play: async (context) => {
    const { canvas, userEvent } = context
    const cancel = canvas.getByRole("button", { name: "Cancelar" })
    await tabTo(context, cancel)
    await userEvent.keyboard("{Enter}")
    await expect(canvas.getByRole("status")).toHaveTextContent("Cancelando la compra…")
    await expect(cancel).toHaveFocus()
    const title = await canvas.findByRole("heading", { name: "Compra cancelada" }, SETTLE)
    await expect(title).toHaveFocus()
    await expect(canvas.getByRole("status")).toBeEmptyDOMElement()
  },
}

/**
 * Con el mouse el foco no salta al título: en el chat vuelve al compositor con el turno siguiente,
 * así que quien presenta puede escribir enseguida. El resultado lo anuncia la región de estado.
 */
export const MouseApprove: Story = {
  play: async ({ canvas }) => {
    mouseClick(canvas.getByRole("button", { name: "Aprobar y comprar" }))
    const title = await canvas.findByRole("heading", { name: "Compra completada" }, SETTLE)
    await expect(title).not.toHaveFocus()
    await expect(canvas.getByRole("status")).toHaveTextContent(
      "Compra completada. El comercio registró la orden.",
    )
  },
}

/** Con el mouse: cancelada, el foco tampoco salta al título y la región de estado lo anuncia. */
export const MouseCancel: Story = {
  play: async ({ canvas }) => {
    mouseClick(canvas.getByRole("button", { name: "Cancelar" }))
    const title = await canvas.findByRole("heading", { name: "Compra cancelada" }, SETTLE)
    await expect(title).not.toHaveFocus()
    await expect(canvas.getByRole("status")).toHaveTextContent(
      "Compra cancelada. No se registró ninguna aprobación ni cargo.",
    )
  },
}

/**
 * Si la persona ya está en otro control (el compositor del chat, por ejemplo) el foco no se mueve:
 * el cambio llega por la región de estado.
 */
export const AnnouncesWhenFocusIsElsewhere: Story = {
  render: (args) => (
    <div className="space-y-3">
      <InteractiveApprovalCard {...args} />
      <button type="button">Otro control</button>
    </div>
  ),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Aprobar y comprar" }))
    const elsewhere = canvas.getByRole("button", { name: "Otro control" })
    await userEvent.click(elsewhere)
    await waitFor(
      () =>
        expect(canvas.getByRole("status")).toHaveTextContent(
          "Compra completada. El comercio registró la orden.",
        ),
      SETTLE,
    )
    await expect(elsewhere).toHaveFocus()
  },
}

export const Approving: Story = {
  args: { status: "approving" },
  play: async (context) => expectBusy(context, "Registrando aprobación…"),
}

/** La aprobación ya está registrada: cancelar desaparece y solo queda completar. */
export const Completing: Story = {
  args: { status: "completing", approvalRecorded: true },
  play: async (context) => {
    await expectBusy(context, "Completando compra…")
    await expect(context.canvas.getByRole("heading", { name: "Aprobación registrada" })).toBeVisible()
    await expect(context.canvas.queryByRole("button", { name: /Cancelar/ })).toBeNull()
  },
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

/** Sin aprobación registrada el error deja volver a aprobar o cancelar. */
export const ApprovalError: Story = {
  args: {
    status: "error",
    error: "Los términos cambiaron. Vuelve a revisarlos antes de aprobar.",
  },
  play: async ({ args, canvas }) => {
    await expect(canvas.getByRole("alert")).toHaveTextContent(args.error ?? "")
    await expect(canvas.getByRole("button", { name: "Aprobar y comprar" })).toBeEnabled()
    await expect(canvas.getByRole("button", { name: "Cancelar" })).toBeEnabled()
  },
}

export const Canceling: Story = {
  args: { status: "canceling" },
  play: async (context) => expectBusy(context, "Cancelando la compra…"),
}

/** Empezar otra compra borra la conversación, así que pide confirmación. */
export const Canceled: Story = {
  args: { status: "canceled" },
  play: async ({ args, canvas, userEvent }) => {
    await expect(canvas.queryByTestId("approve-purchase")).toBeNull()
    await userEvent.click(canvas.getByRole("button", { name: "Nueva compra" }))
    await expect(args.onNewPurchase).not.toHaveBeenCalled()
    await userEvent.click(canvas.getByRole("button", { name: "Seguir aquí" }))
    await userEvent.click(canvas.getByRole("button", { name: "Nueva compra" }))
    await userEvent.click(canvas.getByRole("button", { name: "Empezar otra" }))
    await expect(args.onNewPurchase).toHaveBeenCalledOnce()
  },
}

export const Completed: Story = {
  args: { status: "completed", approvalRecorded: true, order: ORDER },
  play: async ({ canvas }) => {
    const link = canvas.getByRole("link", { name: /Ver orden/ })
    await expect(link).toHaveAttribute("rel", "noopener noreferrer")
  },
}

/** Al decidir, la tarjeta se convierte en recibo: quién aprobó, a qué hora y con qué firma. */
export const ApprovalBecomesReceipt: Story = {
  play: async ({ canvas, userEvent }) => {
    await expect(canvas.queryByTestId("approval-receipt")).toBeNull()
    await userEvent.click(canvas.getByRole("button", { name: "Aprobar y comprar" }))
    await waitFor(() => expect(canvas.getByText("Compra completada")).toBeVisible(), SETTLE)
    await expect(canvas.getByTestId("approval-receipt")).toHaveTextContent(
      /^Aprobado por ti · \d{2}:\d{2} · autorización firmada$/,
    )
  },
}

export const CompletedWithReceipt: Story = {
  args: { status: "completed", approvalRecorded: true, order: ORDER, receipt: RECEIPT },
  play: async ({ canvas }) => {
    await expect(canvas.getByTestId("approval-receipt")).toHaveTextContent(
      /^Aprobado por ti · \d{2}:\d{2} · autorización firmada$/,
    )
  },
}

export const CompletingWithReceipt: Story = {
  args: { status: "completing", approvalRecorded: true, receipt: RECEIPT },
}

/** Sin el paso de firma en la traza, el recibo no la menciona. */
export const ReceiptWithoutSignature: Story = {
  args: {
    status: "completed",
    approvalRecorded: true,
    order: ORDER,
    receipt: { ...RECEIPT, authorizationSigned: false },
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByTestId("approval-receipt")).not.toHaveTextContent(/firmada/)
  },
}

/** Un recibo antes de aprobar sería falso: no se muestra. */
export const ReceiptHiddenBeforeApproval: Story = {
  args: { receipt: RECEIPT },
  play: async ({ canvas }) => {
    await expect(canvas.queryByTestId("approval-receipt")).toBeNull()
  },
}

export const CompletedWithoutOrderLink: Story = {
  args: {
    status: "completed",
    approvalRecorded: true,
    order: { id: "ord_storybook_2", order_number: null, status: null },
  },
}

/** El precio del checkout difiere del catálogo: se avisa y el total sigue siendo el de los términos. */
export const PriceChanged: Story = {
  args: { terms: TERMS_PRICE_CHANGED, priceChanged: true },
  play: async (context) => {
    await expect(context.canvas.getByText(/El precio cambió desde que consulté el catálogo/)).toBeVisible()
    await expectTermsTotal(context)
  },
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
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Sin dirección")).toBeVisible()
  },
}

/**
 * Teléfono de 320 px: el hilo deja 288 px y el mensaje, con su `px-2`, 272. Con un total real en
 * pesos, cada estado con botones cabe sin desbordar: el código de la moneda baja bajo el total y los
 * botones se apilan. Un título por tarjeta: dos regiones con el mismo nombre fallan axe
 * (`landmark-unique`).
 */
export const Phone320: Story = {
  args: { terms: TERMS_MILLIONS },
  render: (args) => (
    <div className={`${PHONE_COLUMN} space-y-3 px-2`} data-testid="phone-column">
      <PurchaseApprovalCard {...args} />
      <PurchaseApprovalCard {...args} status="canceling" />
      <PurchaseApprovalCard
        {...args}
        status="error"
        approvalRecorded
        error="El comercio no pudo procesar la solicitud. La aprobación sigue vigente."
        receipt={RECEIPT}
      />
      <PurchaseApprovalCard
        {...args}
        status="completed"
        approvalRecorded
        order={ORDER}
        receipt={RECEIPT}
      />
      <PurchaseApprovalCard {...args} status="canceled" />
    </div>
  ),
  play: async ({ canvas, canvasElement }) => {
    const cards = canvasElement.querySelectorAll<HTMLElement>("[data-testid=purchase-approval-card]")
    await expect(cards).toHaveLength(5)
    for (const card of cards) {
      await expect(card.scrollWidth).toBeLessThanOrEqual(card.clientWidth)
      const { left, right } = card.getBoundingClientRect()
      for (const action of card.querySelectorAll<HTMLElement>("button, a")) {
        const label = action.textContent ?? ""
        await expect(action).toBeVisible()
        // La etiqueta entera dentro de la píldora, y la píldora dentro de la tarjeta.
        await expect(action.scrollWidth, label).toBeLessThanOrEqual(action.clientWidth)
        const box = action.getBoundingClientRect()
        await expect(box.left, label).toBeGreaterThanOrEqual(left)
        await expect(box.right, label).toBeLessThanOrEqual(right)
      }
    }
    await expect(canvas.getAllByRole("button", { name: "Aprobar y comprar" })).toHaveLength(2)
    await expect(canvas.getByRole("link", { name: /Ver orden/ })).toBeVisible()
    await expectFitsPhoneColumn(canvasElement)
  },
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
