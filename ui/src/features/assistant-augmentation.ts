import type { AgentState, PurchaseUpdatedCommand } from "@/lib/agent-contract"

// Tipos propios de Assistant Transport: el comando `purchase-updated` y el estado del backend que
// leen `useAssistantTransportSendCommand` y `useAssistantTransportState`.
declare module "@assistant-ui/react" {
  namespace Assistant {
    interface Commands {
      purchaseUpdated: PurchaseUpdatedCommand
    }

    interface ExternalState {
      agent: AgentState
    }
  }
}
