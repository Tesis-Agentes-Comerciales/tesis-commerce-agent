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
  ProductChoiceMarker,
  ProductImage,
  type ProductCardProps,
  type ProductChoiceMarkerProps,
} from "@/components/agent/product-card"
export {
  PolicyBlockedCard,
  type PolicyBlockedCardProps,
  type PolicyBlockReason,
} from "@/components/agent/policy-blocked-card"
export { ProductCarousel, type ProductCarouselProps } from "@/components/agent/product-carousel"
export {
  PurchaseActivity,
  type PurchaseActivityProps,
} from "@/components/agent/purchase-activity"
export {
  PurchaseApprovalCard,
  PurchaseApprovalCardSkeleton,
  type ApprovalCardOrder,
  type ApprovalCardStatus,
  type ApprovalReceipt,
  type PurchaseApprovalCardProps,
} from "@/components/agent/purchase-approval-card"
export { ReasoningText, type ReasoningTextProps } from "@/components/agent/reasoning-text"
export {
  StaleTermsCard,
  type StaleTermsCardProps,
  type StaleTermsCardStatus,
  type StaleTermsReason,
} from "@/components/agent/stale-terms-card"
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
