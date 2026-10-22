import { cn } from "cn"

import { formatLiveElapsed } from "@/components/agent/agent-format"

import "./agent.css"

export type ThinkingIndicatorProps = {
  /** Qué hace el agente ahora; por ejemplo `toolStepLabel(toolName, "running")`. */
  label: string
  elapsedMs?: number
  className?: string
}

/**
 * Línea de estado en vivo: el paso actual y el tiempo. La etiqueta se monta de nuevo cuando cambia,
 * así entra con un fundido y los lectores de pantalla la anuncian; el tiempo queda fuera de la región
 * viva, con formato y ancho fijos para que no salte en cada tic.
 */
export function ThinkingIndicator({ label, elapsedMs, className }: ThinkingIndicatorProps) {
  const elapsed = elapsedMs === undefined ? null : formatLiveElapsed(elapsedMs)

  return (
    <div
      className={cn("flex min-h-6 min-w-0 items-center gap-2.5 text-sm", className)}
      data-testid="thinking-indicator"
    >
      <span className="relative flex size-2.5 shrink-0 items-center justify-center" aria-hidden>
        <span className="agent-pulse-dot size-2.5 rounded-full bg-[#0d0d0d]" />
      </span>
      <span role="status" className="min-w-0 truncate font-medium">
        <span key={label} className="agent-enter inline-block max-w-full truncate align-bottom">
          <span className="agent-shimmer">{label}</span>
        </span>
      </span>
      {elapsed && (
        <span
          className="min-w-[6ch] shrink-0 rounded-full bg-[#f2f2f2] px-2 py-0.5 text-center text-xs text-[#525252] tabular-nums"
          aria-hidden
        >
          {elapsed}
        </span>
      )}
    </div>
  )
}
