import type { Preview } from "@storybook/react-vite"
import { sb } from "storybook/test"

import "../src/index.css"
import "./storybook.css"

import { mockAgentApi, type AgentApiMock } from "../src/storybook/agent-api-mock"
import { DEFAULT_AGENT_API } from "../src/storybook/default-agent-api"
import { mockSnapshotSource, type SnapshotReply } from "../src/storybook/governance"
import { mockSession, type StorySession } from "../src/storybook/session"

// Better Auth se reemplaza por src/lib/__mocks__/auth-client.ts: ninguna historia llama a /api/auth.
sb.mock(import("../src/lib/auth-client.ts"))

const preview: Preview = {
  parameters: {
    layout: "centered",
    // Toda llamada al agente se resuelve localmente; las historias sobrescriben rutas puntuales.
    agentApi: DEFAULT_AGENT_API,
    // Sesión de Better Auth simulada: "authenticated", "anonymous" o "pending".
    session: "authenticated",
    // Lecturas del servidor local de snapshots del monitor; sin servidor por defecto.
    governanceSnapshot: ["unreachable"],
    // Una violación de axe hace fallar la historia en `npm run test-storybook`.
    a11y: { test: "error" },
    backgrounds: {
      options: {
        white: { name: "Blanco", value: "#ffffff" },
        muted: { name: "Gris", value: "#f4f4f5" },
      },
    },
    viewport: {
      options: {
        mobile: { name: "Móvil", styles: { width: "390px", height: "844px" } },
        tablet: { name: "Tablet", styles: { width: "820px", height: "1180px" } },
        desktop: { name: "Escritorio", styles: { width: "1440px", height: "900px" } },
      },
    },
  },
  initialGlobals: {
    backgrounds: { value: "white" },
  },
  beforeEach: ({ parameters }) => {
    mockSession(parameters.session as StorySession)
    // El mock del agente instala su `fetch` la primera vez; el del snapshot va encima y se retira.
    const releaseAgentApi = mockAgentApi(parameters.agentApi as AgentApiMock)
    const releaseSnapshot = mockSnapshotSource(parameters.governanceSnapshot as SnapshotReply[])
    return () => {
      releaseSnapshot()
      releaseAgentApi()
    }
  },
}

export default preview
