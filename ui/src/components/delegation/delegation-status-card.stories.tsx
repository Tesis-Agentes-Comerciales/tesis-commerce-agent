import type { Meta, StoryContext, StoryObj } from "@storybook/react-vite"
import { useState } from "react"
import { expect, fn, waitFor } from "storybook/test"

import { formatMoney } from "@/components/agent/agent-format"
import { DelegationConsentCard } from "@/components/delegation/delegation-consent-card"
import {
  DelegationStatusCard,
  type DelegationStatusCardProps,
} from "@/components/delegation/delegation-status-card"
import {
  escalationOf,
  expectFitsPhoneColumn,
  expectKeyboardFocus,
  PHONE_COLUMN,
  plain,
} from "@/components/delegation/story-helpers"
import {
  parseDelegationRevokeReceipt,
  parseDelegationView,
  type DelegationView,
} from "@/lib/delegation-contract"
import {
  DELEGATION_PROPOSAL,
  DELEGATIONS,
  REVOKE_RECEIPTS,
} from "@/storybook/fixtures/delegation-fixtures"
import { mouseClick } from "@/storybook/interactions"

const REVOKE = "Revocar"
const [NO_EFFECT, , IN_FLIGHT] = REVOKE_RECEIPTS.receipts

const meta = {
  title: "Delegation/StatusCard",
  component: DelegationStatusCard,
  tags: ["autodocs"],
  args: { delegation: DELEGATIONS.active, onRevoke: fn(), onBuyFromChat: fn() },
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof DelegationStatusCard>

export default meta
type Story = StoryObj<typeof meta>
type PlayContext = StoryContext<DelegationStatusCardProps>

/** El título del estado, el mensaje del backend tal cual y «Revocar» solo con el grant activo. */
async function expectStatus({ args, canvas }: PlayContext, title: string, revocable: boolean) {
  const { delegation } = args
  await expect(canvas.getByRole("heading", { name: title })).toBeVisible()
  if (delegation.task.message) await expect(canvas.getByText(delegation.task.message)).toBeVisible()
  await expect(canvas.queryAllByRole("button", { name: REVOKE })).toHaveLength(revocable ? 1 : 0)
  await expect(canvas.queryByRole("button", { name: "Comprar desde el chat" })).toBeNull()
}

/** Consentida y lista: el alcance completo y «Revocar». */
export const Active: Story = {
  play: async (context) => {
    const { args, canvas, userEvent } = context
    const { scope } = DELEGATIONS.active
    await expectStatus(context, "Delegación activa", true)
    await expect(canvas.getByText(scope.merchant_name)).toBeVisible()
    for (const { title } of scope.variants) await expect(canvas.getByText(title)).toBeVisible()
    await expect(canvas.getByText(`«${scope.preference_text}»`)).toBeVisible()
    await expect(canvas.getAllByRole("term").map((term) => term.textContent)).toEqual([
      "Total máximo",
      "Vence",
      "Entrega",
      "Pago",
      "Última actividad",
    ])
    const total = formatMoney(scope.total_max.minor, scope.total_max.currency)
    await expect(canvas.getByText(plain(total))).toBeVisible()
    await userEvent.click(canvas.getByRole("button", { name: REVOKE }))
    await expect(args.onRevoke).toHaveBeenCalledOnce()
  },
}

export const Running: Story = {
  args: { delegation: DELEGATIONS.running },
  play: (context) => expectStatus(context, "Compra en curso", true),
}

/** El mensaje del backend trae el producto, el total y la orden. */
export const Purchased: Story = {
  args: { delegation: DELEGATIONS.purchased },
  play: async (context) => {
    await expectStatus(context, "Compra completada", false)
    await expect(context.canvas.getByText(/ord_7d2c9a1e44/)).toBeVisible()
  },
}

/** D1 POL-11: «Necesito tu decisión», el texto canónico y el CTA que prellena la compra HP. */
export const Escalated: Story = {
  args: { delegation: DELEGATIONS.escalated },
  play: async ({ args, canvas, userEvent }) => {
    const escalation = escalationOf(DELEGATIONS.escalated)
    await expect(canvas.getByRole("heading", { name: "Necesito tu decisión" })).toBeVisible()
    await expect(canvas.getByText(escalation.message)).toBeVisible()
    await expect(canvas.queryByRole("button", { name: REVOKE })).toBeNull()
    await userEvent.click(canvas.getByRole("button", { name: "Comprar desde el chat" }))
    await expect(args.onBuyFromChat).toHaveBeenCalledWith(escalation.prefill_request)
  },
}

/** Revocada por el dueño antes del disparo. */
export const Revoked: Story = {
  args: { delegation: DELEGATIONS.revoked },
  play: (context) => expectStatus(context, "Delegación revocada", false),
}

export const Expired: Story = {
  args: { delegation: DELEGATIONS.expired },
  play: (context) => expectStatus(context, "Delegación vencida", false),
}

/** D2 negó con una política que no escala: falla por precaución. */
export const Failed: Story = {
  args: { delegation: DELEGATIONS.failed },
  play: (context) => expectStatus(context, "Compra no completada", false),
}

/** Timeout después de comprometer el pago: el grant sigue activo y revocar daría `in_flight`. */
export const Uncertain: Story = {
  args: { delegation: DELEGATIONS.uncertain },
  play: (context) => expectStatus(context, "Compra por confirmar", true),
}

/**
 * Kill switch del operador (§8.4): grant `revoked(operator)` y tarea `canceled(kill_switch)`. El
 * mensaje es el de `HNP_MESSAGES["task.kill_switch"]`, como lo enviaría el backend.
 */
export const KillSwitch: Story = {
  args: {
    delegation: parseDelegationView({
      ...DELEGATIONS.revoked,
      revoked_reason: "operator",
      task: {
        ...DELEGATIONS.revoked.task,
        outcome_code: "kill_switch",
        message:
          "El operador detuvo las compras delegadas. Cerré tu delegación y no se hizo ningún cobro. Si todavía quieres comprar, hazlo desde el chat.",
      },
    }),
  },
  play: (context) => expectStatus(context, "Compra cancelada", false),
}

/** B0-HNP nunca escala: la causa que en B2 escala falla con el mismo código y sin CTA. */
export const FailedWithEscalationCode: Story = {
  args: {
    delegation: parseDelegationView({
      ...DELEGATIONS.failed,
      task: { ...DELEGATIONS.failed.task, outcome_code: "no_candidate_in_scope" },
    }),
  },
  play: (context) => expectStatus(context, "Compra no completada", false),
}

/**
 * Revocada con el pago ya comprometido (§8.4): manda el grant mientras la tarea sigue, y el recibo
 * `in_flight` dice que esa compra continúa.
 */
export const RevokedWhileInFlight: Story = {
  args: {
    delegation: parseDelegationView({
      ...DELEGATIONS.running,
      state: "revoked",
      revoked_reason: "owner",
    }),
    revokeReceipt: { ...IN_FLIGHT, grant_id: DELEGATIONS.running.grant_id },
  },
  play: async (context) => {
    await expectStatus(context, "Delegación revocada", false)
    const receipt = context.canvas.getByTestId("delegation-revoke-receipt")
    await expect(receipt).toHaveAttribute("data-effect", "in_flight")
    await expect(receipt).toHaveTextContent(IN_FLIGHT.message)
  },
}

/** La orden llegó después de revocar (§4.4): el grant sigue `revoked`, pero hubo compra. */
export const PurchasedAfterRevocation: Story = {
  args: {
    delegation: parseDelegationView({
      ...DELEGATIONS.purchased,
      state: "revoked",
      revoked_reason: "owner",
    }),
  },
  play: (context) => expectStatus(context, "Compra completada", false),
}

/** El grant activo de la fixture, revocado por su dueño antes del disparo. */
const REVOKE_RECEIPT = parseDelegationRevokeReceipt({
  ...NO_EFFECT,
  grant_id: DELEGATIONS.active.grant_id,
})
const REVOKED_ACTIVE = parseDelegationView({
  ...DELEGATIONS.active,
  state: "revoked",
  revoked_reason: "owner",
  task: {
    ...DELEGATIONS.revoked.task,
    task_id: DELEGATIONS.active.task.task_id,
    updated_at: REVOKE_RECEIPT.revoked_at,
  },
  last_activity_at: REVOKE_RECEIPT.revoked_at,
})

/** Simula `POST /delegations/{id}/revoke` con un temporizador local; en la app viene de la API. */
function InteractiveStatusCard(props: DelegationStatusCardProps & { failFirst?: boolean }) {
  const { failFirst = false, ...card } = props
  const [delegation, setDelegation] = useState<DelegationView>(card.delegation)
  const [revoking, setRevoking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [attempts, setAttempts] = useState(0)

  const onRevoke = () => {
    card.onRevoke?.()
    setRevoking(true)
    setError(null)
    setAttempts((current) => current + 1)
    setTimeout(() => {
      setRevoking(false)
      if (failFirst && attempts === 0) setError("No pude comunicarme con el agente.")
      else setDelegation(REVOKED_ACTIVE)
    }, 300)
  }

  return (
    <DelegationStatusCard
      {...card}
      delegation={delegation}
      revoking={revoking}
      error={error}
      revokeReceipt={delegation === REVOKED_ACTIVE ? REVOKE_RECEIPT : null}
      onRevoke={onRevoke}
    />
  )
}

/**
 * Revocar con el teclado: «Revocar» conserva el foco mientras responde; revocada, el botón ya no
 * está y el foco pasa al título. El recibo se ve en la tarjeta y lo anuncia la región de estado.
 */
export const RevokeWithKeyboard: Story = {
  render: (args) => <InteractiveStatusCard {...args} />,
  play: async ({ args, canvas, canvasElement, userEvent }) => {
    const revoke = canvas.getByRole("button", { name: REVOKE })
    await userEvent.tab()
    await expectKeyboardFocus(revoke)
    await userEvent.keyboard("{Enter}")
    await expect(
      canvasElement.querySelector("[data-testid=delegation-status-card]"),
    ).toHaveAttribute("aria-busy", "true")
    await expect(revoke).toHaveAttribute("aria-disabled", "true")
    await expect(revoke).toHaveFocus()
    await userEvent.keyboard("{Enter}")
    const title = await canvas.findByRole("heading", { name: "Delegación revocada" })
    await waitFor(() => expect(title).toHaveFocus())
    await expect(canvas.getByTestId("delegation-revoke-receipt")).toHaveTextContent(
      REVOKE_RECEIPT.message,
    )
    await expect(canvas.getByRole("status")).toHaveTextContent(REVOKE_RECEIPT.message)
    await expect(canvas.queryByRole("button", { name: REVOKE })).toBeNull()
    await expect(args.onRevoke).toHaveBeenCalledOnce()
  },
}

/** Con el mouse el foco no salta al título: la región de estado anuncia el recibo. */
export const RevokeWithMouse: Story = {
  render: (args) => <InteractiveStatusCard {...args} />,
  play: async ({ canvas }) => {
    mouseClick(canvas.getByRole("button", { name: REVOKE }))
    const title = await canvas.findByRole("heading", { name: "Delegación revocada" })
    await expect(title).not.toHaveFocus()
    await expect(canvas.getByRole("status")).toHaveTextContent(REVOKE_RECEIPT.message)
  },
}

/** Revocar falló: el error lo anuncia su aviso y el botón conserva el foco para reintentar. */
export const RevokeFailedThenRetry: Story = {
  render: (args) => <InteractiveStatusCard {...args} failFirst />,
  play: async ({ args, canvas, userEvent }) => {
    const revoke = canvas.getByRole("button", { name: REVOKE })
    await userEvent.tab()
    await userEvent.keyboard("{Enter}")
    await expect(await canvas.findByRole("alert")).toHaveTextContent("No pude comunicarme")
    await expect(revoke).toHaveFocus()
    await expect(canvas.getByRole("status")).toBeEmptyDOMElement()
    await userEvent.keyboard("{Enter}")
    await waitFor(() =>
      expect(canvas.getByRole("heading", { name: "Delegación revocada" })).toHaveFocus(),
    )
    await expect(canvas.queryByRole("alert")).toBeNull()
    await expect(args.onRevoke).toHaveBeenCalledTimes(2)
  },
}

/** La tarjeta de consentimiento y, al autorizar, la de estado en su lugar (HNP-10 lo cablea). */
function ReplacesConsent(props: DelegationStatusCardProps) {
  const [consented, setConsented] = useState(false)
  if (consented) return <DelegationStatusCard {...props} autoFocus />
  return (
    <DelegationConsentCard proposal={DELEGATION_PROPOSAL} onAuthorize={() => setConsented(true)} />
  )
}

/** El botón pulsado desaparece con la tarjeta de consentimiento: el foco pasa al título nuevo. */
export const ReplacesConsentCard: Story = {
  render: (args) => <ReplacesConsent {...args} />,
  play: async ({ canvas, userEvent }) => {
    await userEvent.tab()
    await userEvent.tab()
    await expectKeyboardFocus(canvas.getByRole("button", { name: "Autorizar" }))
    await userEvent.keyboard("{Enter}")
    const title = await canvas.findByRole("heading", { name: "Delegación activa" })
    await expect(title).toHaveFocus()
    await userEvent.tab()
    await expectKeyboardFocus(canvas.getByRole("button", { name: REVOKE }))
  },
}

/** A 320 px cada estado cabe: título, alcance, filas, botones y recibo, sin desbordar. */
export const Phone320: Story = {
  render: (args) => (
    <div className={`${PHONE_COLUMN} space-y-3`} data-testid="phone-column">
      {Object.values(DELEGATIONS).map((delegation) => (
        <DelegationStatusCard
          key={delegation.grant_id + delegation.task.state}
          {...args}
          delegation={delegation}
        />
      ))}
      <DelegationStatusCard delegation={REVOKED_ACTIVE} revokeReceipt={REVOKE_RECEIPT} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectFitsPhoneColumn(canvasElement)
  },
}
