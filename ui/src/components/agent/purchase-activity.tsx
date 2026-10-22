import { cn } from "cn"
import { ChevronRightIcon } from "lucide-react"
import { Collapsible } from "radix-ui"

import { focusRing } from "@/components/agent/agent-styles"
import { ControlTimeline } from "@/components/trace/control-timeline"
import { PurchaseStatusIcon } from "@/components/trace/purchase-status-label"
import { TechnicalDetails } from "@/components/trace/technical-details"
import { purchaseSummary, traceRefs } from "@/components/trace/trace-copy"
import type { PurchaseStatus, PurchaseTrace } from "@/lib/trace-contract"

import "./agent.css"

/** Lo que el stream ya dice de la compra, para pintar la fila antes de que llegue su traza. */
export type StreamPurchase = {
  status: PurchaseStatus
  summary: string
  /** Nombre del producto elegido; el comercio llega con la traza. */
  product: string | null
}

export type PurchaseActivityProps = {
  /** `null` mientras la traza viene en camino: la fila usa `stream` y aún no se abre. */
  trace: PurchaseTrace | null
  stream?: StreamPurchase
  defaultOpen?: boolean
  /**
   * Anunciar el estado a los lectores de pantalla. En el chat calla cuando el mensaje tiene una
   * tarjeta de la compra que ya anuncia ese estado. La región sigue montada aunque calle: una región
   * que aparece ya con su texto no siempre se lee.
   */
  announce?: boolean
  className?: string
}

function productOf(trace: PurchaseTrace) {
  const selection = trace.steps.find((step) => step.kind === "selection")
  return selection?.kind === "selection" ? selection.product : null
}

/**
 * Fila plegable de una compra en el chat. La línea de resumen cambia con el estado; al abrirla se
 * ve la línea de tiempo de controles. Las decisiones (aprobar, el bloqueo) van fuera, siempre a la
 * vista. Sale con lo que dice el stream, ya a su altura final, y la traza solo la completa: así no
 * entra después empujando las tarjetas.
 */
export function PurchaseActivity({
  trace,
  stream,
  defaultOpen = false,
  announce = true,
  className,
}: PurchaseActivityProps) {
  const status = trace?.status ?? stream?.status ?? "in_progress"
  const summary = trace ? purchaseSummary(trace) : (stream?.summary ?? "")
  const label = trace ? [productOf(trace), trace.merchant_name] : [stream?.product]
  const working = status === "in_progress"

  return (
    <Collapsible.Root
      defaultOpen={defaultOpen}
      disabled={trace === null}
      className={cn("min-w-0", className)}
      data-testid="purchase-activity"
      data-status={status}
      data-source={trace ? "trace" : "stream"}
    >
      {/* La traza se relee mientras la compra avanza: cada cambio de estado se anuncia una vez. */}
      <span role="status" className="sr-only">
        {announce ? summary : ""}
      </span>
      {/*
        Rejilla y no flex: el resumen crece primero hasta su ancho completo y el producto solo usa
        lo que sobra. Con flex ambos se encogen a la vez y el estado salía cortado en el móvil. Si
        aun así no cabe (320 px), el resumen pasa a dos líneas: el estado nunca se recorta.
      */}
      <Collapsible.Trigger
        className={cn(
          "group/trigger -ml-2 inline-grid min-h-11 max-w-full grid-cols-[auto_minmax(0,max-content)_minmax(0,1fr)_auto] items-center gap-2 rounded-full px-2 text-left text-sm transition-colors hover:bg-[#f7f7f7] disabled:hover:bg-transparent sm:min-h-9",
          focusRing,
        )}
        aria-busy={working || undefined}
      >
        <PurchaseStatusIcon
          status={status}
          className={status === "failed" ? "text-red-700" : "text-[#6b6b6b]"}
        />
        <span
          className={cn(
            "min-w-0 leading-5 font-medium wrap-break-word",
            working && "agent-shimmer",
            status === "failed" && "text-red-700",
          )}
        >
          {summary}
        </span>
        {/* Producto y comercio por su nombre; sin nombre, solo el producto, nunca el origen. */}
        <span className="truncate text-xs text-[#6b6b6b]">
          {label.filter(Boolean).join(" · ")}
        </span>
        <ChevronRightIcon
          className="size-4 shrink-0 text-[#6b6b6b] transition-transform duration-200 group-disabled/trigger:opacity-40 group-data-[state=open]/trigger:rotate-90"
          aria-hidden
        />
      </Collapsible.Trigger>

      {/* Margen negativo: el contenido recorta, y los anillos de foco necesitan espacio. */}
      <Collapsible.Content className="agent-collapsible -mx-2 px-2">
        {trace && (
          <div className="space-y-3 pt-3 pb-1">
            <ControlTimeline trace={trace} />
            <TechnicalDetails refs={traceRefs(trace)} />
          </div>
        )}
      </Collapsible.Content>
    </Collapsible.Root>
  )
}
