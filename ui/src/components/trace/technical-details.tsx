import { cn } from "cn"
import { ChevronDownIcon } from "lucide-react"

import { focusRing } from "@/components/agent/agent-styles"
import { EvidenceRefs, type EvidenceRef } from "@/components/trace/evidence-refs"
import { TECHNICAL_DETAILS_TITLE } from "@/components/trace/trace-copy"

/** Desplegable cerrado con los identificadores y códigos internos, como «Detalles ACP». */
export function TechnicalDetails({ refs, className }: { refs: EvidenceRef[]; className?: string }) {
  return (
    <details className={cn("group rounded-[16px] bg-[#f7f7f7] text-xs", className)}>
      <summary
        className={cn(
          "flex min-h-11 cursor-pointer list-none items-center justify-between gap-2 rounded-[16px] px-3.5 font-medium text-[#0d0d0d] select-none sm:min-h-10 [&::-webkit-details-marker]:hidden",
          focusRing,
        )}
      >
        {TECHNICAL_DETAILS_TITLE}
        <ChevronDownIcon
          className="size-4 text-[#6b6b6b] transition-transform group-open:rotate-180"
          aria-hidden
        />
      </summary>
      <EvidenceRefs refs={refs} className="px-3.5 pb-2" />
    </details>
  )
}
