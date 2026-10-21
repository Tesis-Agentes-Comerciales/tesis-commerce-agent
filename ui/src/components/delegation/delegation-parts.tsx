import { cn } from "cn"
import { AlertCircleIcon, type LucideIcon } from "lucide-react"
import type { ReactNode, Ref } from "react"

import { formatDateTime } from "@/components/agent/agent-format"
import { SCOPE_LABEL } from "@/components/delegation/delegation-copy"
import type { ScopeView } from "@/lib/delegation-contract"

// Piezas compartidas por las tarjetas de delegación, con la misma superficie, filas y avisos que
// las tarjetas de la compra HP (PurchaseApprovalCard, StaleTermsCard).

export const CARD_SURFACE =
  "@container w-full max-w-[30rem] overflow-hidden rounded-[24px] border border-[#ececec] bg-white text-[#0d0d0d] shadow-[0_1px_2px_rgba(0,0,0,0.04),0_12px_32px_-18px_rgba(0,0,0,0.18)]"

/** Fondo del icono del encabezado; el título dice el estado, que nunca se lee solo por el color. */
export type HeaderTone = "ink" | "success" | "muted" | "warning" | "danger"

const HEADER_TONE: Record<HeaderTone, string> = {
  ink: "bg-[#0d0d0d] text-white",
  success: "bg-emerald-600 text-white",
  muted: "bg-[#f2f2f2] text-[#525252]",
  warning: "bg-amber-50 text-amber-900",
  danger: "bg-red-50 text-red-700",
}

export function CardHeader({
  Icon,
  tone,
  spin = false,
  title,
  titleId,
  titleRef,
  subtitle,
}: {
  Icon: LucideIcon
  tone: HeaderTone
  spin?: boolean
  title: string
  titleId: string
  /** El título recibe el foco cuando desaparece el botón que se pulsó (`useCardFocus`). */
  titleRef?: Ref<HTMLHeadingElement>
  subtitle?: string
}) {
  return (
    <div className="flex items-start gap-3 p-5 pb-0">
      <span
        className={cn(
          "flex size-10 shrink-0 items-center justify-center rounded-full",
          HEADER_TONE[tone],
        )}
      >
        <Icon className={cn("size-5", spin && "motion-safe:animate-spin")} aria-hidden />
      </span>
      <div className="min-w-0 pt-0.5">
        <h3
          ref={titleRef}
          id={titleId}
          tabIndex={titleRef ? -1 : undefined}
          className="text-base leading-6 font-semibold tracking-[-0.01em] wrap-break-word outline-none"
        >
          {title}
        </h3>
        {subtitle && <p className="text-sm wrap-break-word text-[#6b6b6b]">{subtitle}</p>}
      </div>
    </div>
  )
}

export function VariantList({ variants }: { variants: ScopeView["variants"] }) {
  return (
    <ul className="space-y-1">
      {variants.map((variant) => (
        <li key={variant.variant_id} className="text-sm leading-5 font-medium wrap-break-word">
          {variant.title}
        </li>
      ))}
    </ul>
  )
}

export function ScopeRows({ children }: { children: ReactNode }) {
  return <dl className="space-y-2.5 border-t border-[#ececec] pt-4 text-sm">{children}</dl>
}

/** Rótulo y valor en una fila; si no caben a 320 px, el valor baja a la derecha sin salirse. */
export function ScopeRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5">
      <dt className="text-[#6b6b6b]">{label}</dt>
      <dd className="ml-auto min-w-0 text-right font-medium wrap-break-word tabular-nums">
        {children}
      </dd>
    </div>
  )
}

export function DateTime({ value }: { value: string }) {
  return <time dateTime={value}>{formatDateTime(value) ?? value}</time>
}

/** La indicación es texto del propio comprador (§4.2): se muestra tal cual, nunca del modelo. */
export function PreferenceQuote({ text }: { text: string }) {
  return (
    <figure className="rounded-[16px] bg-[#f7f7f7] px-3.5 py-3 text-sm leading-5">
      <figcaption className="text-xs font-medium text-[#6b6b6b]">{SCOPE_LABEL.preference}</figcaption>
      <blockquote className="mt-0.5 wrap-break-word">«{text}»</blockquote>
    </figure>
  )
}

/** Mensaje saneado de la API cuando una acción falla; el botón sigue para reintentar. */
export function ErrorNotice({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className="flex gap-2.5 rounded-[16px] bg-red-50 px-3.5 py-3 text-sm leading-5 text-red-800"
    >
      <AlertCircleIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <p className="min-w-0 wrap-break-word">{children}</p>
    </div>
  )
}
