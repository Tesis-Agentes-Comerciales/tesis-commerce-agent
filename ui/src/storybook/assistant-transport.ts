import earbudsImage from "@/components/agent/story-assets/earbuds.svg?no-inline"
import headphonesBlackImage from "@/components/agent/story-assets/headphones-black.webp?no-inline"
import type { AgentState } from "@/lib/agent-contract"
import {
  splitSseFrames,
  type AgentApiReply,
  type AgentApiRequest,
} from "@/storybook/agent-api-mock"
import clarifyRaw from "@/storybook/fixtures/assistant-transport/clarify.sse?raw"
import delegateRaw from "@/storybook/fixtures/assistant-transport/delegate.sse?raw"
import errorRaw from "@/storybook/fixtures/assistant-transport/error.sse?raw"
import greetingRaw from "@/storybook/fixtures/assistant-transport/greeting.sse?raw"
import noCandidateRaw from "@/storybook/fixtures/assistant-transport/no-candidate.sse?raw"
import pendingPurchaseRaw from "@/storybook/fixtures/assistant-transport/pending-purchase.sse?raw"
import purchaseBlockedRaw from "@/storybook/fixtures/assistant-transport/purchase-blocked.sse?raw"
import purchaseCanceledRaw from "@/storybook/fixtures/assistant-transport/purchase-canceled.sse?raw"
import purchaseCompletedRaw from "@/storybook/fixtures/assistant-transport/purchase-completed.sse?raw"
import purchaseRepeatedRaw from "@/storybook/fixtures/assistant-transport/purchase-repeated.sse?raw"
import purchaseRaw from "@/storybook/fixtures/assistant-transport/purchase.sse?raw"

// Streams reales de `POST /assistant` (ver fixtures/assistant-transport/README.md). Storybook los
// reproduce tal cual; solo cambia las imágenes del feed sintético por SVG locales, para que ninguna
// historia pida recursos externos. Brisa apunta a una ruta inexistente y muestra el placeholder.

const FEED_IMAGES: Record<string, string> = {
  "https://merchant.example.test/assets/products/prod-sony-wh-1000xm6.jpg": headphonesBlackImage,
  "https://merchant.example.test/assets/products/prod-alt-01.jpg": earbudsImage,
  "https://merchant.example.test/assets/products/prod-bri-03.jpg":
    "/storybook-missing/prod-bri-03.jpg",
}

function localizeImages(raw: string) {
  return Object.entries(FEED_IMAGES).reduce(
    (body, [remote, local]) => body.replaceAll(remote, local),
    raw,
  )
}

export const ASSISTANT_SSE = {
  greeting: localizeImages(greetingRaw),
  clarify: localizeImages(clarifyRaw),
  purchase: localizeImages(purchaseRaw),
  pendingPurchase: localizeImages(pendingPurchaseRaw),
  purchaseCompleted: localizeImages(purchaseCompletedRaw),
  purchaseCanceled: localizeImages(purchaseCanceledRaw),
  purchaseRepeated: localizeImages(purchaseRepeatedRaw),
  purchaseBlocked: localizeImages(purchaseBlockedRaw),
  noCandidate: localizeImages(noCandidateRaw),
  error: localizeImages(errorRaw),
  delegate: localizeImages(delegateRaw),
} as const

export type AssistantFixture = keyof typeof ASSISTANT_SSE

/** Texto que envía cada fixture (README de los fixtures). */
export const FIXTURE_REQUESTS = {
  greeting: "hola",
  clarify: "Busco algo para escuchar música",
  purchase: "Quiero audífonos negros por menos de 2 millones",
  pendingPurchase: "También quiero unos audífonos blancos",
  noCandidate: "Quiero audífonos por menos de 50 mil",
  delegate: "Quiero audífonos negros por menos de 2 millones",
} as const

type Operation = { type: "set" | "append-text"; path: string[]; value: unknown }

function frameOperations(frame: string): Operation[] {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n")
  if (!data || data === "[DONE]") return []
  const chunk = JSON.parse(data) as { type?: string; operations?: Operation[] }
  return chunk.type === "update-state" ? (chunk.operations ?? []) : []
}

function applyOperation(state: unknown, { type, path, value }: Operation): unknown {
  if (path.length === 0) return type === "set" ? value : state
  const root = state as Record<string, unknown>
  let target = root
  for (const key of path.slice(0, -1)) target = target[key] as Record<string, unknown>
  const last = path.at(-1)!
  target[last] = type === "append-text" ? `${String(target[last] ?? "")}${String(value)}` : value
  return root
}

/**
 * Estado que reconstruye el runtime al aplicar los frames del fixture, hasta el primer frame que
 * contiene `untilMarker` (incluido) o hasta el final. Sirve para mostrar cualquier momento del
 * stream sin temporizadores.
 */
export function fixtureState(fixture: AssistantFixture, untilMarker?: string): AgentState {
  let state: unknown = undefined
  for (const frame of splitSseFrames(ASSISTANT_SSE[fixture])) {
    // JSON.parse devuelve objetos nuevos en cada llamada; mutar el estado local es seguro.
    for (const operation of frameOperations(frame)) state = applyOperation(state, operation)
    if (untilMarker && frame.includes(untilMarker)) break
  }
  return state as AgentState
}

function renameValue(value: unknown, path: string[], tag: string): unknown {
  if (typeof value !== "object" || value === null) return value
  const record = value as Record<string, unknown>
  if (path.length === 2 && typeof record.id === "string") {
    return { ...record, id: `${record.id}-${tag}` }
  }
  if (path.length === 4 && typeof record.toolCallId === "string") {
    return { ...record, toolCallId: `${record.toolCallId}-${tag}` }
  }
  return value
}

/**
 * Reproduce un fixture sobre el estado que envía la historia: el `set` inicial conserva los mensajes
 * del cliente y los índices y ids nuevos se desplazan para no chocar con los existentes. Así el
 * scripted de `Online` encadena fixtures en cualquier orden.
 */
export function rebaseFixture(fixture: AssistantFixture, clientState: AgentState, tag: string) {
  const frames = splitSseFrames(ASSISTANT_SSE[fixture])
  const base = frameOperations(frames[0] ?? "")[0]?.value as AgentState | undefined
  const shift = clientState.messages.length - (base?.messages.length ?? 0)

  return frames.map((frame) => {
    const operations = frameOperations(frame)
    if (operations.length === 0) return frame
    const rebased = operations.map((operation) => {
      if (operation.path.length === 0) {
        const value = operation.value as AgentState
        return { ...operation, value: { ...value, messages: clientState.messages } }
      }
      if (operation.path[0] !== "messages") return operation
      const path = [...operation.path]
      path[1] = String(Number(path[1]) + shift)
      const value = operation.type === "set" ? renameValue(operation.value, path, tag) : operation.value
      return { ...operation, path, value }
    })
    return `data: ${JSON.stringify({ type: "update-state", operations: rebased })}`
  })
}

type AssistantRequestBody = {
  commands?: Array<{ type?: string; runId?: string; message?: { parts?: Array<{ text?: string }> } }>
  state?: AgentState
  details?: unknown
  turn_id?: unknown
  mode?: unknown
}

// Forma exacta que exige `CheckoutDetails` en el backend (`extra="forbid"`, `strict=True`). El mock
// la verifica para que un cambio en `checkoutDetails()` falle aquí y no solo en la app con un 422.
const DETAILS_FIELDS = {
  buyer: { required: ["email"], optional: ["first_name", "last_name", "phone_number"] },
  fulfillment: { required: ["name", "email", "phone_number", "address"], optional: [] },
  address: {
    required: ["name", "line_one", "city", "state", "country", "postal_code"],
    optional: [],
  },
} as const

function objectProblem(
  label: string,
  value: unknown,
  fields: { required: readonly string[]; optional: readonly string[] },
) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return `${label} no es un objeto`
  }
  const keys = Object.keys(value)
  const missing = fields.required.filter((key) => !keys.includes(key))
  if (missing.length > 0) return `${label}: faltan ${missing.join(", ")}`
  const unknown = keys.filter(
    (key) => !fields.required.includes(key) && !fields.optional.includes(key),
  )
  if (unknown.length > 0) return `${label}: sobran ${unknown.join(", ")}`
  return null
}

/** Motivo por el que el backend real rechazaría el cuerpo con 422, o `null` si lo aceptaría. */
export function requestProblem(body: AssistantRequestBody): string | null {
  if (typeof body.turn_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(body.turn_id)) {
    return "turn_id inválido"
  }
  if (body.mode !== undefined && body.mode !== "present" && body.mode !== "delegate") {
    return "mode inválido"
  }
  if (!(body.commands ?? []).some((command) => command.type === "add-message")) return null
  const details = body.details as { buyer?: unknown; fulfillment?: unknown } | undefined
  const problem = objectProblem("details", details, {
    required: ["buyer", "fulfillment"],
    optional: [],
  })
  if (problem || !details) return problem
  const fulfillment = details.fulfillment as { address?: unknown }
  return (
    objectProblem("buyer", details.buyer, DETAILS_FIELDS.buyer) ??
    objectProblem("fulfillment", fulfillment, DETAILS_FIELDS.fulfillment) ??
    objectProblem("address", fulfillment.address, DETAILS_FIELDS.address)
  )
}

const purchaseOutcomes = new Map<string, "completed" | "canceled">()
let requestCount = 0

/** Lo llaman las rutas REST simuladas para que `purchase-updated` elija el fixture correcto. */
export function recordPurchaseOutcome(runId: string, outcome: "completed" | "canceled") {
  purchaseOutcomes.set(runId, outcome)
}

/** Cómo terminó una compra en la historia, o `undefined` si sigue esperando la aprobación. */
export function purchaseOutcome(runId: string) {
  return purchaseOutcomes.get(runId)
}

/** Elige el fixture según el último comando, como lo haría el backend real. */
export function scriptedFixture(body: AssistantRequestBody): AssistantFixture {
  const command = body.commands?.at(-1)
  if (command?.type === "purchase-updated") {
    return purchaseOutcomes.get(command.runId ?? "") === "canceled"
      ? "purchaseCanceled"
      : "purchaseCompleted"
  }
  const text = (command?.message?.parts ?? [])
    .map((part) => part.text ?? "")
    .join(" ")
    .toLowerCase()
  if (body.state?.purchase?.stage === "awaiting_confirmation") return "pendingPurchase"
  if (/^\s*(hola|buenas|hey)\b/.test(text)) return "greeting"
  // «Delegar»: busca y deja el borrador de la delegación, sin checkout.
  if (body.mode === "delegate" && /aud[ií]fono|headphone/.test(text)) return "delegate"
  if (/\b50 mil\b/.test(text)) return "noCandidate"
  // Pedida otra vez después de cancelar: otra compra, con su propio `run_id`.
  if (/aud[ií]fono|headphone/.test(text)) {
    return body.state?.purchase?.stage === "canceled" ? "purchaseRepeated" : "purchase"
  }
  return "clarify"
}

/** Ruta `POST /assistant` interactiva: saluda, aclara, compra o recuerda la compra pendiente. */
export function scriptedAssistant(
  request: AgentApiRequest,
  frameDelayMs = 80,
): AgentApiReply {
  const body = (request.body ?? {}) as AssistantRequestBody
  const problem = requestProblem(body)
  if (problem) {
    // Igual que el backend: 422 saneado y sin abrir el stream. El motivo solo va a la consola.
    console.error(`El cuerpo de /assistant no cumple el contrato (${problem})`)
    return { status: 422, body: { code: "invalid_request", message: "La solicitud no es válida." } }
  }
  const state: AgentState =
    body.state && Array.isArray(body.state.messages) ? body.state : { messages: [], purchase: null }
  requestCount += 1
  return {
    sse: rebaseFixture(scriptedFixture(body), state, `r${requestCount}`),
    frameDelayMs,
  }
}
