import { cn } from "cn"
import { ChevronRightIcon } from "lucide-react"
import { Collapsible } from "radix-ui"
import { useState, type ReactNode } from "react"

import { formatElapsed, pluralize } from "@/components/agent/agent-format"
import { focusRing } from "@/components/agent/agent-styles"

import "./agent.css"

export type ThinkingGroupProps = {
  /**
   * El mensaje sigue generándose: el título brilla. El grupo no se abre solo, así sus pasos no
   * empujan el mensaje mientras corre. El tiempo tampoco va aquí: lo lleva la línea viva al pie del
   * mensaje, la única con contador.
   */
  active: boolean
  /** Algún paso falló o el mensaje terminó con error. */
  failed?: boolean
  /** El turno se interrumpió (la persona lo detuvo o se cortó el stream). */
  stopped?: boolean
  /** Cuánto pensó el agente, para el resumen al terminar. */
  elapsedMs?: number
  /** Cantidad de pasos, al lado del título. Con 0 todavía no hay qué abrir. */
  stepCount?: number
  /** Abierto al terminar; por defecto se pliega. */
  defaultOpen?: boolean
  /** Control externo; si se omite, el grupo sigue a `active` hasta que la persona lo toque. */
  open?: boolean
  onOpenChange?: (open: boolean) => void
  /** Filas `StepRow`, `ReasoningStep` o los pasos de herramientas. */
  children: ReactNode
  className?: string
}

export function ThinkingGroup({
  active,
  failed = false,
  stopped = false,
  elapsedMs,
  stepCount,
  defaultOpen = false,
  open: controlledOpen,
  onOpenChange,
  children,
  className,
}: ThinkingGroupProps) {
  // Mientras nadie lo toque, plegado: también durante la generación.
  const [userOpen, setUserOpen] = useState<boolean | null>(null)
  const empty = stepCount === 0
  const open = !empty && (controlledOpen ?? userOpen ?? defaultOpen)
  const elapsed = elapsedMs === undefined ? null : formatElapsed(elapsedMs)

  const label = active
    ? "Pensando…"
    : stopped
      ? "Se detuvo antes de terminar"
      : failed
        ? "Se detuvo por un error"
        : elapsed
          ? `Pensó durante ${elapsed}`
          : "Pasos del agente"

  return (
    <Collapsible.Root
      open={open}
      onOpenChange={(next) => {
        setUserOpen(next)
        onOpenChange?.(next)
      }}
      disabled={empty}
      className={cn("min-w-0", className)}
      data-testid="thinking-group"
    >
      <Collapsible.Trigger
        className={cn(
          "group/trigger -ml-2 inline-flex min-h-11 max-w-full items-center gap-2 rounded-full px-2 text-sm transition-colors hover:bg-[#f7f7f7] disabled:hover:bg-transparent sm:min-h-9",
          focusRing,
        )}
        aria-busy={active || undefined}
      >
        <span
          className={cn(
            "truncate font-medium",
            active && "agent-shimmer",
            failed && !active && !stopped && "text-red-700",
            !active && (stopped || !failed) && "text-[#6b6b6b]",
          )}
        >
          {label}
        </span>
        {stepCount !== undefined && stepCount > 0 && (
          <span className="shrink-0 text-xs text-[#6b6b6b]">
            {pluralize(stepCount, "paso", "pasos")}
          </span>
        )}
        <ChevronRightIcon
          className="size-4 shrink-0 text-[#6b6b6b] transition-transform duration-200 group-data-[state=open]/trigger:rotate-90"
          aria-hidden
        />
      </Collapsible.Trigger>

      {/* Margen negativo: el contenido recorta, y los anillos de foco necesitan espacio. */}
      <Collapsible.Content className="agent-collapsible -mx-2 px-2">
        <ol className="pt-3 pb-1 pl-0.5">{children}</ol>
      </Collapsible.Content>
    </Collapsible.Root>
  )
}
