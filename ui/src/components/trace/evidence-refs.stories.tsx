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

export const WithoutIds: Story = {
  args: { refs: [] },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Esta compra todavía no tiene identificadores.")).toBeVisible()
    await expect(canvas.queryByRole("button")).toBeNull()
  },
}
