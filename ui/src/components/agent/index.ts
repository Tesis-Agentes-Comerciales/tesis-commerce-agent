// Componentes de presentación del agente. No leen runtime ni hacen peticiones: reciben datos del
// contrato (docs/agent-ui-contract.md) por props y avisan acciones por callbacks.

export { AgentPlan, type AgentPlanProps } from "@/components/agent/agent-plan"
export { chip, focusRing, pillButton } from "@/components/agent/agent-styles"
export {
  derivePlanProgress,
  planStepStates,
  PLAN_STEPS,
  toolErrorMessage,
  toolResult,
  toolStepLabel,
  toolStepStatus,
  type PlanProgress,
  type PlanStepId,
  type PlanStepState,
  type StepStatus,
} from "@/components/agent/agent-steps"
export {
  ProductCard,
  ProductCardSkeleton,
  ProductImage,
  type ProductCardProps,
} from "@/components/agent/product-card"
export { ProductCarousel, type ProductCarouselProps } from "@/components/agent/product-carousel"
export {
  PurchaseApprovalCard,
  PurchaseApprovalCardSkeleton,
  type ApprovalCardOrder,
  type ApprovalCardStatus,
  type PurchaseApprovalCardProps,
} from "@/components/agent/purchase-approval-card"
export { ReasoningText, type ReasoningTextProps } from "@/components/agent/reasoning-text"
export { ThinkingGroup, type ThinkingGroupProps } from "@/components/agent/thinking-group"
export {
  ThinkingIndicator,
  type ThinkingIndicatorProps,
} from "@/components/agent/thinking-indicator"
export {
  InterpretRequestStep,
  PrepareCheckoutStep,
  PurchaseUpdateStep,
  ReasoningStep,
  SearchCatalogStep,
  StepRow,
  type StepRowProps,
} from "@/components/agent/tool-steps"
export { useElapsedMs } from "@/components/agent/use-elapsed"
