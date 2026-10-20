import { cn } from "cn"
import { CheckIcon, LoaderCircleIcon, ShieldXIcon, XIcon } from "lucide-react"

import { STEP_STATE_LABEL } from "@/components/trace/trace-copy"
import type { StepState } from "@/lib/trace-contract"

import "@/components/agent/agent.css"

// Monocromo como las filas del chat. Solo dos estados llevan tono: `blocked` (la compra se detuvo a
// propósito, por la política o por un control de la aplicación como la frescura de la oferta; el
// título de la fila dice cuál) en ámbar con escudo y `error` (algo falló) en rojo con una equis.
const CIRCLE: Record<StepState, string> = {
  done: "bg-[#ececec] text-[#0d0d0d]",
  running: "bg-white text-[#0d0d0d]",
  waiting: "text-[#0d0d0d] ring-[1.5px] ring-[#0d0d0d] ring-inset",
  blocked: "bg-amber-50 text-amber-900",
  error: "bg-red-50 text-red-700",
  skipped: "border border-dashed border-[#a3a3a3]",
}

function Glyph({ state }: { state: StepState }) {
  switch (state) {
    case "done":
      return <CheckIcon className="size-3" strokeWidth={2.5} />
    case "running":
      return <LoaderCircleIcon className="size-4 motion-safe:animate-spin" />
    case "waiting":
      return <span className="agent-pulse-dot size-2 rounded-full bg-[#0d0d0d]" />
    case "blocked":
      return <ShieldXIcon className="size-3.5" strokeWidth={2.25} />
    case "error":
      return <XIcon className="size-3" strokeWidth={2.5} />
    case "skipped":
      return null
  }
}

export type StepStatusIconProps = {
  state: StepState
  /** La etiqueta queda solo para lectores de pantalla, cuando la fila ya lo dice con su título. */
  hideLabel?: boolean
  className?: string
}

export function StepStatusIcon({ state, hideLabel = false, className }: StepStatusIconProps) {
  return (
    <span className={cn("inline-flex items-center gap-2", className)} data-state={state}>
      <span
        className={cn(
          "relative z-10 flex size-5 shrink-0 items-center justify-center rounded-full",
          CIRCLE[state],
        )}
        aria-hidden
      >
        <Glyph state={state} />
      </span>
      <span className={hideLabel ? "sr-only" : "text-sm leading-5"}>{STEP_STATE_LABEL[state]}</span>
    </span>
  )
}
