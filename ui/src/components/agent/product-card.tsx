import { cn } from "cn"
import { CheckIcon, ExternalLinkIcon, ShoppingBagIcon } from "lucide-react"
import { useCallback, useId, useState, type CSSProperties } from "react"

import {
  formatMoney,
  formatMoneyParts,
  placeholderHue,
  safeImageUrl,
  safeLinkUrl,
} from "@/components/agent/agent-format"
import { chip, focusRing, pillButton } from "@/components/agent/agent-styles"
import type {
  ProductCard as ProductCardData,
  ProductImage as ProductImageData,
} from "@/lib/agent-contract"

import "./agent.css"

const LONG_DESCRIPTION = 180

type ProductImageProps = {
  image: ProductImageData | null | undefined
  /** Texto alternativo cuando el feed no trae uno. */
  fallbackAlt: string
  /** Semilla estable (id del producto) para el tono del placeholder. */
  seed: string
  /** Muestra el texto "Sin imagen" en el placeholder; se omite en miniaturas. */
  caption?: boolean
  className?: string
}

/**
 * Imagen del feed con placeholder. Los hosts del feed de pruebas no resuelven, así que una URL
 * ausente, no permitida o rota muestra el placeholder en vez de un ícono roto.
 */
export function ProductImage({ image, fallbackAlt, seed, caption = true, className }: ProductImageProps) {
  const src = safeImageUrl(image?.url)
  const alt = image?.alt?.trim() || fallbackAlt

  return (
    <div className={cn("relative overflow-hidden bg-[#f7f7f7]", className)}>
      {src ? (
        <LoadedImage key={src} src={src} alt={alt} seed={seed} caption={caption} />
      ) : (
        <ImagePlaceholder seed={seed} caption={caption} />
      )}
    </div>
  )
}

function LoadedImage({
  src,
  alt,
  seed,
  caption,
}: {
  src: string
  alt: string
  seed: string
  caption: boolean
}) {
  const [state, setState] = useState<"loading" | "loaded" | "error">("loading")
  // Una imagen en caché puede terminar antes de que React escuche `load`.
  const measure = useCallback((element: HTMLImageElement | null) => {
    if (!element?.complete) return
    setState(element.naturalWidth > 0 ? "loaded" : "error")
  }, [])

  if (state === "error") return <ImagePlaceholder seed={seed} caption={caption} />

  return (
    <>
      {state === "loading" && <div className="agent-skeleton absolute inset-0" aria-hidden />}
      <img
        ref={measure}
        src={src}
        alt={alt}
        loading="lazy"
        decoding="async"
        referrerPolicy="no-referrer"
        onLoad={() => setState("loaded")}
        onError={() => setState("error")}
        className={cn(
          "size-full object-contain mix-blend-multiply transition-opacity duration-300",
          state === "loaded" ? "opacity-100" : "opacity-0",
        )}
      />
    </>
  )
}

function ImagePlaceholder({ seed, caption }: { seed: string; caption: boolean }) {
  const hue = placeholderHue(seed)
  const style = {
    background: `radial-gradient(120% 90% at 28% 18%, oklch(0.99 0.01 ${hue}), oklch(0.94 0.03 ${hue}) 70%)`,
    color: `oklch(0.42 0.06 ${hue})`,
  } satisfies CSSProperties

  return (
    <div
      className="flex size-full flex-col items-center justify-center gap-2"
      style={style}
      data-testid="product-image-placeholder"
    >
      <span className="flex size-[min(3.5rem,40%)] items-center justify-center rounded-full bg-white/75 ring-1 ring-black/5">
        <ShoppingBagIcon className="size-1/2" strokeWidth={1.6} aria-hidden />
      </span>
      {caption && <span className="text-xs font-medium">Sin imagen del comercio</span>}
    </div>
  )
}

function AvailabilityBadge({ available, className }: { available: boolean; className?: string }) {
  return (
    <span
      className={cn(
        chip({ tone: available ? "success" : "neutral", size: "sm" }),
        "font-medium shadow-[0_1px_2px_rgba(0,0,0,0.06)]",
        !available && "text-[#525252]",
        className,
      )}
    >
      <span
        className={cn("size-1.5 rounded-full", available ? "bg-emerald-600" : "bg-[#8e8e8e]")}
        aria-hidden
      />
      {available ? "Disponible" : "Agotado"}
    </span>
  )
}

function CatalogPrice({
  amountMinor,
  currency,
  size,
}: {
  amountMinor: number
  currency: string
  size: "hero" | "compact"
}) {
  const parts = formatMoneyParts(amountMinor, currency)

  if (size === "compact") {
    return (
      <p className="text-sm">
        <span className="font-semibold tabular-nums">{formatMoney(amountMinor, currency)}</span>
        <span className="block text-xs text-[#6b6b6b]">Precio de catálogo</span>
      </p>
    )
  }

  return (
    <div>
      <p className="flex flex-wrap items-baseline gap-x-1.5">
        <span className="text-2xl font-semibold tracking-[-0.03em] tabular-nums wrap-anywhere">
          {parts.amount}
        </span>
        <span className="text-xs font-medium text-[#6b6b6b]">{parts.code}</span>
      </p>
      <p className="mt-0.5 text-xs leading-5 text-[#6b6b6b]">
        Precio de catálogo. El total final aparece en la aprobación.
      </p>
    </div>
  )
}

function ProductLink({ href, className }: { href: string; className?: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={cn(pillButton({ tone: "outline" }), className)}
    >
      Ver en la tienda
      <ExternalLinkIcon aria-hidden />
      <span className="sr-only">(se abre en una pestaña nueva)</span>
    </a>
  )
}

function Description({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false)
  const id = useId()
  const long = text.length > LONG_DESCRIPTION

  return (
    <div>
      <p
        id={id}
        className={cn(
          "text-sm leading-6 text-[#3d3d3d] wrap-break-word",
          long && !expanded && "line-clamp-3",
        )}
      >
        {text}
      </p>
      {long && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          aria-controls={id}
          className={cn(
            "-mx-1 mt-0.5 inline-flex min-h-11 items-center rounded-[6px] px-1 text-sm font-medium underline-offset-4 hover:underline sm:min-h-8",
            focusRing,
          )}
        >
          {expanded ? "Mostrar menos" : "Mostrar más"}
        </button>
      )}
    </div>
  )
}

export type ProductCardProps = {
  product: ProductCardData
  variant?: "hero" | "compact"
  /** Explicación de `select_product`; solo se muestra en la variante destacada. */
  rationale?: string | null
  /** Marca la tarjeta como la opción elegida (carrusel). */
  selected?: boolean
  /** Nivel del título dentro del documento. */
  headingLevel?: 2 | 3 | 4
  className?: string
}

export function ProductCard({
  product,
  variant = "hero",
  rationale,
  selected = false,
  headingLevel = 3,
  className,
}: ProductCardProps) {
  const Heading = `h${headingLevel}` as const
  const href = safeLinkUrl(product.product_url)
  const options = product.options.filter((option) => option.name && option.value)
  const description = product.description?.trim()
  const reason = rationale?.trim()

  if (variant === "compact") {
    return (
      <article
        className={cn(
          "group relative flex h-full flex-col overflow-hidden rounded-[16px] border bg-white transition-shadow",
          selected ? "border-[#0d0d0d] ring-1 ring-[#0d0d0d]" : "border-[#ececec]",
          href && "hover:shadow-[0_6px_20px_-10px_rgba(0,0,0,0.25)]",
          // El enlace del título cubre la tarjeta; el foco se dibuja en la tarjeta completa.
          "has-[a:focus-visible]:ring-2 has-[a:focus-visible]:ring-[#0d0d0d] has-[a:focus-visible]:ring-offset-2",
          className,
        )}
        data-testid="product-card-compact"
      >
        <div className="relative">
          <ProductImage
            image={product.image}
            fallbackAlt={product.title}
            seed={product.product_id}
            caption={false}
            className="aspect-square"
          />
          <div className="absolute top-2 left-2 flex flex-wrap gap-1.5">
            <AvailabilityBadge available={product.available} />
          </div>
          {selected && (
            <span
              className={cn(
                chip({ tone: "neutral", size: "sm" }),
                "absolute top-2 right-2 bg-[#0d0d0d] font-medium text-white",
              )}
            >
              <CheckIcon className="size-3" aria-hidden />
              Elegido
            </span>
          )}
        </div>
        <div className="flex flex-1 flex-col gap-1 p-3">
          <Heading className="line-clamp-2 text-sm leading-5 font-medium">
            {href ? (
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="outline-none after:absolute after:inset-0 after:content-['']"
              >
                {product.title}
                <span className="sr-only"> (se abre en una pestaña nueva)</span>
              </a>
            ) : (
              product.title
            )}
          </Heading>
          <p className="truncate text-xs text-[#6b6b6b]">{product.variant_title}</p>
          <div className="mt-auto pt-2">
            <CatalogPrice
              amountMinor={product.observed_unit_amount_minor}
              currency={product.currency}
              size="compact"
            />
          </div>
        </div>
      </article>
    )
  }

  return (
    <article
      className={cn(
        "@container overflow-hidden rounded-[24px] border border-[#ececec] bg-white shadow-[0_1px_2px_rgba(0,0,0,0.04),0_12px_32px_-18px_rgba(0,0,0,0.18)]",
        className,
      )}
      data-testid="product-card-hero"
    >
      <div className="grid @lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        <div className="relative">
          <ProductImage
            image={product.image}
            fallbackAlt={product.title}
            seed={product.product_id}
            className="aspect-[4/3] @lg:aspect-auto @lg:h-full @lg:min-h-72"
          />
          <AvailabilityBadge available={product.available} className="absolute top-3 left-3" />
        </div>

        <div className="flex min-w-0 flex-col gap-4 p-5 @lg:p-6">
          <div className="min-w-0">
            <Heading className="text-lg leading-6 font-semibold tracking-[-0.015em] wrap-break-word">
              {product.title}
            </Heading>
            <p className="mt-1 text-sm text-[#6b6b6b] wrap-break-word">{product.variant_title}</p>
          </div>

          {options.length > 0 && (
            <ul className="flex flex-wrap gap-1.5" aria-label="Opciones de la variante">
              {options.map((option) => (
                <li key={`${option.name}:${option.value}`} className={chip({ tone: "outline" })}>
                  <span className="text-[#6b6b6b]">{option.name}</span>
                  <span className="truncate font-medium">{option.value}</span>
                </li>
              ))}
            </ul>
          )}

          <CatalogPrice
            amountMinor={product.observed_unit_amount_minor}
            currency={product.currency}
            size="hero"
          />

          {description && <Description text={description} />}

          {reason && (
            <div className="rounded-[16px] bg-[#f7f7f7] p-4">
              <p className="text-xs font-semibold text-[#0d0d0d]">Por qué lo elegí</p>
              <p className="mt-1 text-sm leading-6 text-[#3d3d3d] wrap-break-word">{reason}</p>
            </div>
          )}

          {href && (
            <div className="mt-auto pt-1">
              <ProductLink href={href} className="w-full @sm:w-auto" />
            </div>
          )}
        </div>
      </div>
    </article>
  )
}

/** Marcador mientras `select_product` o `search_catalog` siguen en curso. */
export function ProductCardSkeleton({
  variant = "hero",
  className,
}: {
  variant?: "hero" | "compact"
  className?: string
}) {
  if (variant === "compact") {
    return (
      <div
        className={cn("overflow-hidden rounded-[16px] border border-[#ececec] bg-white", className)}
        aria-hidden
      >
        <div className="agent-skeleton aspect-square" />
        <div className="space-y-2 p-3">
          <div className="agent-skeleton h-3.5 w-4/5 rounded-full" />
          <div className="agent-skeleton h-3 w-1/2 rounded-full" />
          <div className="agent-skeleton mt-4 h-4 w-2/5 rounded-full" />
        </div>
      </div>
    )
  }

  return (
    <div
      className={cn(
        "@container overflow-hidden rounded-[24px] border border-[#ececec] bg-white",
        className,
      )}
      role="status"
      aria-label="Cargando la recomendación"
      aria-busy="true"
    >
      <div className="grid @lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        <div className="agent-skeleton aspect-[4/3] @lg:aspect-auto @lg:min-h-72" />
        <div className="space-y-3 p-5 @lg:p-6">
          <div className="agent-skeleton h-5 w-3/4 rounded-full" />
          <div className="agent-skeleton h-3.5 w-1/3 rounded-full" />
          <div className="flex gap-1.5 pt-2">
            <div className="agent-skeleton h-6 w-20 rounded-full" />
            <div className="agent-skeleton h-6 w-16 rounded-full" />
          </div>
          <div className="agent-skeleton mt-2 h-7 w-32 rounded-full" />
          <div className="agent-skeleton h-3 w-full rounded-full" />
          <div className="agent-skeleton h-3 w-11/12 rounded-full" />
        </div>
      </div>
    </div>
  )
}
