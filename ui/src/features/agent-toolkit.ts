import type { Toolkit } from "@assistant-ui/react"

import {
  InterpretRequestTool,
  PrepareCheckoutTool,
  PurchaseUpdateTool,
  RequestApprovalTool,
  SearchCatalogTool,
  SelectProductTool,
} from "@/features/agent-tool-uis"

/**
 * Renderers de los pasos que orquesta el backend. Son `backend`: no se exponen al modelo ni se
 * ejecutan en el navegador; solo dan UI a las partes `tool-call` del estado.
 */
export const agentToolkit = {
  interpret_request: { type: "backend", render: InterpretRequestTool },
  search_catalog: { type: "backend", render: SearchCatalogTool },
  select_product: { type: "backend", display: "standalone", render: SelectProductTool },
  prepare_checkout: { type: "backend", render: PrepareCheckoutTool },
  request_approval: { type: "backend", display: "standalone", render: RequestApprovalTool },
  purchase_update: { type: "backend", render: PurchaseUpdateTool },
} satisfies Toolkit

/** Pasos que se muestran destacados, fuera del grupo "Pensando…". */
export const PROMINENT_TOOLS = new Set<string>(["select_product", "request_approval"])
