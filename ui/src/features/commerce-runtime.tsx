import {
  AssistantRuntimeProvider,
  useLocalRuntime,
  type ChatModelAdapter,
  type ThreadMessage,
} from "@assistant-ui/react"
import { useMemo, type ReactNode } from "react"

import {
  AgentApiError,
  getApprovalTerms,
  routeConversation,
  startPurchase,
} from "@/lib/commerce-api"
import type {
  ApprovalTerms,
  BuyerProfile,
  ConversationMessage,
  PurchaseFlowStatus,
  PurchasePreparation,
} from "@/lib/commerce-types"

type CommerceRuntimeProps = {
  children: ReactNode
  profile: BuyerProfile
  flowStatus: PurchaseFlowStatus
  onPreparing: (request: string) => void
  onPrepared: (
    request: string,
    preparation: PurchasePreparation,
    terms: ApprovalTerms,
  ) => void
  onError: (request: string, message: string) => void
}

function latestUserText(messages: readonly ThreadMessage[]): string {
  const message = [...messages].reverse().find((item) => item.role === "user")
  if (!message) return ""
  return message.content
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("\n")
    .trim()
}

function conversationContext(messages: readonly ThreadMessage[]): ConversationMessage[] {
  return messages
    .filter(
      (message): message is ThreadMessage & { role: "user" | "assistant" } =>
        message.role === "user" || message.role === "assistant",
    )
    .map((message) => ({
      role: message.role,
      content: message.content
        .filter((part) => part.type === "text")
        .map((part) => part.text)
        .join("\n")
        .trim(),
    }))
    .filter((message) => message.content.length > 0)
    .slice(-20)
}

function errorMessage(error: unknown): string {
  if (error instanceof AgentApiError) {
    const known: Record<string, string> = {
      no_candidate: "No encontré un producto que cumpla esas condiciones.",
      budget_exceeded: "El total autoritativo del comercio supera tu presupuesto.",
      intent_model_error: "No pude interpretar la solicitud con el modelo en este momento.",
      transport_error: "No pude comunicarme con el ecommerce ACP.",
      timeout: "El ecommerce tardó demasiado en responder.",
      INVALID_INPUT:
        "El ecommerce rechazó los datos del checkout. Revisa tus datos de entrega.",
      http_error: "El ecommerce no pudo procesar la solicitud.",
    }
    return known[error.code] ?? error.message
  }
  return error instanceof Error ? error.message : "Ocurrió un error inesperado."
}

export function CommerceRuntime({
  children,
  profile,
  flowStatus,
  onPreparing,
  onPrepared,
  onError,
}: CommerceRuntimeProps) {
  const adapter = useMemo<ChatModelAdapter>(
    () => ({
      async run({ messages, abortSignal }) {
        const request = latestUserText(messages)
        if (!request) {
          return {
            content: [{ type: "text", text: "Escribe qué producto quieres comprar." }],
          }
        }

        try {
          const decision = await routeConversation(conversationContext(messages), abortSignal)
          if (decision.action !== "purchase") {
            return {
              content: [{ type: "text", text: decision.assistant_message }],
            }
          }
          if (["awaiting_approval", "approving", "completing"].includes(flowStatus)) {
            return {
              content: [
                {
                  type: "text",
                  text: "Ya hay una compra pendiente. Revísala antes de iniciar otra.",
                },
              ],
            }
          }
          if (!decision.intent) {
            throw new Error("El modelo no produjo una intención de compra válida.")
          }

          onPreparing(request)
          const preparation = await startPurchase(
            decision.purchase_request ?? request,
            profile,
            abortSignal,
            decision.intent,
          )
          if (preparation.stage !== "awaiting_confirmation") {
            throw new Error(`La compra quedó en un estado inesperado: ${preparation.stage}`)
          }
          const terms = await getApprovalTerms(preparation.run_id, abortSignal)
          onPrepared(request, preparation, terms)
          const priceNote = preparation.price_changed
            ? " El precio cambió frente al feed; usé el total actual del checkout."
            : ""
          return {
            content: [
              {
                type: "text",
                text: `Encontré **${preparation.selected_candidate.product_title}** y preparé el checkout ACP.${priceNote}\n\nRevisa los términos y decide si quieres aprobar la compra.`,
              },
            ],
          }
        } catch (error) {
          if (abortSignal.aborted) throw error
          const message = errorMessage(error)
          onError(request, message)
          return {
            content: [
              {
                type: "text",
                text: `No pude preparar la compra: ${message}`,
              },
            ],
          }
        }
      },
    }),
    [flowStatus, onError, onPrepared, onPreparing, profile],
  )

  const runtime = useLocalRuntime(adapter)

  return <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
}
