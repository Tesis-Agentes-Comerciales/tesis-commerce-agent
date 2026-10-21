// Tarjetas de delegación Human Not Present (docs/design/hnp-governed.md §3.2, §5.3 y §8). Son de
// presentación: reciben las vistas de `delegation-contract.ts` por props y avisan acciones por
// callbacks. El cableado con la API es de `src/features/`.

export {
  DelegationConsentCard,
  type DelegationConsent,
  type DelegationConsentCardProps,
} from "@/components/delegation/delegation-consent-card"
export {
  DelegationEscalationCard,
  type DelegationEscalationCardProps,
} from "@/components/delegation/delegation-escalation-card"
export {
  DelegationRevokeReceipt,
  type DelegationRevokeReceiptProps,
} from "@/components/delegation/delegation-revoke-receipt"
export {
  DelegationStatusCard,
  type DelegationStatusCardProps,
} from "@/components/delegation/delegation-status-card"
