import { cn } from "cn"
import {
  AlertCircleIcon,
  BanIcon,
  CheckIcon,
  ChevronDownIcon,
  ExternalLinkIcon,
  LoaderCircleIcon,
  RotateCcwIcon,
  ShieldCheckIcon,
  TriangleAlertIcon,
  XIcon,
} from "lucide-react"
import { useId, useState, type ReactNode } from "react"

import {
  compactFingerprint,
  formatDateTime,
  formatMoney,
  formatMoneyParts,
  safeLinkUrl,
} from "@/components/agent/agent-format"
import { chip, focusRing, pillButton } from "@/components/agent/agent-styles"
import { ProductImage } from "@/components/agent/product-card"
import type { ApprovalTerms, ProductCard } from "@/lib/agent-contract"
import type { CompletedCheckout } from "@/lib/commerce-types"

import "./agent.css"

export type ApprovalCardStatus =
  | "awaiting_approval"
  | "approving"
  | "completing"
  | "canceling"
  | "canceled"
  | "completed"
  | "error"

export type ApprovalCardOrder = Pick<CompletedCheckout["order"], "id"> &
  Partial<Pick<CompletedCheckout["order"], "order_number" | "status" | "permalink_url">>

export type PurchaseApprovalCardProps = {
  /** Términos autoritativos de `request_approval`; el total sale solo de aquí. */
  terms: ApprovalTerms
  status: ApprovalCardStatus
  /** Producto elegido, para la miniatura y el nombre. Sin él se usa la primera línea. */
  product?: ProductCard | null
  /**
   * La aprobación ya quedó registrada: cancelar deja de estar disponible y el botón principal solo
   * completa la compra (reintento).
   */
  approvalRecorded?: boolean
  /** Mensaje saneado del backend. */
  error?: string | null
  /** `prepare_checkout.price_changed`: el precio difiere del observado en el catálogo. */
  priceChanged?: boolean
  order?: ApprovalCardOrder | null
  /** Aprueba y completa; con `approvalRecorded` solo debe completar. */
  onApprove?: () => void
  onCancel?: () => void
  onNewPurchase?: () => void
  className?: string
}

const ORDER_STATUS_LABELS: Record<string, string> = {
  confirmed: "Confirmada",
  created: "Creada",
  processing: "En proceso",
  shipped: "Enviada",
  canceled: "Cancelada",
}

function Rows({ children, className }: { children: ReactNode; className?: string }) {
  return <dl className={cn("space-y-2.5 text-sm", className)}>{children}</dl>
}

function Row({
  label,
  children,
  strong = false,
  mono = false,
}: {
  label: ReactNode
  children: ReactNode
  strong?: boolean
  mono?: boolean
}) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="min-w-0 text-[#6b6b6b] wrap-break-word">{label}</dt>
      <dd
        className={cn(
          "shrink-0 text-right tabular-nums",
          strong ? "font-semibold" : "font-medium",
          mono && "max-w-[60%] shrink font-mono text-xs break-all",
        )}
      >
        {children}
      </dd>
    </div>
  )
}

function deliveryText(terms: ApprovalTerms) {
  const address = terms.fulfillment_details?.address
  const parts = [address?.city, address?.country].filter(Boolean)
  return parts.length > 0 ? parts.join(", ") : "Sin dirección"
}

const APPROVE_FOOTER = "Aprobar autoriza únicamente estos términos. No mueve dinero real."

/** Encabezado, rótulo del total y pie: las tres líneas cambian juntas para no contradecirse. */
function statusCopy(status: ApprovalCardStatus, approvalRecorded: boolean) {
  if (status === "completed") {
    return {
      title: "Compra completada",
      subtitle: "El comercio registró la orden.",
      total: "Total pagado en sandbox",
      footer: null,
    }
  }
  if (status === "canceled") {
    return {
      title: "Compra cancelada",
      subtitle: "No se registró ninguna aprobación ni cargo.",
      total: "Total no aprobado",
      footer: null,
    }
  }
  if (status === "canceling") {
    return {
      title: "Cancelando la compra",
      subtitle: "Liberando el checkout en el comercio.",
      total: "Total a aprobar",
      footer: APPROVE_FOOTER,
    }
  }
  if (approvalRecorded || status === "completing") {
    return {
      title: "Aprobación registrada",
      subtitle: "Falta completar la compra en el comercio.",
      total: "Total aprobado",
      footer: "Ya autorizaste estos términos; completar no cambia el total ni mueve dinero real.",
    }
  }
  return {
    title: "Aprueba la compra",
    subtitle: "Revisa los términos antes de continuar.",
    total: "Total a aprobar",
    footer: APPROVE_FOOTER,
  }
}

function HeaderIcon({ status }: { status: ApprovalCardStatus }) {
  if (status === "completed") {
    return (
      <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-emerald-600 text-white">
        <CheckIcon className="size-5" aria-hidden />
      </span>
    )
  }
  if (status === "canceled") {
    return (
      <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-[#f2f2f2] text-[#525252]">
        <BanIcon className="size-5" aria-hidden />
      </span>
    )
  }
  return (
    <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-[#0d0d0d] text-white">
      <ShieldCheckIcon className="size-5" aria-hidden />
    </span>
  )
}

function Notice({
  tone,
  children,
  alert = false,
}: {
  tone: "warning" | "danger"
  children: ReactNode
  alert?: boolean
}) {
  const Icon = tone === "danger" ? AlertCircleIcon : TriangleAlertIcon
  return (
    <div
      role={alert ? "alert" : undefined}
      className={cn(
        "flex gap-2.5 rounded-[16px] px-3.5 py-3 text-sm leading-5",
        tone === "danger" ? "bg-red-50 text-red-800" : "bg-amber-50 text-amber-900",
      )}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <p className="min-w-0 wrap-break-word">{children}</p>
    </div>
  )
}

function AcpDetails({ terms }: { terms: ApprovalTerms }) {
  const expires = formatDateTime(terms.expires_at)
  return (
    <details className="group rounded-[16px] bg-[#f7f7f7] text-xs">
      <summary
        className={cn(
          "flex min-h-11 cursor-pointer list-none items-center justify-between gap-2 rounded-[16px] px-3.5 font-medium text-[#0d0d0d] select-none sm:min-h-10 [&::-webkit-details-marker]:hidden",
          focusRing,
        )}
      >
        Detalles ACP
        <ChevronDownIcon
          className="size-4 text-[#6b6b6b] transition-transform group-open:rotate-180"
          aria-hidden
        />
      </summary>
      <Rows className="px-3.5 pb-3.5 text-xs">
        <Row label="Checkout" mono>
          {terms.checkout_id}
        </Row>
        <Row label="Fingerprint" mono>
          <span title={terms.fingerprint}>{compactFingerprint(terms.fingerprint)}</span>
        </Row>
        <Row label="Manejador de pago" mono>
          {terms.payment_handler}
        </Row>
        <Row label="Comercio" mono>
          {terms.merchant_origin}
        </Row>
        {expires && <Row label="Vence">{expires}</Row>}
      </Rows>
    </details>
  )
}

export function PurchaseApprovalCard({
  terms,
  status,
  product,
  approvalRecorded = false,
  error,
  priceChanged = false,
  order,
  onApprove,
  onCancel,
  onNewPurchase,
  className,
}: PurchaseApprovalCardProps) {
  const titleId = useId()
  const recorded = approvalRecorded || status === "completing"
  const busy = status === "approving" || status === "completing" || status === "canceling"
  const settled = status === "completed" || status === "canceled"
  const copy = statusCopy(status, recorded)
  const total = formatMoneyParts(terms.total_minor, terms.currency)
  const productTitle = product?.title ?? terms.lines[0]?.name ?? "Compra"
  const lineSum = terms.lines.reduce((sum, line) => sum + line.quantity * line.unit_amount_minor, 0)
  const extra = terms.total_minor - lineSum
  const orderUrl = safeLinkUrl(order?.permalink_url)
  const liveStatus =
    status === "approving"
      ? "Registrando aprobación…"
      : status === "completing"
        ? "Completando compra…"
        : status === "canceling"
          ? "Cancelando la compra…"
          : ""

  return (
    <section
      aria-labelledby={titleId}
      aria-busy={busy || undefined}
      data-status={status}
      data-testid="purchase-approval-card"
      className={cn(
        "@container w-full max-w-[30rem] overflow-hidden rounded-[24px] border border-[#ececec] bg-white text-[#0d0d0d] shadow-[0_1px_2px_rgba(0,0,0,0.04),0_12px_32px_-18px_rgba(0,0,0,0.18)]",
        className,
      )}
    >
      <p role="status" className="sr-only">
        {liveStatus}
      </p>

      <div className="flex items-start gap-3 p-5 pb-0">
        <HeaderIcon status={status} />
        <div className="min-w-0 pt-0.5">
          <h3 id={titleId} className="text-base leading-6 font-semibold tracking-[-0.01em]">
            {copy.title}
          </h3>
          <p className="text-sm text-[#6b6b6b]">{copy.subtitle}</p>
        </div>
      </div>

      <div className="space-y-5 p-5">
        <div className="flex items-center gap-3">
          <ProductImage
            image={product?.image}
            fallbackAlt={productTitle}
            seed={product?.product_id ?? terms.checkout_id}
            caption={false}
            className="size-14 shrink-0 rounded-[12px]"
          />
          <div className="min-w-0">
            <p className="line-clamp-2 text-sm leading-5 font-medium wrap-break-word">
              {productTitle}
            </p>
            {product?.variant_title && (
              <p className="truncate text-xs text-[#6b6b6b]">{product.variant_title}</p>
            )}
          </div>
        </div>

        <div>
          <p className="text-xs font-medium text-[#6b6b6b]">{copy.total}</p>
          <p className="mt-0.5 flex items-baseline gap-1.5">
            <span
              className={cn(
                "text-[2rem] leading-10 font-semibold tracking-[-0.04em] tabular-nums",
                status === "canceled" && "text-[#6b6b6b] line-through decoration-2",
              )}
            >
              {total.amount}
            </span>
            <span className="text-sm font-medium text-[#6b6b6b]">{total.code}</span>
          </p>
        </div>

        {priceChanged && !settled && (
          <Notice tone="warning">
            El precio cambió desde que consulté el catálogo. Este total es el vigente en el
            checkout.
          </Notice>
        )}

        {status === "completed" && order ? (
          <Rows className="border-t border-[#ececec] pt-4">
            <Row label="Orden">{order.order_number ?? order.id}</Row>
            {order.status && (
              <Row label="Estado">{ORDER_STATUS_LABELS[order.status] ?? order.status}</Row>
            )}
            <Row label="Entrega">{deliveryText(terms)}</Row>
            <Row label="Pago">Sandbox</Row>
          </Rows>
        ) : (
          <Rows className="border-t border-[#ececec] pt-4">
            {terms.lines.map((line) => (
              <Row key={line.item_id} label={`${line.quantity} × ${line.name}`}>
                {formatMoney(line.quantity * line.unit_amount_minor, terms.currency)}
              </Row>
            ))}
            {extra !== 0 && (
              <Row label={extra > 0 ? "Envío y otros cargos" : "Descuentos"}>
                {formatMoney(extra, terms.currency)}
              </Row>
            )}
            <Row label="Entrega">{deliveryText(terms)}</Row>
            <Row label="Pago">
              <span className={chip({ tone: "neutral", size: "sm" })}>Sandbox</span>
            </Row>
          </Rows>
        )}

        {status !== "completed" && <AcpDetails terms={terms} />}

        {status === "error" && error && (
          <Notice tone="danger" alert>
            {error}
          </Notice>
        )}

        <Actions
          status={status}
          recorded={recorded}
          busy={busy}
          orderUrl={orderUrl}
          onApprove={onApprove}
          onCancel={onCancel}
          onNewPurchase={onNewPurchase}
        />

        {copy.footer && (
          <p className="text-center text-xs leading-5 text-[#6b6b6b]">{copy.footer}</p>
        )}
      </div>
    </section>
  )
}

function Spinner() {
  return <LoaderCircleIcon className="motion-safe:animate-spin" aria-hidden />
}

/**
 * Empezar otra compra reinicia la conversación, así que se confirma antes: el recibo, el número de
 * orden y el enlace al comercio desaparecen con ella y no hay forma de recuperarlos.
 */
function NewPurchaseButton({
  onNewPurchase,
  detail,
  className,
  confirmClassName,
}: {
  onNewPurchase?: () => void
  /** Qué se pierde al reiniciar, según lo que muestre esta tarjeta. */
  detail: string
  className?: string
  confirmClassName?: string
}) {
  const [confirming, setConfirming] = useState(false)
  if (!onNewPurchase) return null

  if (!confirming) {
    return (
      <button
        type="button"
        onClick={() => setConfirming(true)}
        className={cn(pillButton({ tone: "soft", size: "lg" }), className)}
      >
        <RotateCcwIcon aria-hidden />
        Nueva compra
      </button>
    )
  }

  return (
    <div className={cn("space-y-3 rounded-[16px] bg-[#f7f7f7] p-3.5", confirmClassName, className)}>
      <p className="text-sm leading-5 text-[#3d3d3d]">{detail}</p>
      <div className="grid grid-cols-1 gap-2 @xs:grid-cols-2">
        <button
          type="button"
          onClick={() => setConfirming(false)}
          className={pillButton({ tone: "outline", size: "lg" })}
        >
          Seguir aquí
        </button>
        <button
          type="button"
          onClick={onNewPurchase}
          className={pillButton({ tone: "ink", size: "lg" })}
        >
          Empezar otra
        </button>
      </div>
    </div>
  )
}

function Actions({
  status,
  recorded,
  busy,
  orderUrl,
  onApprove,
  onCancel,
  onNewPurchase,
}: {
  status: ApprovalCardStatus
  recorded: boolean
  busy: boolean
  orderUrl: string | null
  onApprove?: () => void
  onCancel?: () => void
  onNewPurchase?: () => void
}) {
  if (status === "completed") {
    return (
      <div className="grid grid-cols-1 gap-2 @xs:grid-cols-2">
        <NewPurchaseButton
          onNewPurchase={onNewPurchase}
          detail="Empezar otra compra borra esta conversación, con el recibo y el enlace a la orden."
          confirmClassName="@xs:col-span-2"
        />
        {orderUrl && (
          <a
            href={orderUrl}
            target="_blank"
            rel="noopener noreferrer"
            className={pillButton({ tone: "ink", size: "lg" })}
          >
            Ver orden
            <ExternalLinkIcon aria-hidden />
            <span className="sr-only">(se abre en una pestaña nueva)</span>
          </a>
        )}
      </div>
    )
  }

  if (status === "canceled") {
    return (
      <NewPurchaseButton
        onNewPurchase={onNewPurchase}
        detail="Empezar otra compra borra esta conversación y sus mensajes."
        className="w-full"
      />
    )
  }

  // Sin `onApprove` la tarjeta es de solo lectura (compra reemplazada o fallida): no hay acciones.
  if (!onApprove) return null

  const primaryLabel =
    status === "approving"
      ? "Registrando aprobación…"
      : status === "completing"
        ? "Completando compra…"
        : recorded
          ? status === "error"
            ? "Reintentar compra"
            : "Completar compra"
          : "Aprobar y comprar"

  return (
    <div className={cn("grid gap-2", recorded ? "grid-cols-1" : "grid-cols-[auto_1fr]")}>
      {!recorded && (
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className={pillButton({ tone: "soft", size: "lg" })}
        >
          {status === "canceling" ? <Spinner /> : <XIcon aria-hidden />}
          {status === "canceling" ? "Cancelando…" : "Cancelar"}
        </button>
      )}
      <button
        type="button"
        onClick={onApprove}
        disabled={busy}
        className={cn(pillButton({ tone: "ink", size: "lg" }), "w-full")}
        data-testid="approve-purchase"
      >
        {(status === "approving" || status === "completing") && <Spinner />}
        {status === "error" && recorded && <RotateCcwIcon aria-hidden />}
        {primaryLabel}
      </button>
    </div>
  )
}

/** Marcador mientras `request_approval` trae los términos. */
export function PurchaseApprovalCardSkeleton({ className }: { className?: string }) {
  return (
    <div
      role="status"
      aria-label="Preparando los términos de la compra"
      className={cn(
        "w-full max-w-[30rem] space-y-5 rounded-[24px] border border-[#ececec] bg-white p-5",
        className,
      )}
    >
      <div className="flex items-center gap-3">
        <div className="agent-skeleton size-10 rounded-full" />
        <div className="flex-1 space-y-2">
          <div className="agent-skeleton h-4 w-1/2 rounded-full" />
          <div className="agent-skeleton h-3 w-2/3 rounded-full" />
        </div>
      </div>
      <div className="agent-skeleton h-9 w-40 rounded-[12px]" />
      <div className="space-y-3 border-t border-[#ececec] pt-4">
        <div className="agent-skeleton h-3 w-full rounded-full" />
        <div className="agent-skeleton h-3 w-5/6 rounded-full" />
        <div className="agent-skeleton h-3 w-2/3 rounded-full" />
      </div>
      <div className="agent-skeleton h-12 w-full rounded-full" />
    </div>
  )
}
