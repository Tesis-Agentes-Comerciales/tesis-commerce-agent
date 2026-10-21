import { cn } from "cn"
import { AlertCircleIcon, BanIcon, LoaderCircleIcon, RefreshCwIcon } from "lucide-react"
import { useId } from "react"

import { pillButton } from "@/components/agent/agent-styles"
import { useCardFocus } from "@/components/agent/use-card-focus"
import type { PendingTermsStatus } from "@/lib/commerce-api"

export type StaleTermsCardStatus = "stale" | "canceling" | "canceled"

/**
 * Por qué ya no se puede aprobar: la oferta cambió, no se pudo leer o venció (la oferta o el
 * checkout). `expired` solo se conoce en la sesión que recibió el error: al recargar, el backend
 * informa una oferta vencida como `changed`.
 */
export type StaleTermsReason = PendingTermsStatus | "expired"

export type StaleTermsCardProps = {
  terms: StaleTermsReason
  status: StaleTermsCardStatus
  /** Producto de la compra, si se conoce; una compra restaurada no lo trae. */
  productTitle?: string | null
  /** Mensaje saneado si cancelar falló; el botón sigue disponible para reintentar. */
  error?: string | null
  onCancel?: () => void
  /**
   * Ocupa el lugar de la tarjeta de aprobación que tenía el foco (aprobar respondió que los términos
   * ya no valen): el foco pasa a su título en vez de caer al `<body>`.
   */
  autoFocus?: boolean
  className?: string
}

const NO_CHARGE = "No se hizo ningún cobro."

const COPY = {
  changed: {
    title: "Las condiciones del comercio cambiaron",
    explanation:
      "El precio, el envío, la vigencia o la disponibilidad ya no son los que revisaste. Cancela esta compra y pídela de nuevo: prepararé otra con las condiciones vigentes.",
  },
  expired: {
    title: "Las condiciones que revisaste vencieron",
    explanation:
      "La oferta o el checkout del comercio ya no está vigente. Cancela esta compra y pídela de nuevo: prepararé otra con las condiciones vigentes.",
  },
  unavailable: {
    title: "No pude confirmar las condiciones con el comercio",
    explanation:
      "No sé si el precio y la disponibilidad siguen vigentes. Cancela esta compra y pídela de nuevo para revisarlas.",
  },
} satisfies Record<StaleTermsReason, { title: string; explanation: string }>

/**
 * Reemplaza a la tarjeta de aprobación cuando los términos ya no valen: aprobar no es posible y la
 * única salida es cancelar y pedir la compra otra vez. No es un error ni un bloqueo de la política,
 * así que va en ámbar y dice qué no ocurrió.
 */
export function StaleTermsCard({
  terms,
  status,
  productTitle,
  error,
  onCancel,
  autoFocus = false,
  className,
}: StaleTermsCardProps) {
  const titleId = useId()
  const canceled = status === "canceled"
  const copy = canceled
    ? { title: "Compra cancelada", explanation: "Ya puedes pedirla de nuevo en el chat." }
    : COPY[terms]
  // Cancelada, el botón que se pulsó ya no está: el foco pasa al título.
  const controlsGone = canceled || !onCancel
  const { cardRef, titleRef, announce } = useCardFocus<HTMLElement>(
    `${status}:${controlsGone}`,
    controlsGone,
    autoFocus,
  )
  // Si cancelar falló, lo dice su aviso (`role="alert"`); repetir el título sobraría.
  const live =
    status === "canceling"
      ? "Cancelando la compra…"
      : error
        ? ""
        : `${copy.title}. ${NO_CHARGE}`

  return (
    <>
      {/* Fuera de la sección, que lleva `aria-busy` mientras cancela (ver PurchaseApprovalCard). */}
      <p role="status" className="sr-only">
        {announce ? live : ""}
      </p>
      <section
        ref={cardRef}
        aria-labelledby={titleId}
        aria-busy={status === "canceling" || undefined}
        data-testid="stale-terms-card"
        data-status={status}
        className={cn(
          "w-full max-w-[30rem] overflow-hidden rounded-[24px] border border-[#ececec] bg-white text-[#0d0d0d] shadow-[0_1px_2px_rgba(0,0,0,0.04),0_12px_32px_-18px_rgba(0,0,0,0.18)]",
          className,
        )}
      >
        <div className="flex items-start gap-3 p-5 pb-0">
          <span
            className={cn(
              "flex size-10 shrink-0 items-center justify-center rounded-full",
              canceled ? "bg-[#f2f2f2] text-[#525252]" : "bg-amber-50 text-amber-900",
            )}
          >
            {canceled ? (
              <BanIcon className="size-5" aria-hidden />
            ) : (
              <RefreshCwIcon className="size-5" aria-hidden />
            )}
          </span>
          <div className="min-w-0 pt-0.5">
            <h3
              ref={titleRef}
              id={titleId}
              tabIndex={-1}
              className="text-base leading-6 font-semibold tracking-[-0.01em] outline-none"
            >
              {copy.title}
            </h3>
            <p className="text-sm text-[#6b6b6b]">{NO_CHARGE}</p>
          </div>
        </div>

        <div className="space-y-4 p-5">
          {productTitle && (
            <p className="text-sm leading-5 font-medium wrap-break-word">{productTitle}</p>
          )}
          <p className="text-sm leading-5 text-[#3d3d3d]">{copy.explanation}</p>

          {error && (
            <div
              role="alert"
              className="flex gap-2.5 rounded-[16px] bg-red-50 px-3.5 py-3 text-sm leading-5 text-red-800"
            >
              <AlertCircleIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
              <p className="min-w-0 wrap-break-word">{error}</p>
            </div>
          )}

          {!canceled && onCancel && (
            <button
              type="button"
              onClick={status === "canceling" ? undefined : onCancel}
              aria-disabled={status === "canceling" || undefined}
              className={cn(pillButton({ tone: "ink", size: "lg" }), "w-full")}
            >
              {status === "canceling" && (
                <LoaderCircleIcon className="motion-safe:animate-spin" aria-hidden />
              )}
              {status === "canceling" ? "Cancelando…" : "Cancelar compra"}
            </button>
          )}
        </div>
      </section>
    </>
  )
}
