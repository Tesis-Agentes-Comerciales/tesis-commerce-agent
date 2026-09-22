import { cn } from "cn"
import { useLayoutEffect, useRef, useState, type ReactNode } from "react"

import { focusRing } from "@/components/agent/agent-styles"

import "./agent.css"

const PIN_THRESHOLD = 24

/** Los resúmenes de la Responses API usan `**título**`; se muestran sin interpretar HTML. */
function inline(text: string) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((segment, index) =>
    segment.startsWith("**") && segment.endsWith("**") && segment.length > 4 ? (
      <strong key={index} className="font-medium text-[#0d0d0d]">
        {segment.slice(2, -2)}
      </strong>
    ) : (
      segment
    ),
  )
}

function Paragraph({ text, caret }: { text: string; caret: ReactNode }) {
  const heading = /^\*\*([^*]+)\*\*$/.exec(text.trim())
  if (heading) {
    return (
      <p className="font-medium text-[#0d0d0d]">
        {heading[1]}
        {caret}
      </p>
    )
  }
  return (
    <p className="whitespace-pre-wrap">
      {inline(text)}
      {caret}
    </p>
  )
}

export type ReasoningTextProps = {
  text: string
  /** Mientras llega texto se fija al final, salvo que la persona haya subido. */
  streaming?: boolean
  className?: string
}

export function ReasoningText({ text, streaming = false, className }: ReasoningTextProps) {
  const container = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  // Solo es foco de teclado si de verdad desplaza; si no, sería una parada de tabulación vacía.
  const [scrollable, setScrollable] = useState(false)
  const paragraphs = text.split(/\n{2,}/).filter((paragraph) => paragraph.trim().length > 0)

  useLayoutEffect(() => {
    const element = container.current
    if (!element) return
    setScrollable(element.scrollHeight > element.clientHeight + 1)
    if (streaming && pinned.current) element.scrollTop = element.scrollHeight
  }, [text, streaming])

  const onScroll = () => {
    const element = container.current
    if (!element) return
    pinned.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < PIN_THRESHOLD
  }

  const caret = streaming ? (
    <span
      className="agent-caret ml-0.5 inline-block h-[1.1em] w-[0.45em] translate-y-[0.2em] rounded-[1px] bg-[#0d0d0d]/70"
      aria-hidden
    />
  ) : null

  return (
    <div
      ref={container}
      onScroll={onScroll}
      tabIndex={scrollable ? 0 : undefined}
      role={scrollable ? "group" : undefined}
      aria-label={scrollable ? "Resumen del razonamiento" : undefined}
      aria-busy={streaming || undefined}
      className={cn(
        "max-h-56 space-y-2.5 overflow-y-auto overscroll-contain rounded-[10px] text-sm leading-6 text-[#525252] wrap-break-word",
        focusRing,
        className,
      )}
    >
      {paragraphs.length === 0
        ? caret
        : paragraphs.map((paragraph, index) => (
            <Paragraph
              key={index}
              text={paragraph}
              caret={index === paragraphs.length - 1 ? caret : null}
            />
          ))}
    </div>
  )
}
