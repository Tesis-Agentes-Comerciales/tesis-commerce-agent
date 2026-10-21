import type {
  CheckoutStatus,
  HardConstraints,
  InterpretRequestResult,
  OptionConstraint,
  SoftPreferences,
} from "@/lib/agent-contract"

const LOCALE = "es-CO"
const MAX_URL_LENGTH = 2048

function moneyFormatter(currency: string) {
  try {
    // Una sola notación en toda la app: símbolo corto ("$") y el código ISO aparte. Con
    // `currencyDisplay: "symbol"`, es-CO escribiría "US$" y el mismo total se leería de dos formas.
    return new Intl.NumberFormat(LOCALE, {
      style: "currency",
      currency: currency.toUpperCase(),
      currencyDisplay: "narrowSymbol",
      // Los montos llegan en centavos: siempre dos decimales. Los datos de CLDR recientes (Node 26,
      // el Chromium de Playwright) dan 0 decimales a COP, y 999,99 se leería "$ 1.000". El backend
      // escribe la misma cadena en los mensajes HNP (K16, fixtures/money-notation.json).
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  } catch {
    // Moneda inválida en datos no confiables: se muestra el número sin símbolo.
    return null
  }
}

/** Monto con símbolo corto y el código ISO aparte, para totales destacados. */
export function formatMoneyParts(amountMinor: number, currency: string) {
  const formatter = moneyFormatter(currency)
  const amount = amountMinor / 100
  return {
    amount: formatter ? formatter.format(amount) : amount.toLocaleString(LOCALE),
    code: currency.toUpperCase(),
  }
}

/** Montos en unidades menores (centavos), en una línea: "$ 1.924.000,00 COP". */
export function formatMoney(amountMinor: number, currency: string) {
  const { amount, code } = formatMoneyParts(amountMinor, currency)
  return `${amount} ${code}`
}

export function formatElapsed(ms: number) {
  if (!Number.isFinite(ms) || ms < 0) return null
  const seconds = ms / 1000
  if (seconds < 60) {
    const value = new Intl.NumberFormat(LOCALE, {
      maximumFractionDigits: seconds < 10 ? 1 : 0,
    }).format(seconds)
    return `${value} s`
  }
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return rest === 0 ? `${minutes} min` : `${minutes} min ${rest} s`
}

/** Como `formatElapsed`, pero un control de milisegundos no se redondea a "0 s". */
export function formatDuration(ms: number) {
  if (!Number.isFinite(ms) || ms < 0) return null
  if (ms >= 1000) return formatElapsed(ms)
  return new Intl.NumberFormat(LOCALE, { style: "unit", unit: "millisecond" }).format(ms)
}

export function formatDateTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return new Intl.DateTimeFormat(LOCALE, { dateStyle: "medium", timeStyle: "short" }).format(date)
}

/** Fecha y hora con segundos, y milisegundos si se piden; para distinguir dos instantes cercanos. */
export function formatPreciseDateTime(value: string, { milliseconds = false } = {}) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  // Mismo orden que `dateStyle: "medium"`, que no admite `fractionalSecondDigits`.
  return new Intl.DateTimeFormat(LOCALE, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    ...(milliseconds ? { fractionalSecondDigits: 3 } : {}),
  }).format(date)
}

/** Hora local en 24 h ("10:42"), para recibos y pasos de la traza. */
export function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return new Intl.DateTimeFormat(LOCALE, {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date)
}

export function compactFingerprint(value: string) {
  return value.length <= 24 ? value : `${value.slice(0, 10)}…${value.slice(-10)}`
}

function parseUrl(value: unknown, base?: string) {
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_URL_LENGTH) return null
  try {
    const url = new URL(value, base)
    if (url.username || url.password) return null
    return url
  } catch {
    return null
  }
}

/** Enlaces del feed o del merchant: absolutos, solo http(s) y sin credenciales. */
export function safeLinkUrl(value: unknown) {
  const url = parseUrl(value)
  if (!url || (url.protocol !== "https:" && url.protocol !== "http:")) return null
  return url.href
}

/**
 * Imágenes del feed: https como exige el contrato. También se aceptan recursos del mismo origen
 * de la app (Storybook sirve ahí sus imágenes locales).
 */
export function safeImageUrl(value: unknown) {
  const url = parseUrl(value, window.location.href)
  if (!url) return null
  if (url.protocol === "https:" || url.origin === window.location.origin) return url.href
  return null
}

/** Tono estable por producto para que el placeholder no sea igual en todas las tarjetas. */
export function placeholderHue(seed: string) {
  let hash = 0
  for (const char of seed) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return hash % 360
}

const REJECTION_LABELS: Record<string, string> = {
  unavailable: "sin disponibilidad",
  currency: "en otra moneda",
  budget: "fuera del presupuesto",
  category: "de otra categoría",
  condition: "en otra condición",
  option: "sin la opción pedida",
  term: "sin los términos pedidos",
}

export type RejectionSummary = {
  total: number
  items: Array<{ reason: string; label: string; count: number }>
}

export function summarizeRejections(rejections: Record<string, number> | null | undefined) {
  const items = Object.entries(rejections ?? {})
    .filter(([, count]) => Number.isFinite(count) && count > 0)
    .map(([reason, count]) => ({
      reason,
      label: REJECTION_LABELS[reason] ?? "por otro motivo",
      count: Math.floor(count),
    }))
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label))
  return {
    total: items.reduce((sum, item) => sum + item.count, 0),
    items,
  } satisfies RejectionSummary
}

export function pluralize(count: number, singular: string, plural: string) {
  return `${count.toLocaleString(LOCALE)} ${count === 1 ? singular : plural}`
}

const CHECKOUT_STATUS_LABELS: Record<CheckoutStatus, string> = {
  not_ready_for_payment: "Faltan datos para pagar",
  ready_for_payment: "Listo para pago",
  completed: "Completado",
  canceled: "Cancelado",
  expired: "Vencido",
}

export function checkoutStatusLabel(status: string) {
  return CHECKOUT_STATUS_LABELS[status as CheckoutStatus] ?? status
}

export type IntentChip = {
  value: string
  /** Las preferencias ordenan resultados; los requisitos descartan productos. */
  soft: boolean
}

export type IntentGroup = {
  kind: "categoria" | "presupuesto" | "opciones" | "condicion" | "terminos" | "preferencias"
  label: string
  chips: IntentChip[]
}

function optionText(option: OptionConstraint) {
  return `${option.name}: ${option.value}`
}

function chips(values: readonly string[] | undefined, soft: boolean): IntentChip[] {
  return (values ?? []).filter(Boolean).map((value) => ({ value, soft }))
}

/** Agrupa la intención interpretada en chips legibles; omite grupos vacíos. */
export function intentGroups(
  constraints: HardConstraints | null | undefined,
  preferences: SoftPreferences | null | undefined,
): IntentGroup[] {
  const hard = constraints
  const soft = preferences
  const budget: IntentChip[] = []
  if (hard?.max_unit_amount_minor != null) {
    budget.push({
      value: `hasta ${formatMoney(hard.max_unit_amount_minor, hard.currency)} por unidad`,
      soft: false,
    })
  }
  if (hard?.max_total_amount_minor != null) {
    budget.push({
      value: `hasta ${formatMoney(hard.max_total_amount_minor, hard.currency)} en total`,
      soft: false,
    })
  }
  const strategy: IntentChip[] =
    soft?.selection_strategy === "lowest_observed_price"
      ? [{ value: "el precio más bajo", soft: true }]
      : []

  const groups: IntentGroup[] = [
    {
      kind: "categoria",
      label: "Categoría",
      chips: [...chips(hard?.categories, false), ...chips(soft?.categories, true)],
    },
    { kind: "presupuesto", label: "Presupuesto", chips: budget },
    {
      kind: "opciones",
      label: "Opciones",
      chips: [
        ...chips(hard?.required_options?.map(optionText), false),
        ...chips(soft?.options?.map(optionText), true),
      ],
    },
    {
      kind: "condicion",
      label: "Condición",
      chips: [...chips(hard?.conditions, false), ...chips(soft?.conditions, true)],
    },
    { kind: "terminos", label: "Debe incluir", chips: chips(hard?.required_terms, false) },
    {
      kind: "preferencias",
      label: "Preferencias",
      chips: [...chips(soft?.terms, true), ...strategy],
    },
  ]
  return groups.filter((group) => group.chips.length > 0)
}

export function interpretActionLabel(action: InterpretRequestResult["action"] | undefined) {
  switch (action) {
    case "purchase":
      return "Quieres comprar"
    case "clarify":
      return "Necesito más detalles"
    case "respond":
      return "Respuesta conversacional"
    default:
      return "Solicitud interpretada"
  }
}
