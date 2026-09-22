import { cn } from "cn"
import { CheckIcon, LoaderCircleIcon, TriangleAlertIcon, XIcon } from "lucide-react"
import type { ReactNode } from "react"

import {
  checkoutStatusLabel,
  formatElapsed,
  formatMoney,
  intentGroups,
  interpretActionLabel,
  pluralize,
  purchaseStageLabel,
  summarizeRejections,
} from "@/components/agent/agent-format"
import { toolStepLabel, type StepStatus } from "@/components/agent/agent-steps"
import { chip } from "@/components/agent/agent-styles"
import { ReasoningText } from "@/components/agent/reasoning-text"
import type {
  InterpretRequestResult,
  PrepareCheckoutResult,
  PurchaseUpdateResult,
  SearchCatalogResult,
} from "@/lib/agent-contract"

import "./agent.css"

const STATUS_TEXT: Record<StepStatus, string> = {
  running: "En curso",
  complete: "Completado",
  error: "Con error",
}

function StatusIcon({ status }: { status: StepStatus }) {
  return (
    <span
      className={cn(
        "relative z-10 flex size-5 shrink-0 items-center justify-center rounded-full",
        status === "complete" && "bg-[#ececec] text-[#0d0d0d]",
        status === "running" && "bg-white text-[#0d0d0d]",
        status === "error" && "bg-red-50 text-red-700",
      )}
      aria-hidden
    >
      {status === "complete" && <CheckIcon className="size-3" strokeWidth={2.5} />}
      {status === "running" && <LoaderCircleIcon className="size-4 motion-safe:animate-spin" />}
      {status === "error" && <XIcon className="size-3" strokeWidth={2.5} />}
    </span>
  )
}

export type StepRowProps = {
  status: StepStatus
  title: ReactNode
  /** Línea breve bajo el título. */
  summary?: ReactNode
  elapsedMs?: number
  /** Mensaje saneado cuando el paso falla. */
  error?: string | null
  children?: ReactNode
  className?: string
}

/** Fila de la línea de tiempo; se usa dentro de `ThinkingGroup` (que aporta el `<ol>`). */
export function StepRow({
  status,
  title,
  summary,
  elapsedMs,
  error,
  children,
  className,
}: StepRowProps) {
  const elapsed = elapsedMs === undefined ? null : formatElapsed(elapsedMs)

  return (
    <li
      className={cn("group/step relative flex gap-3 pb-4 last:pb-0", className)}
      data-status={status}
    >
      <span
        className="absolute top-6 bottom-1 left-[9.5px] w-px bg-[#e5e5e5] group-last/step:hidden"
        aria-hidden
      />
      <StatusIcon status={status} />
      <div className="min-w-0 flex-1">
        <div className="flex min-h-5 items-baseline justify-between gap-3">
          <p
            className={cn(
              "min-w-0 text-sm leading-5 font-medium",
              status === "error" && "text-red-700",
            )}
          >
            {status === "running" ? <span className="agent-shimmer">{title}</span> : title}
            <span className="sr-only"> ({STATUS_TEXT[status]})</span>
          </p>
          {elapsed && (
            <span className="shrink-0 text-xs text-[#6b6b6b] tabular-nums">{elapsed}</span>
          )}
        </div>
        {summary && <div className="mt-0.5 text-sm leading-5 text-[#6b6b6b]">{summary}</div>}
        {status === "error" && error && (
          <p className="mt-1 text-sm leading-5 text-red-700 wrap-break-word">{error}</p>
        )}
        {children && <div className="mt-2 empty:hidden">{children}</div>}
      </div>
    </li>
  )
}

type StepProps<Result> = {
  status: StepStatus
  result?: Result | null
  elapsedMs?: number
  error?: string | null
  /** Contenido extra bajo el detalle (por ejemplo, el carrusel de resultados). */
  children?: ReactNode
}

export function ReasoningStep({
  text,
  streaming = false,
  elapsedMs,
}: {
  text: string
  streaming?: boolean
  elapsedMs?: number
}) {
  return (
    <StepRow
      status={streaming ? "running" : "complete"}
      title={streaming ? "Razonando…" : "Razonamiento"}
      elapsedMs={elapsedMs}
    >
      <ReasoningText text={text} streaming={streaming} />
    </StepRow>
  )
}

export function InterpretRequestStep({
  status,
  result,
  elapsedMs,
  error,
  children,
}: StepProps<InterpretRequestResult>) {
  const groups = result ? intentGroups(result.constraints, result.preferences) : []
  const hasSoft = groups.some((group) => group.chips.some((item) => item.soft))

  return (
    <StepRow
      status={status}
      title={toolStepLabel("interpret_request", status)}
      elapsedMs={elapsedMs}
      error={error}
      summary={
        status === "complete" && result ? interpretActionLabel(result.action) : undefined
      }
    >
      {status === "complete" && groups.length > 0 && (
        <div className="space-y-2">
          <dl className="space-y-1.5">
            {groups.map((group) => (
              <div key={group.kind} className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <dt className="w-24 shrink-0 text-xs text-[#6b6b6b]">{group.label}</dt>
                <dd className="flex min-w-0 flex-wrap gap-1.5">
                  {group.chips.map((item) => (
                    <span
                      key={`${item.soft}:${item.value}`}
                      className={chip({ tone: item.soft ? "soft" : "neutral", size: "sm" })}
                      title={item.soft ? "Preferencia" : "Requisito"}
                    >
                      <span className="truncate">{item.value}</span>
                      {item.soft && <span className="sr-only"> (preferencia)</span>}
                    </span>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
          {hasSoft && (
            <p className="text-xs leading-5 text-[#6b6b6b]">
              Borde punteado: preferencia deseable, no obligatoria.
            </p>
          )}
        </div>
      )}
      {children}
    </StepRow>
  )
}

export function SearchCatalogStep({
  status,
  result,
  elapsedMs,
  error,
  children,
}: StepProps<SearchCatalogResult>) {
  const rejections = summarizeRejections(result?.rejections)
  const summary =
    status === "complete" && result
      ? [
          pluralize(result.matches, "coincidencia", "coincidencias"),
          rejections.total > 0 ? pluralize(rejections.total, "descartado", "descartados") : null,
        ]
          .filter(Boolean)
          .join(", ")
      : undefined

  return (
    <StepRow
      status={status}
      title={toolStepLabel("search_catalog", status)}
      elapsedMs={elapsedMs}
      error={error}
      summary={summary}
    >
      {status === "complete" && rejections.items.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label="Productos descartados por motivo">
          {rejections.items.map((item) => (
            <li key={item.reason} className={chip({ tone: "neutral", size: "sm" })}>
              <span className="font-medium tabular-nums">{item.count}</span>
              <span>{item.label}</span>
            </li>
          ))}
        </ul>
      )}
      {children}
    </StepRow>
  )
}

export function PrepareCheckoutStep({
  status,
  result,
  elapsedMs,
  error,
  children,
}: StepProps<PrepareCheckoutResult>) {
  const ready = result?.checkout_status === "ready_for_payment"

  return (
    <StepRow
      status={status}
      title={toolStepLabel("prepare_checkout", status)}
      elapsedMs={elapsedMs}
      error={error}
    >
      {status === "complete" && result && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className={chip({ tone: ready ? "success" : "warning", size: "sm" })}>
              {checkoutStatusLabel(result.checkout_status)}
            </span>
            <span className="text-[#6b6b6b]">
              Total del checkout{" "}
              <span className="font-medium text-[#0d0d0d] tabular-nums">
                {formatMoney(result.total_minor, result.currency)}
              </span>
            </span>
          </div>
          {result.price_changed && (
            <p className="flex gap-2 rounded-[12px] bg-amber-50 px-3 py-2 text-sm leading-5 text-amber-900">
              <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
              El precio cambió respecto al catálogo. Revisa el total antes de aprobar.
            </p>
          )}
        </div>
      )}
      {children}
    </StepRow>
  )
}

export function PurchaseUpdateStep({
  status,
  result,
  elapsedMs,
  error,
  children,
}: StepProps<PurchaseUpdateResult>) {
  const title =
    status === "complete" && result
      ? purchaseStageLabel(result.stage)
      : toolStepLabel("purchase_update", status)
  const failedStage = result?.stage === "failed"

  return (
    <StepRow
      status={failedStage ? "error" : status}
      title={title}
      elapsedMs={elapsedMs}
      error={error}
      summary={
        status === "complete" && result?.order_id ? (
          <>
            Orden <span className="font-mono text-xs text-[#0d0d0d]">{result.order_id}</span>
          </>
        ) : undefined
      }
    >
      {children}
    </StepRow>
  )
}
