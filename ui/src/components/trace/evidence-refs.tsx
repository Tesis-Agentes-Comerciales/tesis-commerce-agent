import { cn } from "cn"
import { CheckIcon, CopyIcon } from "lucide-react"
import { useEffect, useState } from "react"

import { focusRing } from "@/components/agent/agent-styles"

export type EvidenceRef = {
  /** Qué identifica, en español: "Decisión", "Evidencia", "Orden". */
  label: string
  value: string
}

export type EvidenceRefsProps = {
  refs: EvidenceRef[]
  className?: string
}

/** Identificadores técnicos en mono, cada uno con su botón de copiar. */
export function EvidenceRefs({ refs, className }: EvidenceRefsProps) {
  if (refs.length === 0) {
    return (
      <p className={cn("text-xs leading-5 text-[#6b6b6b]", className)}>
        Esta compra todavía no tiene identificadores.
      </p>
    )
  }
  return (
    <dl className={cn("space-y-0.5", className)}>
      {refs.map((ref, index) => (
        <div
          key={`${index}:${ref.label}`}
          className="flex min-h-10 items-center justify-between gap-3 sm:min-h-8"
        >
          <dt className="shrink-0 text-xs text-[#6b6b6b]">{ref.label}</dt>
          <dd className="flex min-w-0 items-center gap-1">
            <span className="truncate font-mono text-xs" title={ref.value}>
              {ref.value}
            </span>
            <CopyButton evidence={ref} />
          </dd>
        </div>
      ))}
    </dl>
  )
}

type CopyState = "idle" | "copied" | "failed"

const COPY_FEEDBACK: Record<CopyState, string> = {
  idle: "",
  copied: "Copiado",
  failed: "No se pudo copiar",
}

function CopyButton({ evidence }: { evidence: EvidenceRef }) {
  const [state, setState] = useState<CopyState>("idle")

  useEffect(() => {
    if (state === "idle") return
    const timer = window.setTimeout(() => setState("idle"), 2000)
    return () => window.clearTimeout(timer)
  }, [state])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(evidence.value)
      setState("copied")
    } catch {
      // Sin permiso de portapapeles o fuera de un contexto seguro: el valor sigue visible para copiarlo a mano.
      setState("failed")
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={copy}
        aria-label={`Copiar ${evidence.label.toLowerCase()}`}
        className={cn(
          "flex size-10 shrink-0 items-center justify-center rounded-full text-[#6b6b6b] transition-colors hover:bg-[#f2f2f2] hover:text-[#0d0d0d] sm:size-8",
          focusRing,
        )}
      >
        {state === "copied" ? (
          <CheckIcon className="size-3.5" aria-hidden />
        ) : (
          <CopyIcon className="size-3.5" aria-hidden />
        )}
      </button>
      <span
        role="status"
        className={state === "failed" ? "shrink-0 text-xs text-red-700" : "sr-only"}
      >
        {COPY_FEEDBACK[state]}
      </span>
    </>
  )
}
