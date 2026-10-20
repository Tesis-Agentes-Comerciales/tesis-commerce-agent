import type { VariantProps } from "class-variance-authority"
import { cn } from "cn"
import {
  BanIcon,
  CheckIcon,
  CircleAlertIcon,
  CircleHelpIcon,
  ClockIcon,
  LoaderCircleIcon,
  ShieldXIcon,
  type LucideIcon,
} from "lucide-react"

import { chip } from "@/components/agent/agent-styles"
import { PURCHASE_STATUS_LABEL } from "@/components/trace/trace-copy"
import type { PurchaseStatus } from "@/lib/trace-contract"

type ChipTone = NonNullable<VariantProps<typeof chip>["tone"]>

// Icono y texto siempre acompañan al tono: el estado nunca se lee solo por el color.
const STATUS_VIEW: Record<PurchaseStatus, { tone: ChipTone; Icon: LucideIcon }> = {
  in_progress: { tone: "neutral", Icon: LoaderCircleIcon },
  awaiting_approval: { tone: "outline", Icon: ClockIcon },
  completed: { tone: "success", Icon: CheckIcon },
  blocked: { tone: "warning", Icon: ShieldXIcon },
  canceled: { tone: "neutral", Icon: BanIcon },
  failed: { tone: "danger", Icon: CircleAlertIcon },
  uncertain: { tone: "warning", Icon: CircleHelpIcon },
}

export type PurchaseStatusLabelProps = {
  status: PurchaseStatus
  className?: string
}

export function PurchaseStatusLabel({ status, className }: PurchaseStatusLabelProps) {
  const { tone, Icon } = STATUS_VIEW[status]
  return (
    <span className={cn(chip({ tone, size: "sm" }), "font-medium", className)} data-status={status}>
      <Icon
        className={cn("size-3.5 shrink-0", status === "in_progress" && "motion-safe:animate-spin")}
        aria-hidden
      />
      {PURCHASE_STATUS_LABEL[status]}
    </span>
  )
}
