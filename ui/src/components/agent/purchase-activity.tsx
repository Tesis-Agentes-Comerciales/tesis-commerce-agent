import { cn } from "cn"
import { ChevronRightIcon } from "lucide-react"
import { Collapsible } from "radix-ui"

import { focusRing } from "@/components/agent/agent-styles"
import { ControlTimeline } from "@/components/trace/control-timeline"
import { PurchaseStatusIcon } from "@/components/trace/purchase-status-label"
import { TechnicalDetails } from "@/components/trace/technical-details"
import { purchaseSummary, traceRefs } from "@/components/trace/trace-copy"
import type { PurchaseTrace } from "@/lib/trace-contract"

import "./agent.css"

export type PurchaseActivityProps = {
  trace: PurchaseTrace
  defaultOpen?: boolean
  /**
   * Anunciar los cambios de estado a los lectores de pantalla. En el chat calla cuando el mensaje
   * tiene una tarjeta de la compra, que ya los anuncia.
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
 * vista.
 */
export function PurchaseActivity({
  trace,
  defaultOpen = false,
  announce = true,
  className,
}: PurchaseActivityProps) {
  const product = productOf(trace)
  const summary = purchaseSummary(trace)
  const working = trace.status === "in_progress"

  return (
    <Collapsible.Root
      defaultOpen={defaultOpen}
      className={cn("min-w-0", className)}
      data-testid="purchase-activity"
      data-status={trace.status}
    >
      {/* La traza se relee mientras la compra avanza: cada cambio de estado se anuncia una vez. */}
      {announce && (
        <span role="status" className="sr-only">
          {summary}
        </span>
      )}
      {/*
        Rejilla y no flex: el resumen crece primero hasta su ancho completo y el producto solo usa
        lo que sobra. Con flex ambos se encogen a la vez y el estado salía cortado en el móvil. Si
        aun así no cabe (320 px), el resumen pasa a dos líneas: el estado nunca se recorta.
      */}
      <Collapsible.Trigger
        className={cn(
          "group/trigger -ml-2 inline-grid min-h-11 max-w-full grid-cols-[auto_minmax(0,max-content)_minmax(0,1fr)_auto] items-center gap-2 rounded-full px-2 text-left text-sm transition-colors hover:bg-[#f7f7f7] sm:min-h-9",
          focusRing,
        )}
        aria-busy={working || undefined}
      >
        <PurchaseStatusIcon
          status={trace.status}
          className={trace.status === "failed" ? "text-red-700" : "text-[#6b6b6b]"}
        />
        <span
          className={cn(
            "min-w-0 leading-5 font-medium wrap-break-word",
            working && "agent-shimmer",
            trace.status === "failed" && "text-red-700",
          )}
        >
          {summary}
        </span>
        <span className="truncate text-xs text-[#6b6b6b]">{product}</span>
        <ChevronRightIcon
          className="size-4 shrink-0 text-[#6b6b6b] transition-transform duration-200 group-data-[state=open]/trigger:rotate-90"
          aria-hidden
        />
      </Collapsible.Trigger>

      {/* Margen negativo: el contenido recorta, y los anillos de foco necesitan espacio. */}
      <Collapsible.Content className="agent-collapsible -mx-2 px-2">
        <div className="space-y-3 pt-3 pb-1">
          <ControlTimeline trace={trace} />
          <TechnicalDetails refs={traceRefs(trace)} />
        </div>
      </Collapsible.Content>
    </Collapsible.Root>
  )
}
