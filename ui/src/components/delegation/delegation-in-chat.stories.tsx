import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, fn, waitFor, within } from "storybook/test"

import { DelegationInChat } from "@/components/delegation/delegation-in-chat"
import { asDelegationDraft } from "@/features/agent-results"
import { DEMO_PROFILE } from "@/lib/commerce-types"
import type { DelegationView } from "@/lib/delegation-contract"
import { chatDraft } from "@/lib/delegation-draft"
import { fixtureState } from "@/storybook/assistant-transport"
import { DELEGATION_PROPOSAL, DELEGATIONS } from "@/storybook/fixtures/delegation-fixtures"
import { withRunId } from "@/storybook/trace-fixtures"
import { HNP_TRACES } from "@/storybook/trace-fixtures/hnp"

// La tarjeta de la compra delegada en el hilo (docs/design/hnp-chat-delegate.md). El borrador es el
// de `delegate.sse`, el stream real del backend para «Quiero audífonos negros por menos de 2
// millones» en el modo «Delegar».

const VIEW = asDelegationDraft(fixtureState("delegate").delegation_draft)
if (!VIEW) throw new Error("delegate.sse has no delegation_draft")
const DRAFT = chatDraft(VIEW, DEMO_PROFILE)
const GOVERNANCE = "Gobernanza de la compra"

const traceFor = (delegation: DelegationView) => {
  if (!delegation.task.run_id) throw new Error(`${delegation.grant_id} has no purchase`)
  return withRunId(HNP_TRACES.enforceCompleted, delegation.task.run_id)
}

const meta = {
  title: "Delegation/InChat",
  component: DelegationInChat,
  tags: ["autodocs"],
  args: {
    offers: VIEW.offers,
    draft: DRAFT,
    flow: { step: "form", submitting: false, error: null },
    onPropose: fn(),
    onCloseForm: fn(),
    onOpenForm: fn(),
    onAuthorize: fn(),
    onBackToForm: fn(),
    onBuyFromChat: fn(),
  },
  decorators: [
    (Story) => (
      <div className="w-[min(48rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof DelegationInChat>

export default meta
type Story = StoryObj<typeof meta>

/**
 * Recién llegado el turno: las opciones marcadas, el total con el presupuesto del mensaje y el
 * mensaje como indicación. Todo es editable y «Preparar» envía lo que quedó.
 */
export const Form: Story = {
  play: async ({ args, canvas, userEvent }) => {
    const form = within(canvas.getByTestId("delegation-form"))
    const boxes = form.getAllByRole("checkbox")
    await expect(boxes).toHaveLength(VIEW.offers.length)
    for (const box of boxes) await expect(box).toBeChecked()
    await expect(form.getByLabelText("Total máximo con envío (pesos)")).toHaveValue("2.000.000")
    await expect(form.getByLabelText("Tu indicación (opcional)")).toHaveValue(VIEW.preference_text)
    await userEvent.click(form.getByRole("button", { name: "Preparar la autorización" }))
    await expect(args.onPropose).toHaveBeenCalledWith(
      expect.objectContaining({
        variantIds: DRAFT.variantIds,
        total: "2.000.000",
        preference: VIEW.preference_text,
      }),
    )
  },
}

/** El mensaje no traía presupuesto: el total queda vacío y lo escribe el comprador. */
export const FormWithoutBudget: Story = {
  args: { draft: chatDraft({ ...VIEW, budget_minor: null }, DEMO_PROFILE) },
  play: async ({ canvas }) => {
    await expect(canvas.getByLabelText("Total máximo con envío (pesos)")).toHaveValue("")
  },
}

/** Mientras responde la propuesta, «Preparar» gira y conserva el foco. */
export const Proposing: Story = {
  args: { flow: { step: "form", submitting: true, error: null } },
  play: async ({ canvas }) => {
    const submit = canvas.getByRole("button", { name: "Preparar la autorización" })
    await expect(submit).toHaveAttribute("aria-disabled", "true")
  },
}

/** La propuesta exacta del servidor, sin texto del modelo: «Autorizar» firma ese alcance. */
export const Consent: Story = {
  args: {
    flow: {
      step: "consent",
      proposal: DELEGATION_PROPOSAL,
      authorizing: false,
      error: null,
      canAuthorize: true,
    },
  },
  play: async ({ args, canvas, userEvent }) => {
    const card = within(canvas.getByTestId("delegation-consent-card"))
    await userEvent.click(card.getByRole("button", { name: "Autorizar" }))
    await expect(args.onAuthorize).toHaveBeenCalledWith({
      proposal_id: DELEGATION_PROPOSAL.proposal_id,
      scope_digest: DELEGATION_PROPOSAL.scope_digest,
    })
    await userEvent.click(card.getByRole("button", { name: "Cancelar" }))
    await expect(args.onBackToForm).toHaveBeenCalled()
  },
}

/** Una propuesta vencida no se reintenta: el error se lee y «Autorizar» ya no firma. */
export const ConsentExpired: Story = {
  args: {
    flow: {
      step: "consent",
      proposal: DELEGATION_PROPOSAL,
      authorizing: false,
      error: "La propuesta venció. Prepara otra.",
      canAuthorize: false,
    },
  },
  play: async ({ args, canvas }) => {
    await expect(canvas.getByText("La propuesta venció. Prepara otra.")).toBeVisible()
    const authorize = canvas.getByRole("button", { name: "Autorizar" })
    await expect(authorize).toHaveAttribute("aria-disabled", "true")
    authorize.click()
    await expect(args.onAuthorize).not.toHaveBeenCalled()
  },
}

/** Autorizada: la tarea espera el disparo del lanzador. */
export const Scheduled: Story = {
  args: { delegation: DELEGATIONS.active },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("heading", { name: "Delegación activa" })).toBeVisible()
    await expect(canvas.queryByTestId("delegation-form")).toBeNull()
  },
}

/** El agente compra solo; la gobernanza aparece cuando llega la traza. */
export const Running: Story = {
  args: { delegation: DELEGATIONS.running },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("heading", { name: "Compra en curso" })).toBeVisible()
  },
}

/**
 * Compra completada con la gobernanza de la traza HNP: D1, D2, D3 y la orden. En el hilo sale
 * abierta y sus filas entran de a una.
 */
export const Purchased: Story = {
  args: { delegation: DELEGATIONS.purchased, trace: traceFor(DELEGATIONS.purchased) },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("heading", { name: "Compra completada" })).toBeVisible()
    const governance = within(canvas.getByRole("group", { name: GOVERNANCE }))
    const row = governance.getByRole("button", { name: /^Compra completada en / })
    await expect(row).toHaveAttribute("aria-expanded", "true")
    const timeline = governance.getByRole("list", { name: "Controles de la compra" })
    await expect(timeline).toBeVisible()
    await expect(within(timeline).queryByText("Orden confirmada")).toBeNull()
    await waitFor(() => expect(within(timeline).getByText("Orden confirmada")).toBeVisible(), {
      timeout: 8000,
    })
  },
}

/** La política escaló: la decisión vuelve al comprador como una compra en el chat. */
export const Escalated: Story = {
  args: { delegation: DELEGATIONS.escalated },
  play: async ({ args, canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Comprar desde el chat" }))
    await expect(args.onBuyFromChat).toHaveBeenCalledWith(
      DELEGATIONS.escalated.escalation?.prefill_request,
    )
  },
}

/** El comprador cerró el formulario: un botón lo vuelve a abrir con el mismo borrador. */
export const Closed: Story = {
  args: { flow: { step: "idle" } },
  play: async ({ args, canvas, userEvent }) => {
    await expect(canvas.queryByTestId("delegation-form")).toBeNull()
    await userEvent.click(canvas.getByRole("button", { name: "Delegar estas opciones" }))
    await expect(args.onOpenForm).toHaveBeenCalled()
  },
}
