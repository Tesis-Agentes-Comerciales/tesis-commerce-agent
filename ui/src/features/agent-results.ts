import type {
  ApprovalTerms,
  DelegationDraftView,
  InterpretRequestResult,
  PrepareCheckoutResult,
  ProductCard,
  PurchaseUpdateResult,
  RequestApprovalResult,
  SearchCatalogResult,
  SelectProductResult,
} from "@/lib/agent-contract"

// Validación de los `result` de cada paso. El backend los genera, pero el contrato los trata como no
// confiables: un campo con otra forma descarta el dato en vez de romper el render.

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

const isString = (value: unknown): value is string => typeof value === "string"
const isNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value)
const nullableString = (value: unknown) => (isString(value) ? value : null)

function nameValuePairs(value: unknown) {
  return Array.isArray(value)
    ? value.filter(
        (item): item is { name: string; value: string } =>
          isRecord(item) && isString(item.name) && isString(item.value),
      )
    : []
}

export function asProduct(value: unknown): ProductCard | null {
  if (!isRecord(value)) return null
  const { product_id, variant_id, title, variant_title, observed_unit_amount_minor, currency } =
    value
  if (
    !isString(product_id) ||
    !isString(variant_id) ||
    !isString(title) ||
    !isNumber(observed_unit_amount_minor) ||
    !isString(currency)
  ) {
    return null
  }
  const image =
    isRecord(value.image) && isString(value.image.url)
      ? { url: value.image.url, alt: isString(value.image.alt) ? value.image.alt : title }
      : null
  return {
    product_id,
    variant_id,
    title,
    variant_title: isString(variant_title) ? variant_title : "",
    description: nullableString(value.description),
    image,
    observed_unit_amount_minor,
    currency,
    available: value.available === true,
    options: nameValuePairs(value.options),
    product_url: nullableString(value.product_url),
  }
}

function stringList(value: unknown) {
  return Array.isArray(value) ? value.filter(isString) : []
}

export function asInterpretResult(value: unknown): InterpretRequestResult | null {
  if (!isRecord(value)) return null
  const { action, constraints, preferences } = value
  if (action !== "respond" && action !== "clarify" && action !== "purchase") return null
  return {
    action,
    constraints: isRecord(constraints)
      ? {
          currency: isString(constraints.currency) ? constraints.currency : "",
          max_unit_amount_minor: isNumber(constraints.max_unit_amount_minor)
            ? constraints.max_unit_amount_minor
            : null,
          max_total_amount_minor: isNumber(constraints.max_total_amount_minor)
            ? constraints.max_total_amount_minor
            : null,
          categories: stringList(constraints.categories),
          conditions: stringList(constraints.conditions),
          required_options: nameValuePairs(constraints.required_options),
          required_terms: stringList(constraints.required_terms),
        }
      : null,
    preferences: isRecord(preferences)
      ? {
          terms: stringList(preferences.terms),
          categories: stringList(preferences.categories),
          conditions: stringList(preferences.conditions),
          options: nameValuePairs(preferences.options),
          selection_strategy:
            preferences.selection_strategy === "lowest_observed_price"
              ? "lowest_observed_price"
              : null,
        }
      : null,
  }
}

export function asSearchResult(value: unknown): SearchCatalogResult | null {
  if (!isRecord(value) || !isNumber(value.matches)) return null
  const rejections = isRecord(value.rejections)
    ? Object.fromEntries(
        Object.entries(value.rejections).filter((entry): entry is [string, number] =>
          isNumber(entry[1]),
        ),
      )
    : {}
  const products = Array.isArray(value.products)
    ? value.products.flatMap((item) => asProduct(item) ?? []).slice(0, 6)
    : []
  return { matches: value.matches, rejections, products }
}

export function asSelectResult(value: unknown): SelectProductResult | null {
  if (!isRecord(value)) return null
  const decision = value.decision === "select" ? "select" : value.decision === "clarify" ? "clarify" : null
  if (!decision) return null
  return {
    decision,
    product: asProduct(value.product),
    rationale: isString(value.rationale) ? value.rationale : "",
  }
}

export function asPrepareResult(value: unknown): PrepareCheckoutResult | null {
  if (!isRecord(value)) return null
  const { run_id, checkout_id, checkout_status, total_minor, currency } = value
  if (!isString(run_id) || !isString(checkout_id) || !isNumber(total_minor) || !isString(currency)) {
    return null
  }
  return {
    run_id,
    checkout_id,
    checkout_status: (isString(checkout_status)
      ? checkout_status
      : "not_ready_for_payment") as PrepareCheckoutResult["checkout_status"],
    price_changed: value.price_changed === true,
    total_minor,
    currency,
  }
}

function asTerms(value: unknown): ApprovalTerms | null {
  if (!isRecord(value)) return null
  const { merchant_origin, checkout_id, currency, total_minor, fingerprint, lines } = value
  if (
    !isString(merchant_origin) ||
    !isString(checkout_id) ||
    !isString(currency) ||
    !isNumber(total_minor) ||
    !isString(fingerprint) ||
    !Array.isArray(lines)
  ) {
    return null
  }
  const address = isRecord(value.fulfillment_details) ? value.fulfillment_details.address : null
  return {
    merchant_origin,
    checkout_id,
    currency,
    total_minor,
    lines: lines.filter(
      (line): line is ApprovalTerms["lines"][number] =>
        isRecord(line) &&
        isString(line.item_id) &&
        isString(line.name) &&
        isNumber(line.quantity) &&
        isNumber(line.unit_amount_minor),
    ),
    selected_fulfillment_options: Array.isArray(value.selected_fulfillment_options)
      ? value.selected_fulfillment_options.filter(isRecord)
      : [],
    buyer: isRecord(value.buyer) ? value.buyer : null,
    fulfillment_details: isRecord(address)
      ? {
          address: {
            city: isString(address.city) ? address.city : undefined,
            country: isString(address.country) ? address.country : undefined,
          },
        }
      : null,
    payment_handler: isString(value.payment_handler) ? value.payment_handler : "",
    action: isString(value.action) ? value.action : "",
    expires_at: isString(value.expires_at) ? value.expires_at : "",
    fingerprint_version: isString(value.fingerprint_version) ? value.fingerprint_version : "",
    fingerprint,
  }
}

export function asApprovalResult(value: unknown): RequestApprovalResult | null {
  if (!isRecord(value) || !isString(value.run_id)) return null
  const terms = asTerms(value.terms)
  if (!terms) return null
  return {
    run_id: value.run_id,
    stage: (isString(value.stage) ? value.stage : "awaiting_confirmation") as RequestApprovalResult["stage"],
    terms,
  }
}

export function asPurchaseUpdateResult(value: unknown): PurchaseUpdateResult | null {
  if (!isRecord(value) || !isString(value.run_id) || !isString(value.stage)) return null
  return {
    run_id: value.run_id,
    stage: value.stage as PurchaseUpdateResult["stage"],
    order_id: nullableString(value.order_id),
  }
}

/**
 * `delegation_draft` del estado del chat, que llega del backend y se trata como no confiable: solo
 * quedan las opciones con forma de `ProductCard`, disponibles y en pesos, como en `seenOffers`.
 */
export function asDelegationDraft(value: unknown): DelegationDraftView | null {
  if (!isRecord(value) || !Array.isArray(value.offers)) return null
  if (typeof value.preference_text !== "string") return null
  const offers = value.offers
    .flatMap((item) => asProduct(item) ?? [])
    .filter((offer) => offer.available && offer.currency.toUpperCase() === "COP")
    .slice(0, 6)
  const budget = value.budget_minor
  return {
    offers,
    budget_minor:
      typeof budget === "number" && Number.isSafeInteger(budget) && budget > 0 ? budget : null,
    currency: "COP",
    preference_text: value.preference_text,
  }
}
