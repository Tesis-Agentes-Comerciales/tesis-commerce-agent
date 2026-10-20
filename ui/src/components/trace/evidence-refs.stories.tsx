import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, spyOn, waitFor } from "storybook/test"

import { EvidenceRefs } from "@/components/trace/evidence-refs"
import { decisionOf, TRACES } from "@/storybook/trace-fixtures"

const TRACE = TRACES.completedShadow
const DECISION = decisionOf(TRACE)

const meta = {
  title: "Trace/EvidenceRefs",
  component: EvidenceRefs,
  tags: ["autodocs"],
  args: {
    refs: [
      { label: "Compra", value: TRACE.run_id },
      { label: "Decisión", value: DECISION.decision_id },
      { label: "Evidencia", value: DECISION.evidence_id },
    ],
  },
  decorators: [
    (Story) => (
      <div className="w-[min(32rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof EvidenceRefs>

export default meta
type Story = StoryObj<typeof meta>

/** Copiar deja el valor completo en el portapapeles y lo anuncia. */
export const WithIds: Story = {
  beforeEach: () => {
    const writeText = spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined)
    return () => writeText.mockRestore()
  },
  play: async ({ canvas, userEvent }) => {
    await expect(canvas.getByText(DECISION.decision_id)).toBeVisible()
    await userEvent.click(canvas.getByRole("button", { name: "Copiar decisión" }))
    await expect(navigator.clipboard.writeText).toHaveBeenCalledWith(DECISION.decision_id)
    await waitFor(() => expect(canvas.getByText("Copiado")).toBeInTheDocument())
  },
}

/** Sin permiso de portapapeles se avisa a la vista; el valor sigue ahí para copiarlo a mano. */
export const CopyFails: Story = {
  beforeEach: () => {
    const writeText = spyOn(navigator.clipboard, "writeText").mockRejectedValue(
      new DOMException("Denied", "NotAllowedError"),
    )
    return () => writeText.mockRestore()
  },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Copiar compra" }))
    await waitFor(() => expect(canvas.getByText("No se pudo copiar")).toBeVisible())
  },
}

/**
 * Columna estrecha (el detalle técnico a 320 px): rótulo sobre valor, y el valor entero aunque se
 * parta en varias líneas. El botón de copiar queda al lado del valor.
 */
export const Narrow: Story = {
  args: {
    refs: [
      { label: "Compra", value: TRACE.run_id },
      { label: "Evidencia de frescura al aprobar", value: `sha256:${"a1b2c3d4".repeat(8)}` },
    ],
  },
  decorators: [
    (Story) => (
      <div className="w-[260px]">
        <Story />
      </div>
    ),
  ],
  play: async ({ canvas }) => {
    for (const value of [TRACE.run_id, `sha256:${"a1b2c3d4".repeat(8)}`]) {
      const text = canvas.getByText(value)
      await expect(text.scrollWidth).toBeLessThanOrEqual(text.clientWidth)
      const term = canvas.getByText(text.closest("div")?.querySelector("dt")?.textContent ?? "")
      await expect(text.getBoundingClientRect().top).toBeGreaterThanOrEqual(
        term.getBoundingClientRect().bottom - 1,
      )
    }
    await expect(canvas.getByRole("button", { name: "Copiar compra" })).toBeVisible()
  },
}

/** Con espacio, cada identificador va en una fila: rótulo a la izquierda y valor a la derecha. */
export const Wide: Story = {
  decorators: [
    (Story) => (
      <div className="w-[36rem]">
        <Story />
      </div>
    ),
  ],
  play: async ({ canvas }) => {
    const value = canvas.getByText(DECISION.decision_id)
    const term = canvas.getByText("Decisión")
    await expect(Math.abs(value.getBoundingClientRect().top - term.getBoundingClientRect().top)).toBeLessThan(4)
  },
}

export const WithoutIds: Story = {
  args: { refs: [] },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Esta compra todavía no tiene identificadores.")).toBeVisible()
    await expect(canvas.queryByRole("button")).toBeNull()
  },
}
