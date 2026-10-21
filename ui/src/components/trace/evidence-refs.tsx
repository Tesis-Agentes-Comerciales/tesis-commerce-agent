import { cn } from "cn"
import { CheckIcon, CopyIcon } from "lucide-react"
import { useEffect, useState } from "react"

import { focusRing } from "@/components/agent/agent-styles"
import { COPY_FEEDBACK, copyEvidenceLabel, EVIDENCE_EMPTY } from "@/components/trace/trace-copy"

export type EvidenceRef = {
  /** Qué identifica, en español: "Decisión", "Evidencia", "Orden". */
  label: string
  value: string
}

export type EvidenceRefsProps = {
  refs: EvidenceRef[]
  className?: string
}

/**
 * Identificadores técnicos en mono, cada uno con su botón de copiar. Con espacio (desde 28rem) van
 * en una fila y el valor se recorta; en una columna estrecha, como el chat a 320 px, el rótulo va
 * encima y el valor se lee entero, partido en varias líneas.
 */
export function EvidenceRefs({ refs, className }: EvidenceRefsProps) {
  if (refs.length === 0) {
    return (
      <p className={cn("text-xs leading-5 text-[#6b6b6b]", className)}>{EVIDENCE_EMPTY}</p>
    )
  }
  return (
    <dl className={cn("@container space-y-0.5", className)}>
      {refs.map((ref, index) => (
        <div
          key={`${index}:${ref.label}`}
          className="pt-1.5 @md:flex @md:min-h-10 @md:items-center @md:justify-between @md:gap-3 @md:pt-0 sm:@md:min-h-8"
        >
          <dt className="text-xs text-[#6b6b6b] @md:shrink-0">{ref.label}</dt>
          <dd className="flex min-w-0 items-center justify-between gap-1 @md:justify-end">
            <span className="min-w-0 font-mono text-xs break-all @md:truncate" title={ref.value}>
              {ref.value}
            </span>
            <CopyButton evidence={ref} />
          </dd>
        </div>
      ))}
    </dl>
  )
}

type CopyState = keyof typeof COPY_FEEDBACK

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
        aria-label={copyEvidenceLabel(evidence.label)}
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
