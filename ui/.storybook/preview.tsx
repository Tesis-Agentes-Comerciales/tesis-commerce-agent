import type { Preview } from "@storybook/react-vite"

import "../src/index.css"
import "./storybook.css"

import { mockAgentApi, type AgentApiMock } from "../src/storybook/agent-api-mock"
import { DEFAULT_AGENT_API } from "../src/storybook/fixtures"

const preview: Preview = {
  parameters: {
    layout: "centered",
    // Toda llamada al agente se resuelve localmente; las historias sobrescriben rutas puntuales.
    agentApi: DEFAULT_AGENT_API,
    a11y: { test: "todo" },
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
  beforeEach: ({ parameters }) => mockAgentApi(parameters.agentApi as AgentApiMock),
}

export default preview
