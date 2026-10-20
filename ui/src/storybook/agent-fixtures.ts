import earbudsImage from "@/components/agent/story-assets/earbuds.svg?no-inline"
import headphonesBlackImage from "@/components/agent/story-assets/headphones-black.webp?no-inline"
import headphonesWhiteImage from "@/components/agent/story-assets/headphones-white.svg?no-inline"
import speakerImage from "@/components/agent/story-assets/speaker.svg?no-inline"
import type {
  AgentState,
  HardConstraints,
  InterpretRequestResult,
  PrepareCheckoutResult,
  ProductCard,
  PurchaseUpdateResult,
  RequestApprovalResult,
  SearchCatalogResult,
  SelectProductResult,
  SoftPreferences,
} from "@/lib/agent-contract"
import type { ApprovalTerms } from "@/lib/commerce-types"
import { COMPLETED, PREPARATION, PURCHASE_REPLY, PURCHASE_REQUEST, TERMS } from "@/storybook/fixtures"

// Datos sintéticos alineados con fixtures.ts (PREPARATION.run_id, cs_storybook_1, SON-01). Los
// `run_id` tienen la forma real del backend, `run_<hex>`. Las imágenes son SVG locales servidos por
// Storybook: ninguna historia pide recursos externos.

const PRODUCT_BASE = "https://merchant.example.test/products"

export const SONORA: ProductCard = {
  product_id: PREPARATION.selected_candidate.product_id,
  variant_id: PREPARATION.selected_candidate.variant_id,
  title: PREPARATION.selected_candidate.product_title,
  variant_title: PREPARATION.selected_candidate.variant_title,
  description:
    "Audífonos de diadema sintéticos con almohadillas acolchadas, cancelación pasiva de ruido y 30 horas de batería. Producto nuevo.",
  image: { url: headphonesBlackImage, alt: "Audífonos Sonora negros de diadema" },
  observed_unit_amount_minor: PREPARATION.selected_candidate.observed_unit_amount_minor,
  currency: PREPARATION.selected_candidate.currency,
  available: true,
  options: [
    { name: "Color", value: "negro" },
    { name: "Conexión", value: "inalámbrica" },
  ],
  product_url: `${PRODUCT_BASE}/prod-sony-wh-1000xm6`,
}

export const SONORA_WHITE: ProductCard = {
  ...SONORA,
  variant_id: "SON-01-W",
  variant_title: "Sonora blanco",
  image: { url: headphonesWhiteImage, alt: "Audífonos Sonora blancos de diadema" },
  options: [
    { name: "Color", value: "blanco" },
    { name: "Conexión", value: "inalámbrica" },
  ],
}

export const AURORA: ProductCard = {
  product_id: "prod-aur-02",
  variant_id: "AUR-02",
  title: "Audífonos Aurora Buds AUR-02",
  variant_title: "Aurora blanco perla",
  description: "Audífonos inalámbricos sintéticos con estuche de carga. Producto nuevo.",
  image: { url: earbudsImage, alt: "Audífonos Aurora inalámbricos con su estuche" },
  observed_unit_amount_minor: 45000,
  currency: "COP",
  available: true,
  options: [{ name: "Color", value: "blanco" }],
  product_url: `${PRODUCT_BASE}/prod-aur-02`,
}

/** Sin imagen en el feed (`image: null`). */
export const ESTUDIO: ProductCard = {
  product_id: "prod-est-04",
  variant_id: "EST-04",
  title: "Audífonos Estudio Pro EST-04",
  variant_title: "Estudio grafito",
  description: null,
  image: null,
  observed_unit_amount_minor: 79900,
  currency: "COP",
  available: true,
  options: [{ name: "Color", value: "grafito" }],
  product_url: `${PRODUCT_BASE}/prod-est-04`,
}

/** La URL existe en el contrato pero no carga (mismo origen, ruta inexistente). */
export const PULSO: ProductCard = {
  product_id: "prod-pul-06",
  variant_id: "PUL-06",
  title: "Audífonos deportivos Pulso Sport PUL-06",
  variant_title: "Pulso negro",
  description: "Audífonos deportivos sintéticos resistentes al sudor.",
  image: { url: "/storybook-missing/prod-pul-06.jpg", alt: "Audífonos Pulso Sport" },
  observed_unit_amount_minor: 38000,
  currency: "COP",
  available: true,
  options: [{ name: "Color", value: "negro" }],
  product_url: `${PRODUCT_BASE}/prod-pul-06`,
}

export const BRISA: ProductCard = {
  product_id: "prod-bri-03",
  variant_id: "BRI-03",
  title: "Parlante portátil Brisa BRI-03",
  variant_title: "Brisa azul",
  description: "Parlante sintético resistente al agua.",
  image: { url: speakerImage, alt: "Parlante Brisa azul" },
  observed_unit_amount_minor: 52000,
  currency: "COP",
  available: false,
  options: [{ name: "Color", value: "azul" }],
  product_url: `${PRODUCT_BASE}/prod-bri-03`,
}

export const NIMBUS: ProductCard = {
  product_id: "prod-nim-05",
  variant_id: "NIM-05",
  title: "Audífonos Nimbus NIM-05",
  variant_title: "Nimbus arena",
  description: "Audífonos sintéticos plegables.",
  image: null,
  observed_unit_amount_minor: 64000,
  currency: "COP",
  available: true,
  options: [{ name: "Color", value: "arena" }],
  product_url: `${PRODUCT_BASE}/prod-nim-05`,
}

/** Como lo enviaría el feed de pruebas; la URL no es https y la UI no la carga. */
export const SONORA_INSECURE_IMAGE: ProductCard = {
  ...SONORA,
  image: { url: "http://merchant.example.test/assets/products/prod-sony-wh-1000xm6.jpg", alt: "Sonora" },
}

export const SONORA_LONG: ProductCard = {
  ...SONORA,
  title:
    "Audífonos inalámbricos Sonora SON-01 Pro Edición Estudio con cancelación activa de ruido y estuche rígido de viaje",
  variant_title: "Negro mate con acabados en aluminio cepillado · Estuche de viaje incluido",
  description: `${"Diadema ajustable de acero con acolchado de espuma viscoelástica, almohadillas reemplazables y plegado compacto. ".repeat(4)}Incluye cable de 3,5 mm, adaptador de avión y manual sintético.`.slice(
    0,
    500,
  ),
  options: [
    { name: "Color", value: "negro mate" },
    { name: "Conexión", value: "inalámbrica y cable" },
    { name: "Cancelación de ruido", value: "activa híbrida" },
    { name: "Garantía", value: "12 meses" },
  ],
}

export const SELECT_RATIONALE =
  "Cumple tu presupuesto de 2 millones, es negro y está disponible. Entre las cuatro opciones es la que mejor coincide con tu preferencia por audífonos inalámbricos."

export const HARD_CONSTRAINTS: HardConstraints = {
  currency: "COP",
  max_unit_amount_minor: 80000,
  max_total_amount_minor: null,
  categories: ["audífonos"],
  conditions: ["nuevo"],
  required_options: [{ name: "color", value: "negro" }],
  required_terms: [],
}

export const SOFT_PREFERENCES: SoftPreferences = {
  terms: ["inalámbricos"],
  categories: [],
  conditions: [],
  options: [{ name: "conexión", value: "bluetooth" }],
  selection_strategy: "lowest_observed_price",
}

export const INTERPRET_PURCHASE: InterpretRequestResult = {
  action: "purchase",
  constraints: HARD_CONSTRAINTS,
  preferences: SOFT_PREFERENCES,
}

export const INTERPRET_CLARIFY: InterpretRequestResult = {
  action: "clarify",
  constraints: null,
  preferences: null,
}

export const INTERPRET_RESPOND: InterpretRequestResult = {
  action: "respond",
  constraints: null,
  preferences: null,
}

export const SEARCH_RESULT: SearchCatalogResult = {
  matches: 4,
  rejections: { budget: 3, unavailable: 2, category: 1 },
  products: [SONORA, SONORA_WHITE, AURORA, ESTUDIO],
}

export const SEARCH_RESULT_MANY: SearchCatalogResult = {
  matches: 14,
  rejections: { budget: 6, option: 3, condition: 1, term: 1, currency: 1 },
  products: [SONORA, SONORA_WHITE, AURORA, ESTUDIO, NIMBUS, PULSO],
}

export const SEARCH_RESULT_SINGLE: SearchCatalogResult = {
  matches: 1,
  rejections: {},
  products: [SONORA],
}

export const SEARCH_RESULT_EMPTY: SearchCatalogResult = {
  matches: 0,
  rejections: { budget: 5, option: 2, unavailable: 1 },
  products: [],
}

export const SELECT_RESULT: SelectProductResult = {
  decision: "select",
  product: SONORA,
  rationale: SELECT_RATIONALE,
}

export const PREPARE_RESULT: PrepareCheckoutResult = {
  run_id: PREPARATION.run_id,
  checkout_id: PREPARATION.checkout.id,
  checkout_status: "ready_for_payment",
  price_changed: false,
  total_minor: TERMS.total_minor,
  currency: "COP",
}

export const PREPARE_PRICE_CHANGED: PrepareCheckoutResult = {
  ...PREPARE_RESULT,
  price_changed: true,
  total_minor: 76000,
}

export const PREPARE_NOT_READY: PrepareCheckoutResult = {
  ...PREPARE_RESULT,
  checkout_status: "not_ready_for_payment",
}

export const APPROVAL_RESULT: RequestApprovalResult = {
  run_id: PREPARATION.run_id,
  stage: "awaiting_confirmation",
  terms: TERMS,
}

export const TERMS_PRICE_CHANGED: ApprovalTerms = {
  ...TERMS,
  total_minor: 76000,
  lines: [{ ...TERMS.lines[0]!, unit_amount_minor: 74000 }],
}

export const TERMS_MULTIPLE_LINES: ApprovalTerms = {
  ...TERMS,
  total_minor: 158000,
  lines: [
    ...TERMS.lines,
    {
      item_id: "CASE-02",
      name: "Estuche rígido Sonora con cable de repuesto y adaptador de avión",
      quantity: 2,
      unit_amount_minor: 7000,
    },
    {
      item_id: "PAD-03",
      name: "Almohadillas de repuesto",
      quantity: 1,
      unit_amount_minor: 58000,
    },
  ],
}

export const TERMS_NO_ADDRESS: ApprovalTerms = { ...TERMS, fulfillment_details: null }

export const ORDER = COMPLETED.order

export const PURCHASE_COMPLETED: PurchaseUpdateResult = {
  run_id: PREPARATION.run_id,
  stage: "completed",
  order_id: COMPLETED.order.id,
}

export const PURCHASE_CANCELED: PurchaseUpdateResult = {
  run_id: PREPARATION.run_id,
  stage: "canceled",
  order_id: null,
}

export const PURCHASE_FAILED: PurchaseUpdateResult = {
  run_id: PREPARATION.run_id,
  stage: "failed",
  order_id: null,
}

export const REASONING_TEXT = `**Entendiendo el pedido**

La persona quiere audífonos negros y fija un tope de $2.000.000 por unidad. Menciona que prefiere que sean inalámbricos, pero no lo pone como condición.

**Revisando el catálogo**

Descarto los productos fuera del presupuesto y los que no tienen la variante negra. Entre los que quedan, priorizo los inalámbricos y, a igualdad, el precio de catálogo más bajo.`

export const REASONING_LONG = Array.from(
  { length: 6 },
  (_, index) =>
    `**Paso ${index + 1}**\n\nComparo las variantes disponibles con las condiciones del pedido y anoto por qué cada una se mantiene o se descarta. El precio del catálogo es solo una referencia: el total válido llega con los términos del checkout.`,
).join("\n\n")

export const TOOL_ERROR = "El catálogo no respondió a tiempo. Intenta de nuevo en unos segundos."

const PREPARE_ARGS = { variant_id: SONORA.variant_id, run_id: PREPARATION.run_id }

/** Estado completo tras preparar la aprobación, con la forma que reconstruye el runtime. */
export const AGENT_STATE_AWAITING_APPROVAL: AgentState = {
  messages: [
    { id: "msg_storybook_1", role: "user", content: [{ type: "text", text: PURCHASE_REQUEST }] },
    {
      id: "msg_storybook_2",
      role: "assistant",
      status: { type: "complete", reason: "stop" },
      content: [
        { type: "reasoning", text: REASONING_TEXT },
        {
          type: "tool-call",
          toolCallId: "call_storybook_interpret",
          toolName: "interpret_request",
          args: {},
          argsText: "{}",
          result: INTERPRET_PURCHASE,
          isError: false,
        },
        {
          type: "tool-call",
          toolCallId: "call_storybook_search",
          toolName: "search_catalog",
          args: { constraints: HARD_CONSTRAINTS, preferences: SOFT_PREFERENCES },
          argsText: JSON.stringify({ constraints: HARD_CONSTRAINTS, preferences: SOFT_PREFERENCES }),
          result: SEARCH_RESULT,
          isError: false,
        },
        {
          type: "tool-call",
          toolCallId: "call_storybook_select",
          toolName: "select_product",
          args: { candidate_variant_ids: SEARCH_RESULT.products.map((item) => item.variant_id) },
          argsText: JSON.stringify({
            candidate_variant_ids: SEARCH_RESULT.products.map((item) => item.variant_id),
          }),
          result: SELECT_RESULT,
          isError: false,
        },
        {
          type: "tool-call",
          toolCallId: "call_storybook_prepare",
          toolName: "prepare_checkout",
          args: PREPARE_ARGS,
          argsText: JSON.stringify(PREPARE_ARGS),
          result: PREPARE_RESULT,
          isError: false,
        },
        {
          type: "tool-call",
          toolCallId: "call_storybook_approval",
          toolName: "request_approval",
          args: { run_id: PREPARATION.run_id },
          argsText: JSON.stringify({ run_id: PREPARATION.run_id }),
          result: APPROVAL_RESULT,
          isError: false,
        },
        { type: "text", text: PURCHASE_REPLY },
      ],
    },
  ],
  purchase: { run_id: PREPARATION.run_id, stage: "awaiting_confirmation", order_id: null },
}

const CHECKOUT_ERROR_MESSAGE = "No pude comunicarme con el ecommerce ACP."
const [PURCHASE_USER_MESSAGE, AWAITING_ASSISTANT] = AGENT_STATE_AWAITING_APPROVAL.messages

/** Como `error.sse`: `prepare_checkout` falla con `isError` y el mensaje queda incompleto. */
export const AGENT_STATE_CHECKOUT_ERROR: AgentState = {
  messages: [
    PURCHASE_USER_MESSAGE!,
    {
      id: "msg_storybook_2",
      role: "assistant",
      status: { type: "incomplete", reason: "error", error: CHECKOUT_ERROR_MESSAGE },
      content: [
        ...(AWAITING_ASSISTANT?.role === "assistant" ? AWAITING_ASSISTANT.content.slice(0, 4) : []),
        {
          type: "tool-call",
          toolCallId: "call_storybook_prepare",
          toolName: "prepare_checkout",
          args: PREPARE_ARGS,
          argsText: JSON.stringify(PREPARE_ARGS),
          result: { error: { code: "transport_error", message: CHECKOUT_ERROR_MESSAGE } },
          isError: true,
        },
      ],
    },
  ],
  purchase: null,
}
