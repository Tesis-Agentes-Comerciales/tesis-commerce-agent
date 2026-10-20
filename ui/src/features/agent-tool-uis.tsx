import { useAuiState, type ToolCallMessagePartProps } from "@assistant-ui/react"

import {
  InterpretRequestStep,
  PrepareCheckoutStep,
  ProductCard,
  ProductCardSkeleton,
  ProductCarousel,
  PurchaseApprovalCardSkeleton,
  PurchaseUpdateStep,
  SearchCatalogStep,
  StepRow,
  toolErrorMessage,
  toolStepLabel,
  toolStepStatus,
} from "@/components/agent"
import {
  asApprovalResult,
  asInterpretResult,
  asPrepareResult,
  asProduct,
  asPurchaseUpdateResult,
  asSearchResult,
  asSelectResult,
} from "@/features/agent-results"
import { PurchaseApproval, StaleTermsApproval } from "@/features/purchase-approval"
import { staleApprovalOf } from "@/features/stale-terms"
import type { AgentMessageStatus, AgentToolName } from "@/lib/agent-contract"

type StepProps = ToolCallMessagePartProps<Record<string, unknown>, unknown>

/** Estado visual del paso; sin `result` y con el mensaje cerrado, el paso quedó interrumpido. */
function useStepStatus(part: StepProps) {
  const messageStatus = useAuiState((state) => state.message.status)
  return toolStepStatus(part, messageStatus as AgentMessageStatus | undefined)
}

/** `result` de otro paso del mismo mensaje (por ejemplo, el producto elegido). */
function useSiblingResult(toolName: AgentToolName) {
  return useAuiState((state) => {
    const part = state.message.parts.find(
      (item) => item.type === "tool-call" && item.toolName === toolName && !item.isError,
    )
    return part?.type === "tool-call" ? part.result : undefined
  })
}

function successfulResult(part: StepProps) {
  return part.isError ? undefined : part.result
}

export function InterpretRequestTool(part: StepProps) {
  const status = useStepStatus(part)
  return (
    <InterpretRequestStep
      status={status}
      result={asInterpretResult(successfulResult(part))}
      error={toolErrorMessage(part)}
    />
  )
}

export function SearchCatalogTool(part: StepProps) {
  const status = useStepStatus(part)
  const result = asSearchResult(successfulResult(part))
  const selected = asSelectResult(useSiblingResult("select_product"))
  return (
    <SearchCatalogStep status={status} result={result} error={toolErrorMessage(part)}>
      {status === "running" ? (
        // El paso ya se titula "Buscando en el catálogo…": el carrusel no lo repite en pantalla.
        <ProductCarousel products={[]} matches={0} loading quietHeading />
      ) : result && result.products.length > 0 ? (
        <ProductCarousel
          products={result.products}
          matches={result.matches}
          selectedVariantId={selected?.product?.variant_id ?? null}
        />
      ) : null}
    </SearchCatalogStep>
  )
}

export function PrepareCheckoutTool(part: StepProps) {
  const status = useStepStatus(part)
  return (
    <PrepareCheckoutStep
      status={status}
      result={asPrepareResult(successfulResult(part))}
      error={toolErrorMessage(part)}
    />
  )
}

export function PurchaseUpdateTool(part: StepProps) {
  const status = useStepStatus(part)
  return (
    <PurchaseUpdateStep
      status={status}
      result={asPurchaseUpdateResult(successfulResult(part))}
      error={toolErrorMessage(part)}
    />
  )
}

/** Error de un paso destacado; `StepRow` es un `<li>`, así que va en su propia lista. */
function ProminentStepError({ toolName, part }: { toolName: AgentToolName; part: StepProps }) {
  return (
    <ol aria-label="Paso con error">
      <StepRow status="error" title={toolStepLabel(toolName, "error")} error={toolErrorMessage(part)} />
    </ol>
  )
}

export function SelectProductTool(part: StepProps) {
  const status = useStepStatus(part)
  if (status === "running") return <ProductCardSkeleton />
  if (status === "error") return <ProminentStepError toolName="select_product" part={part} />
  const result = asSelectResult(part.result)
  // Con `clarify` no hay producto: el texto final pide los detalles que faltan.
  if (result?.decision !== "select" || !result.product) return null
  // Sin `rationale`: el texto final del turno ya dice por qué se eligió, y repetirlo en la tarjeta
  // hacía leer la misma frase dos veces en el mismo mensaje.
  return <ProductCard product={result.product} />
}

export function RequestApprovalTool(part: StepProps) {
  const status = useStepStatus(part)
  const selected = asSelectResult(useSiblingResult("select_product"))
  const prepared = asPrepareResult(useSiblingResult("prepare_checkout"))
  if (status === "running") return <PurchaseApprovalCardSkeleton />
  // Términos que ya no valen: la compra sigue pendiente y la tarjeta ofrece cancelarla.
  const stale = staleApprovalOf(part)
  if (stale) {
    return (
      <StaleTermsApproval
        runId={stale.runId}
        terms={stale.terms}
        product={asProduct(selected?.product) ?? null}
      />
    )
  }
  if (status === "error") return <ProminentStepError toolName="request_approval" part={part} />
  const result = asApprovalResult(part.result)
  if (!result) return <ProminentStepError toolName="request_approval" part={part} />
  return (
    <PurchaseApproval
      runId={result.run_id}
      terms={result.terms}
      product={asProduct(selected?.product) ?? null}
      priceChanged={prepared?.run_id === result.run_id && prepared.price_changed}
    />
  )
}
