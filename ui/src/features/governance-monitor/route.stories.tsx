import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import GovernanceRoute, { EvidenceRoute } from "./route.tsx"
import { EVIDENCE } from "@/storybook/governance"

const meta = {
  title: "Governance/Route",
  component: GovernanceRoute,
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof GovernanceRoute>

export default meta
type Story = StoryObj<typeof meta>

type PlayContext = Parameters<NonNullable<Story["play"]>>[0]

// Solo existen si se mostró evidencia: ninguno puede aparecer sin sesión ni con un bundle inválido.
const EVIDENCE_TEXT = [/Reproducción offline/, ...EVIDENCE.scenarios.map((s) => s.label)]

async function expectNoEvidence({ canvas, canvasElement }: PlayContext) {
  await expect(canvas.queryByRole("region", { name: "Monitor de gobernanza" })).toBeNull()
  for (const text of EVIDENCE_TEXT) await expect(canvasElement).not.toHaveTextContent(text)
}

/** Better Auth aún no responde: solo el aviso de carga. */
export const SessionPending: Story = {
  parameters: { session: "pending" },
  play: async (context) => {
    await expect(context.canvas.getByText("Cargando sesión…")).toBeVisible()
    await expectNoEvidence(context)
  },
}

/** Sin sesión: el formulario de acceso de la app, nunca la evidencia. */
export const SignedOut: Story = {
  parameters: { session: "anonymous" },
  play: async (context) => {
    await expect(context.canvas.getByRole("button", { name: "Entrar" })).toBeVisible()
    await expectNoEvidence(context)
  },
}

/** Con sesión: el monitor en reproducción offline. */
export const SignedIn: Story = {
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("status")).toHaveTextContent(/^Reproducción offline/)
    await expect(canvas.getByRole("region", { name: "Monitor de gobernanza" })).toBeVisible()
  },
}

/** Un bundle de otra versión del contrato no se adapta: error visible y ninguna evidencia. */
export const ContractError: Story = {
  render: () => <EvidenceRoute raw={{ ...EVIDENCE, schema: "governance-monitor/2" }} />,
  play: async (context) => {
    await expect(context.canvas.getByRole("alert")).toHaveTextContent(/incompatible/)
    await expectNoEvidence(context)
  },
}

/** Una etiqueta «permitido» sin allow D1 aplicado invalida el bundle entero; no llega a mostrarse. */
export const PermissionLabelWithoutAllow: Story = {
  render: () => (
    <EvidenceRoute
      raw={{
        ...EVIDENCE,
        scenarios: [{ ...EVIDENCE.scenarios[0], label: "HP · permitido" }, ...EVIDENCE.scenarios.slice(1)],
      }}
    />
  ),
  play: async (context) => {
    await expect(context.canvas.getByRole("alert")).toHaveTextContent(/inconsistente/)
    await expect(context.canvasElement).not.toHaveTextContent(/permitid/i)
    await expectNoEvidence(context)
  },
}
