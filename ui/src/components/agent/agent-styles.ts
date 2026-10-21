import { cva } from "class-variance-authority"

// Superficies compartidas: tinta #0d0d0d sobre blanco, grafito #6b6b6b (5.3:1) para texto
// secundario y bordes #ececec, igual que el hilo del chat.

/** Anillo sólido: el `ring-black/20` del resto de la app no llega a 3:1 sobre blanco. */
export const focusRing =
  "outline-none focus-visible:ring-2 focus-visible:ring-[#0d0d0d] focus-visible:ring-offset-2 focus-visible:ring-offset-white"

/**
 * Total destacado de una tarjeta `@container` (aprobación HP, consentimiento HNP). La fuente es la
 * del sistema: a 2rem, «$ 2.000.000,00» mide 219 px en macOS y 252 con DejaVu Sans (Chromium en
 * Linux), y en un teléfono de 320 px la tarjeta deja 230 px o menos. Bajo 20rem de tarjeta el
 * tamaño baja con ella (10cqi); un monto que aun así no cabe se parte antes de salirse del padding.
 */
export const totalAmount =
  "text-[length:clamp(1.5rem,10cqi,2rem)] leading-10 font-semibold tracking-[-0.04em] tabular-nums wrap-anywhere"

export const pillButton = cva(
  `inline-flex shrink-0 items-center justify-center gap-2 rounded-full text-sm font-medium whitespace-nowrap transition-[background-color,opacity,transform] select-none active:translate-y-px disabled:pointer-events-none disabled:opacity-55 aria-disabled:pointer-events-none aria-disabled:opacity-55 [&_svg]:size-4 [&_svg]:shrink-0 ${focusRing}`,
  {
    variants: {
      tone: {
        ink: "bg-[#0d0d0d] text-white hover:bg-[#0d0d0d]/85",
        soft: "bg-[#f2f2f2] text-[#0d0d0d] hover:bg-[#e8e8e8]",
        outline: "border border-[#e2e2e2] bg-white text-[#0d0d0d] hover:bg-[#f7f7f7]",
        ghost: "text-[#0d0d0d] hover:bg-[#f2f2f2]",
      },
      size: {
        md: "h-11 px-4",
        lg: "h-12 px-5",
        icon: "size-11",
      },
    },
    defaultVariants: { tone: "ink", size: "md" },
  },
)

export const chip = cva(
  "inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-full text-xs leading-5",
  {
    variants: {
      tone: {
        neutral: "bg-[#f2f2f2] text-[#0d0d0d]",
        outline: "border border-[#e2e2e2] bg-white text-[#0d0d0d]",
        soft: "border border-dashed border-[#a3a3a3] bg-white text-[#0d0d0d]",
        success: "bg-emerald-50 text-emerald-800",
        warning: "bg-amber-50 text-amber-900",
        danger: "bg-red-50 text-red-700",
      },
      size: {
        sm: "px-2 py-0.5",
        md: "px-2.5 py-1",
      },
    },
    defaultVariants: { tone: "neutral", size: "md" },
  },
)
