import type { ApprovalTerms } from "@/lib/commerce-types";

// Tipos del contrato Assistant Transport (docs/agent-ui-contract.md). Todo lo que llega del backend
// es no confiable: los componentes validan URLs y toleran campos ausentes antes de mostrarlos.

export type { ApprovalTerms };

/** Etapas de `PurchaseStage` leídas de SQLite; nunca se deducen del estado del cliente. */
export type PurchaseStage =
  | "received"
  | "selected"
  | "checkout_prepared"
  | "awaiting_confirmation"
  | "approved"
  | "completing"
  | "uncertain"
  | "completed"
  | "canceled"
  | "failed";

/** Estados de un checkout ACP. */
export type CheckoutStatus =
  | "not_ready_for_payment"
  | "ready_for_payment"
  | "completed"
  | "canceled"
  | "expired";

// Intención (JSON de `PurchaseIntent`). El backend normaliza textos a minúsculas.

export type OptionConstraint = {
  name: string;
  value: string;
};

export type HardConstraints = {
  currency: string;
  max_unit_amount_minor: number | null;
  max_total_amount_minor: number | null;
  categories: string[];
  conditions: string[];
  required_options: OptionConstraint[];
  required_terms: string[];
};

export type SoftPreferences = {
  terms: string[];
  categories: string[];
  conditions: string[];
  options: OptionConstraint[];
  selection_strategy: "lowest_observed_price" | null;
};

/** Motivos de descarte que cuenta `rank_candidates`. */
export type RejectionReason =
  | "unavailable"
  | "currency"
  | "budget"
  | "category"
  | "condition"
  | "option"
  | "term";

// ProductCard: proyección de presentación del feed.

export type ProductImage = {
  url: string;
  alt: string;
};

export type ProductOption = {
  name: string;
  value: string;
};

export type ProductCard = {
  product_id: string;
  variant_id: string;
  title: string;
  variant_title: string;
  /** Texto plano truncado a 500 caracteres; `null` si el feed no lo trae. */
  description: string | null;
  /** Solo https, sin credenciales y del mismo host que `product_url`; si no, `null`. */
  image: ProductImage | null;
  /** Precio observado en el catálogo, no un término autoritativo. */
  observed_unit_amount_minor: number;
  currency: string;
  available: boolean;
  options: ProductOption[];
  /** El backend lo envía `null` si la URL del feed no es http(s) válida. */
  product_url: string | null;
};

// Pasos que orquesta el backend, expuestos como tool calls.

export type InterpretAction = "respond" | "clarify" | "purchase";

export type InterpretRequestArgs = Record<string, never>;

export type InterpretRequestResult = {
  action: InterpretAction;
  constraints: HardConstraints | null;
  preferences: SoftPreferences | null;
};

export type SearchCatalogArgs = {
  constraints: HardConstraints | null;
  preferences: SoftPreferences | null;
};

export type SearchCatalogResult = {
  matches: number;
  /** Conteos por motivo; las claves pueden crecer, por eso no se restringen a `RejectionReason`. */
  rejections: Record<string, number>;
  /** Máximo 6 productos. */
  products: ProductCard[];
};

export type SelectProductArgs = {
  candidate_variant_ids: string[];
};

export type SelectProductResult = {
  decision: "select" | "clarify";
  product: ProductCard | null;
  rationale: string;
  /** Solo en un turno condicional que evaluó sus ramas; ver `parseConditionalView`. */
  conditional?: ConditionalView;
};

/** Estado de una rama (docs/design/p0-conditional-intent.md §3.3). */
export type ConditionalBranchStatus =
  | "eligible"
  | "over_limit"
  | "out_of_stock"
  | "inactive"
  | "intent_mismatch"
  | "unknown"
  | "not_triggered"
  | "not_evaluated";

/** Una rama tal como la decidió el backend con la oferta del comercio; montos en centavos COP. */
export type ConditionalBranchView = {
  /** Su lugar en el orden del comprador, desde 0. */
  index: number;
  variant_id: string;
  title: string;
  status: ConditionalBranchStatus;
  /** Total del comercio con envío, si se leyó su oferta. */
  total_minor: number | null;
  /** Límite de total efectivo de la rama; `null` si no hay ninguno. */
  limit_minor: number | null;
  /** Precio sin envío de esa misma oferta. */
  items_total_minor: number | null;
  /** Límite por unidad del pedido: un `over_limit` dentro de `limit_minor` es por este. */
  unit_limit_minor: number | null;
};

/**
 * `SelectProductResult.conditional` (§3.10). `exclusions_only` y `clarify` no traen vista: la
 * exclusión se ve en `rejections.excluded` y la aclaración es una pregunta.
 */
export type ConditionalView = {
  outcome: "branch" | "none" | "undetermined" | "offer_changed";
  /** La rama elegida, solo con `branch` u `offer_changed`; siempre una rama `eligible`. */
  chosen_index: number | null;
  currency: "COP";
  /** De 1 a 3, en orden. */
  branches: ConditionalBranchView[];
  /** Solo con `offer_changed`: el total del checkout, distinto del de la oferta. */
  checkout_total_minor: number | null;
};

// Validación con las mismas reglas que `ConditionalView` en Pydantic (application/conditional.py);
// tests/test_conditional_contract.py valida las mismas fixtures. Cada error nombra el primer campo
// que no cumple.

type Check = (value: unknown, path: string) => string | null;

const BRANCH_STATUSES: ConditionalBranchStatus[] = [
  "eligible",
  "over_limit",
  "out_of_stock",
  "inactive",
  "intent_mismatch",
  "unknown",
  "not_triggered",
  "not_evaluated",
];
const VIEW_OUTCOMES: ConditionalView["outcome"][] = [
  "branch",
  "none",
  "undetermined",
  "offer_changed",
];

const valid =
  (test: (value: unknown) => boolean): Check =>
  (value, path) =>
    test(value) ? null : path;
const orNull =
  (check: Check): Check =>
  (value, path) =>
    value === null ? null : check(value, path);
const minor = valid(
  (value) => Number.isSafeInteger(value) && (value as number) >= 0,
);
const branchIndex = valid(
  (value) =>
    Number.isSafeInteger(value) &&
    (value as number) >= 0 &&
    (value as number) < 3,
);
const nonEmpty = valid(
  (value) => typeof value === "string" && value.length > 0,
);
const among = <T>(options: readonly T[]) =>
  valid((value) => options.includes(value as T));

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Exactamente estos campos, como `extra="forbid"`; después, la regla entre campos. */
function exactly(
  fields: Record<string, Check>,
  rule: (value: Record<string, unknown>, path: string) => string | null,
): Check {
  return (value, path) => {
    if (!record(value)) return path;
    const extra = Object.keys(value).find((key) => !Object.hasOwn(fields, key));
    if (extra !== undefined) return `${path}.${extra}`;
    for (const [key, check] of Object.entries(fields)) {
      const failure = check(value[key], `${path}.${key}`);
      if (failure !== null) return failure;
    }
    return rule(value, path);
  };
}

const branchView = exactly(
  {
    index: branchIndex,
    variant_id: nonEmpty,
    title: nonEmpty,
    status: among(BRANCH_STATUSES),
    total_minor: orNull(minor),
    limit_minor: orNull(minor),
    items_total_minor: orNull(minor),
    unit_limit_minor: orNull(minor),
  } satisfies { [K in keyof ConditionalBranchView]-?: Check },
  (branch, path) => {
    // Los dos totales salen de la misma oferta, y uno incluye el envío.
    const { total_minor: total, items_total_minor: items } = branch;
    return (total === null) !== (items === null) ||
      (items as number) > (total as number)
      ? `${path}.items_total_minor`
      : null;
  },
);

const conditionalView = exactly(
  {
    outcome: among(VIEW_OUTCOMES),
    chosen_index: orNull(branchIndex),
    currency: among(["COP"]),
    branches: (value, path) => {
      if (!Array.isArray(value) || value.length < 1 || value.length > 3)
        return path;
      for (const [index, branch] of value.entries()) {
        const failure = branchView(branch, `${path}[${index}]`);
        if (failure !== null) return failure;
      }
      return null;
    },
    checkout_total_minor: orNull(minor),
  } satisfies { [K in keyof ConditionalView]-?: Check },
  (view, path) => {
    const branches = view.branches as ConditionalBranchView[];
    const outOfOrder = branches.findIndex(
      (branch, index) => branch.index !== index,
    );
    if (outOfOrder >= 0) return `${path}.branches[${outOfOrder}].index`;
    const chosen = view.chosen_index as number | null;
    const chooses =
      view.outcome === "branch" || view.outcome === "offer_changed";
    if (chooses !== (chosen !== null)) return `${path}.chosen_index`;
    if (chosen !== null && branches[chosen]?.status !== "eligible")
      return `${path}.chosen_index`;
    if (
      (view.outcome === "offer_changed") !==
      (view.checkout_total_minor !== null)
    ) {
      return `${path}.checkout_total_minor`;
    }
    return null;
  },
);

/** Lanza `Invalid conditional view at <campo>` si no cumple el contrato. */
export function parseConditionalView(value: unknown): ConditionalView {
  const failure = conditionalView(value, "conditional");
  if (failure !== null)
    throw new Error(`Invalid conditional view at ${failure}`);
  return value as ConditionalView;
}

export type PrepareCheckoutArgs = {
  variant_id: string;
  /** La compra que se prepara; si la política la detiene, el paso falla y solo queda aquí. */
  run_id: string;
};

export type PrepareCheckoutResult = {
  run_id: string;
  checkout_id: string;
  checkout_status: CheckoutStatus;
  price_changed: boolean;
  total_minor: number;
  currency: string;
};

export type RequestApprovalArgs = {
  run_id: string;
};

/** Igual a `GET /purchases/{run_id}/approval`. */
export type RequestApprovalResult = {
  run_id: string;
  stage: PurchaseStage;
  terms: ApprovalTerms;
};

export type PurchaseUpdateArgs = {
  run_id: string;
};

export type PurchaseUpdateResult = {
  run_id: string;
  stage: PurchaseStage;
  order_id: string | null;
};

/** `result` de un paso con `isError: true` (lo emite el backend aunque la tabla no lo liste). */
export type AgentToolError = {
  error: { code: string; message: string };
};

export type AgentTools = {
  interpret_request: {
    args: InterpretRequestArgs;
    result: InterpretRequestResult;
  };
  search_catalog: { args: SearchCatalogArgs; result: SearchCatalogResult };
  select_product: { args: SelectProductArgs; result: SelectProductResult };
  prepare_checkout: {
    args: PrepareCheckoutArgs;
    result: PrepareCheckoutResult;
  };
  request_approval: {
    args: RequestApprovalArgs;
    result: RequestApprovalResult;
  };
  purchase_update: { args: PurchaseUpdateArgs; result: PurchaseUpdateResult };
};

export type AgentToolName = keyof AgentTools;

export const AGENT_TOOL_NAMES = [
  "interpret_request",
  "search_catalog",
  "select_product",
  "prepare_checkout",
  "request_approval",
  "purchase_update",
] as const satisfies readonly AgentToolName[];

// Mensajes (compatibles con `ThreadMessageLike`).

export type AgentTextPart = {
  type: "text";
  text: string;
};

/** Resumen de razonamiento de la Responses API; nunca razonamiento crudo. */
export type AgentReasoningPart = {
  type: "reasoning";
  text: string;
};

export type AgentToolCallPart<Name extends AgentToolName = AgentToolName> = {
  [Key in Name]: {
    type: "tool-call";
    toolCallId: string;
    toolName: Key;
    args: AgentTools[Key]["args"];
    argsText: string;
    /** Falta mientras el paso está en curso; con `isError` trae un `AgentToolError`. */
    result?: AgentTools[Key]["result"] | AgentToolError;
    isError?: boolean;
  };
}[Name];

export type AgentAssistantPart =
  AgentTextPart | AgentReasoningPart | AgentToolCallPart;

export type AgentMessageStatus =
  | { type: "running" }
  | { type: "complete"; reason: "stop" }
  | { type: "incomplete"; reason: "error"; error: string }
  | { type: "incomplete"; reason: "cancelled" };

export type AgentUserMessage = {
  id: string;
  role: "user";
  content: AgentTextPart[];
};

export type AgentAssistantMessage = {
  id: string;
  role: "assistant";
  status: AgentMessageStatus;
  content: AgentAssistantPart[];
};

export type AgentMessage = AgentUserMessage | AgentAssistantMessage;

/** Etapa de la compra leída de SQLite; `null` hasta que exista un checkout. */
export type AgentPurchase = {
  run_id: string;
  stage: PurchaseStage;
  order_id: string | null;
};

/**
 * Lo que encontró un turno `delegate` (docs/design/hnp-chat-delegate.md): prellena la tarjeta de
 * delegación del hilo. Solo para mostrar: `POST /delegations/proposals` revalida cada variante y el
 * total.
 */
export type DelegationDraftView = {
  /** La lista corta del feed validado: disponibles y en COP, máximo 6. */
  offers: ProductCard[];
  /** El presupuesto del mensaje, en centavos; `null` si el comprador no escribió ninguno. */
  budget_minor: number | null;
  currency: "COP";
  /** El mensaje del comprador, tal cual. */
  preference_text: string;
  /**
   * El orden que leyó el turno entre `offers` (p0-conditional-intent.md §3.9): de 1 a 3 variantes
   * distintas. Ausente si no leyó ninguno; nunca `null`.
   */
  priority?: string[];
};

export type AgentState = {
  messages: AgentMessage[];
  purchase: AgentPurchase | null;
  /** Solo después de un turno `delegate`; el siguiente turno lo reemplaza o lo quita. */
  delegation_draft?: DelegationDraftView | null;
};

/** `mode` del cuerpo de `/assistant`: con «Delegar», el turno busca y no crea checkout. */
export type ChatMode = "present" | "delegate";

export const INITIAL_AGENT_STATE: AgentState = { messages: [], purchase: null };

/**
 * Máximo de caracteres de una parte de texto (`MAX_MESSAGE_CHARS` en `assistant_transport.py`).
 * Pasarse devuelve un 422 genérico, así que el compositor no deja llegar hasta ahí.
 */
export const MAX_MESSAGE_CHARS = 2_000;

// Comandos. Ninguno transporta evidencia de aprobación: esa va solo por REST.

export type AddMessageCommand = {
  type: "add-message";
  message: { role: "user"; parts: AgentTextPart[] };
  parentId: string | null;
  sourceId: string | null;
};

export type PurchaseUpdatedCommand = {
  type: "purchase-updated";
  runId: string;
};

export type AgentCommand = AddMessageCommand | PurchaseUpdatedCommand;
