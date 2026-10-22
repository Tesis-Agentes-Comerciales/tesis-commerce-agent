import { cn } from "cn"
import { MessageCircleIcon, SearchIcon } from "lucide-react"
import { useId } from "react"

import { pillButton } from "@/components/agent/agent-styles"
import { SEARCH_FIRST_COPY } from "@/components/delegation/delegation-copy"
import { CARD_SURFACE, CardHeader } from "@/components/delegation/delegation-parts"

export type DelegationSearchFirstProps = {
  /** Cierra «Delegaciones» y deja el foco en el compositor del chat. */
  onGoToChat?: () => void
  className?: string
}

/**
 * Lo que ve quien elige «Delegar» sin haber buscado: el formulario solo ofrece las opciones que el
 * chat ya mostró (§3.2), así que primero se busca. Ocupa el lugar de «Nueva delegación».
 */
export function DelegationSearchFirst({ onGoToChat, className }: DelegationSearchFirstProps) {
  const titleId = useId()
  return (
    <section
      aria-labelledby={titleId}
      data-testid="delegation-search-first"
      className={cn(CARD_SURFACE, className)}
    >
      <CardHeader Icon={SearchIcon} tone="muted" title={SEARCH_FIRST_COPY.title} titleId={titleId} />
      <div className="space-y-4 p-5">
        <p className="text-sm leading-5 wrap-break-word text-[#6b6b6b]">{SEARCH_FIRST_COPY.body}</p>
        {onGoToChat && (
          <button
            type="button"
            onClick={onGoToChat}
            className={cn(pillButton({ tone: "ink", size: "lg" }), "w-full")}
          >
            <MessageCircleIcon aria-hidden />
            {SEARCH_FIRST_COPY.action}
          </button>
        )}
      </div>
    </section>
  )
}
