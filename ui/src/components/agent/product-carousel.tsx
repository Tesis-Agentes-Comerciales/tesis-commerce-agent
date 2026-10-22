import { cn } from "cn"
import { ChevronLeftIcon, ChevronRightIcon, SearchXIcon } from "lucide-react"
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from "react"

import { pluralize, summarizeRejections } from "@/components/agent/agent-format"
import { focusRing, pillButton } from "@/components/agent/agent-styles"
import { ProductCard, ProductCardSkeleton } from "@/components/agent/product-card"
import type { ProductCard as ProductCardData } from "@/lib/agent-contract"

import "./agent.css"

const listFormat = new Intl.ListFormat("es", { style: "long", type: "conjunction" })

function prefersReducedMotion() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false
}

/** "3 fuera del presupuesto, 2 sin disponibilidad y 1 de otra categoría". */
function RejectionSummaryText({ rejections }: { rejections: Record<string, number> }) {
  const summary = summarizeRejections(rejections)
  if (summary.total === 0) return null
  return (
    <p className="text-sm leading-6 text-[#6b6b6b]">
      Descarté {pluralize(summary.total, "producto", "productos")}:{" "}
      {listFormat.format(summary.items.map((item) => `${item.count} ${item.label}`))}.
    </p>
  )
}

export type ProductCarouselProps = {
  /** `result.products` de `search_catalog` (máximo 6). */
  products: ProductCardData[]
  /** `result.matches`: puede superar a los productos mostrados. */
  matches: number
  rejections?: Record<string, number>
  /** Resalta la variante que eligió `select_product`. */
  selectedVariantId?: string | null
  /** Muestra marcadores mientras la búsqueda sigue en curso. */
  loading?: boolean
  /** El título se mantiene para lectores de pantalla; el paso que lo envuelve ya lo nombra. */
  quietHeading?: boolean
  className?: string
}

export function ProductCarousel({
  products,
  matches,
  rejections = {},
  selectedVariantId,
  loading = false,
  quietHeading = false,
  className,
}: ProductCarouselProps) {
  const headingId = useId()
  const scrollerId = useId()
  const scroller = useRef<HTMLDivElement>(null)
  const [edges, setEdges] = useState({ start: true, end: true })

  const updateEdges = useCallback(() => {
    const element = scroller.current
    if (!element) return
    const max = element.scrollWidth - element.clientWidth
    setEdges({ start: element.scrollLeft <= 1, end: element.scrollLeft >= max - 1 })
  }, [])

  useEffect(() => {
    const element = scroller.current
    if (!element) return
    updateEdges()
    const observer = new ResizeObserver(updateEdges)
    observer.observe(element)
    return () => observer.disconnect()
  }, [updateEdges, products.length, loading])

  const scrollByCard = (direction: -1 | 1) => {
    const element = scroller.current
    const card = element?.querySelector<HTMLElement>("[data-carousel-item]")
    if (!element || !card) return
    const gap = Number.parseFloat(getComputedStyle(card.parentElement ?? card).columnGap) || 12
    element.scrollBy({
      left: direction * (card.offsetWidth + gap),
      behavior: prefersReducedMotion() ? "auto" : "smooth",
    })
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const element = scroller.current
    if (!element) return
    const keys: Record<string, () => void> = {
      ArrowRight: () => scrollByCard(1),
      ArrowLeft: () => scrollByCard(-1),
      Home: () => element.scrollTo({ left: 0 }),
      End: () => element.scrollTo({ left: element.scrollWidth }),
    }
    const action = keys[event.key]
    if (!action) return
    event.preventDefault()
    action()
  }

  if (!loading && products.length === 0) {
    return (
      <section
        className={cn("rounded-[24px] border border-dashed border-[#d4d4d4] p-5", className)}
        aria-labelledby={headingId}
        data-testid="product-carousel-empty"
      >
        <div className="flex items-start gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-[#f2f2f2]">
            <SearchXIcon className="size-5" aria-hidden />
          </span>
          <div className="min-w-0">
            <h3 id={headingId} className="text-sm leading-6 font-semibold">
              Ningún producto cumple todo lo que pediste
            </h3>
            <RejectionSummaryText rejections={rejections} />
          </div>
        </div>
      </section>
    )
  }

  const shown = products.length
  const scrollable = !(edges.start && edges.end)

  return (
    <section className={cn("min-w-0", className)} aria-labelledby={headingId}>
      <div className="flex items-end justify-between gap-3 px-1">
        <div className="min-w-0">
          <h3
            id={headingId}
            className={cn("text-sm leading-6 font-semibold", quietHeading && "sr-only")}
          >
            {loading
              ? "Buscando en el catálogo…"
              : pluralize(matches, "coincidencia", "coincidencias")}
            {!loading && matches > shown && (
              <span className="font-normal text-[#6b6b6b]"> (muestro {shown})</span>
            )}
          </h3>
          {!loading && <RejectionSummaryText rejections={rejections} />}
        </div>
        {scrollable && (
          <div className="hidden shrink-0 gap-1.5 sm:flex">
            <button
              type="button"
              onClick={() => scrollByCard(-1)}
              disabled={edges.start}
              aria-controls={scrollerId}
              aria-label="Ver productos anteriores"
              className={pillButton({ tone: "outline", size: "icon" })}
            >
              <ChevronLeftIcon aria-hidden />
            </button>
            <button
              type="button"
              onClick={() => scrollByCard(1)}
              disabled={edges.end}
              aria-controls={scrollerId}
              aria-label="Ver más productos"
              className={pillButton({ tone: "outline", size: "icon" })}
            >
              <ChevronRightIcon aria-hidden />
            </button>
          </div>
        )}
      </div>

      <div
        ref={scroller}
        id={scrollerId}
        // Grupo y no región: varias búsquedas en el hilo repetirían el mismo landmark.
        role="group"
        aria-roledescription="carrusel"
        aria-label="Resultados del catálogo"
        aria-busy={loading || undefined}
        tabIndex={scrollable ? 0 : undefined}
        onScroll={updateEdges}
        onKeyDown={onKeyDown}
        className={cn(
          "agent-scroller -mx-1 mt-3 snap-x snap-mandatory scroll-px-1 overflow-x-auto overscroll-x-contain rounded-[16px] px-1 pt-1 pb-3",
          focusRing,
        )}
      >
        {/* role="list": Safari quita la semántica de lista cuando no hay viñetas. */}
        <ul role="list" className="flex gap-3">
          {loading
            ? Array.from({ length: 3 }, (_, index) => (
                <li key={index} data-carousel-item className="w-[min(14rem,68vw)] shrink-0 snap-start">
                  <ProductCardSkeleton />
                </li>
              ))
            : products.map((product) => (
                <li
                  key={product.variant_id}
                  data-carousel-item
                  className="w-[min(14rem,68vw)] shrink-0 snap-start"
                >
                  <ProductCard
                    product={product}
                    variant="compact"
                    headingLevel={4}
                    selected={product.variant_id === selectedVariantId}
                  />
                </li>
              ))}
        </ul>
      </div>
    </section>
  )
}
