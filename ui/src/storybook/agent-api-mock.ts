import { AGENT_API_URL } from "@/lib/commerce-api"
import { delay } from "@/storybook/delay"

export type AgentApiRequest = {
  method: string
  path: string
  params: Record<string, string>
  body: unknown
}

export type AgentApiReply = {
  status?: number
  body?: unknown
  delayMs?: number
  /** Simula que el navegador no alcanza al agente (fetch rechaza con TypeError). */
  networkError?: boolean
  /**
   * Cuerpo `text/event-stream`: un texto SSE completo (se parte en líneas en blanco) o sus frames
   * ya separados. Tiene prioridad sobre `body`.
   */
  sse?: string | readonly string[]
  /** Pausa antes de cada frame SSE; por defecto 80 ms. */
  frameDelayMs?: number
}

export type AgentApiRoute =
  | AgentApiReply
  | ((request: AgentApiRequest) => AgentApiReply | Promise<AgentApiReply>)

/** Rutas con la forma `"POST /purchases/:runId/approval"`. */
export type AgentApiMock = Record<string, AgentApiRoute>

const nativeFetch = globalThis.fetch.bind(globalThis)
let activeMock: AgentApiMock = {}
let installed = false

function matchRoute(mock: AgentApiMock, method: string, path: string) {
  for (const [key, route] of Object.entries(mock)) {
    const [routeMethod, routePath] = key.split(" ")
    if (routeMethod !== method || !routePath) continue
    const names: string[] = []
    const pattern = routePath.replace(/:(\w+)/g, (_, name: string) => {
      names.push(name)
      return "([^/]+)"
    })
    const result = new RegExp(`^${pattern}$`).exec(path)
    if (!result) continue
    const params = Object.fromEntries(
      names.map((name, index) => [name, decodeURIComponent(result[index + 1] ?? "")]),
    )
    return { route, params }
  }
  return null
}

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

/** Frames de un cuerpo SSE, separados por líneas en blanco y sin espacios sobrantes. */
export function splitSseFrames(body: string) {
  return body
    .split(/\r?\n\r?\n/)
    .map((frame) => frame.trim())
    .filter(Boolean)
}

function sseResponse(frames: readonly string[], frameDelayMs: number, signal: AbortSignal) {
  const encoder = new TextEncoder()
  let index = 0
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (index >= frames.length) {
        controller.close()
        return
      }
      try {
        // El `set` inicial y el mensaje del usuario llegan juntos, como la ráfaga del backend.
        if (index !== 1) await delay(frameDelayMs, signal)
      } catch (reason) {
        // Abortar corta el stream como lo haría la red: el runtime ve el error del lector.
        controller.error(reason)
        return
      }
      controller.enqueue(encoder.encode(`${frames[index]}\n\n`))
      index += 1
    },
  })
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
  })
}

async function mockedFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const request = new Request(input, init)
  if (!request.url.startsWith(`${AGENT_API_URL}/`)) return nativeFetch(input, init)

  const path = new URL(request.url).pathname
  const matched = matchRoute(activeMock, request.method, path)
  if (!matched) {
    console.warn(`[agent-api-mock] Sin mock para ${request.method} ${path}`)
    return jsonResponse(501, {
      code: "storybook_unmocked",
      message: `Storybook no simula ${request.method} ${path}`,
    })
  }

  const text = await request.text()
  const reply =
    typeof matched.route === "function"
      ? await matched.route({
          method: request.method,
          path,
          params: matched.params,
          body: text ? JSON.parse(text) : undefined,
        })
      : matched.route

  await delay(reply.delayMs ?? 0, request.signal)
  if (reply.networkError) throw new TypeError("Failed to fetch")
  if (reply.sse !== undefined) {
    const frames = typeof reply.sse === "string" ? splitSseFrames(reply.sse) : reply.sse
    return sseResponse(frames, reply.frameDelayMs ?? 80, request.signal)
  }
  // `body: null` es una respuesta válida (p. ej. sin compra pendiente); solo la ausencia da `{}`.
  return jsonResponse(reply.status ?? 200, reply.body === undefined ? {} : reply.body)
}

/**
 * Activa las rutas simuladas para la historia actual. Las peticiones al agente nunca salen del
 * navegador: una ruta sin mock responde 501 en lugar de tocar un backend real.
 */
export function mockAgentApi(mock: AgentApiMock = {}) {
  if (!installed) {
    globalThis.fetch = mockedFetch
    installed = true
  }
  activeMock = mock
  return () => {
    activeMock = {}
  }
}
