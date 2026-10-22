import { cn } from "cn"
import { MessageCircleIcon, ShieldCheckIcon, type LucideIcon } from "lucide-react"
import { useId } from "react"

/**
 * Cómo compra el agente: con el comprador en el chat (Human Present, HP) o con una delegación que
 * el comprador autoriza antes de irse (Human Not Present, HNP; docs/design/hnp-governed.md).
 */
export type PurchaseMode = "present" | "delegated"

const PURCHASE_MODE_COPY = {
  label: "Modo de compra",
  present: "Comprar conmigo",
  delegated: "Delegar",
} as const

const MODES: { mode: PurchaseMode; Icon: LucideIcon }[] = [
  { mode: "present", Icon: MessageCircleIcon },
  { mode: "delegated", Icon: ShieldCheckIcon },
]

export type PurchaseModeSwitchProps = {
  mode: PurchaseMode
  onModeChange?: (mode: PurchaseMode) => void
  className?: string
}

/**
 * Selector segmentado de la cabecera. Son radios nativos: Tab entra al modo elegido y las flechas
 * cambian de modo, como cualquier grupo de radios. La app lo muestra solo si la API expone las
 * delegaciones (`hnp_enabled` en `/health`).
 */
export function PurchaseModeSwitch({ mode, onModeChange, className }: PurchaseModeSwitchProps) {
  const name = useId()
  return (
    <fieldset
      data-testid="purchase-mode-switch"
      className={cn("flex h-10 min-w-0 rounded-full bg-[#f2f2f2] p-1", className)}
    >
      <legend className="sr-only">{PURCHASE_MODE_COPY.label}</legend>
      {MODES.map(({ mode: option, Icon }) => (
        <label
          key={option}
          className={cn(
            "flex min-w-0 flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-full px-3 text-sm font-medium whitespace-nowrap text-[#6b6b6b] transition-colors select-none hover:text-[#0d0d0d] sm:flex-none",
            "has-checked:bg-white has-checked:text-[#0d0d0d] has-checked:shadow-[0_1px_2px_rgba(0,0,0,0.08)]",
            "has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-[#0d0d0d] has-[:focus-visible]:ring-offset-2 has-[:focus-visible]:ring-offset-[#f2f2f2]",
          )}
        >
          <input
            type="radio"
            name={name}
            value={option}
            checked={mode === option}
            onChange={() => onModeChange?.(option)}
            className="sr-only"
          />
          <Icon className="size-4 shrink-0" aria-hidden />
          {PURCHASE_MODE_COPY[option]}
        </label>
      ))}
    </fieldset>
  )
}
