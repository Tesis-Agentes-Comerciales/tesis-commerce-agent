import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, fn, within } from "storybook/test"

import { DelegationList, type DelegationRevocation } from "@/components/delegation/delegation-list"
import { expectFitsPhoneColumn, PHONE_COLUMN } from "@/components/delegation/story-helpers"
import {
  parseDelegationList,
  parseDelegationRevokeAllReceipt,
  type DelegationView,
} from "@/lib/delegation-contract"
import { DELEGATIONS } from "@/storybook/fixtures/delegation-fixtures"
import { DELEGATION_REPLAYS, replayed } from "@/storybook/fixtures/delegation-replays"

// Las respuestas reales de «Revocar todo» (delegation-api/revoke-all.json): antes, el recibo y
// después.
const BEFORE = parseDelegationList(
  replayed(DELEGATION_REPLAYS.revokeAll, "GET /me/delegations", 0).body,
).delegations
const AFTER = parseDelegationList(
  replayed(DELEGATION_REPLAYS.revokeAll, "GET /me/delegations", 1).body,
).delegations
const { receipts } = parseDelegationRevokeAllReceipt(
  replayed(DELEGATION_REPLAYS.revokeAll, "POST /me/delegations/revoke-all").body,
)
const RECEIPTS: Record<string, DelegationRevocation> = Object.fromEntries(
  receipts.map((receipt) => [receipt.grant_id, { revoking: false, error: null, receipt }]),
)
const activeIds = (delegations: DelegationView[]) =>
  delegations.filter((item) => item.state === "active").map((item) => item.grant_id)

const meta = {
  title: "Delegation/List",
  component: DelegationList,
  tags: ["autodocs"],
  args: { delegations: BEFORE, onRevoke: fn(), onRevokeAll: fn(), onBuyFromChat: fn() },
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof DelegationList>

export default meta
type Story = StoryObj<typeof meta>

/** Tres activas (sin reserva, con la reserva tomada y con el pago comprometido) y una revocada. */
export const Mine: Story = {
  play: async ({ args, canvas, userEvent }) => {
    await expect(canvas.getByRole("heading", { name: "Mis delegaciones" })).toBeVisible()
    const cards = canvas.getAllByTestId("delegation-status-card")
    await expect(cards).toHaveLength(BEFORE.length)
    await expect(canvas.getAllByRole("button", { name: "Revocar" })).toHaveLength(3)
    const [first] = activeIds(BEFORE)
    const firstCard = cards[BEFORE.findIndex((item) => item.grant_id === first)]
    await userEvent.click(within(firstCard).getByRole("button", { name: "Revocar" }))
    await expect(args.onRevoke).toHaveBeenCalledWith(first)
    await userEvent.click(canvas.getByRole("button", { name: "Revocar todo" }))
    await expect(args.onRevokeAll).toHaveBeenCalledOnce()
  },
}

/** Mientras «Revocar todo» responde, la lista está ocupada y el botón conserva el foco. */
export const RevokingAll: Story = {
  args: {
    revokingAll: true,
    revocations: Object.fromEntries(
      activeIds(BEFORE).map((id) => [id, { revoking: true, error: null, receipt: null }]),
    ),
  },
  play: async ({ args, canvas, userEvent }) => {
    await expect(canvas.getByTestId("delegation-list")).toHaveAttribute("aria-busy", "true")
    const revokeAll = canvas.getByRole("button", { name: "Revocar todo" })
    await expect(revokeAll).toHaveAttribute("aria-disabled", "true")
    revokeAll.focus()
    await userEvent.keyboard("{Enter}")
    await expect(args.onRevokeAll).not.toHaveBeenCalled()
  },
}

/** Después de «Revocar todo»: cada tarjeta releída con su recibo, y ya nada que revocar. */
export const AfterRevokeAll: Story = {
  args: { delegations: AFTER, revocations: RECEIPTS },
  play: async ({ canvas }) => {
    await expect(canvas.queryByRole("button", { name: "Revocar todo" })).toBeNull()
    await expect(canvas.queryByRole("button", { name: "Revocar" })).toBeNull()
    const shown = canvas.getAllByTestId("delegation-revoke-receipt")
    await expect(shown.map((receipt) => receipt.dataset.effect).sort()).toEqual([
      "in_flight",
      "no_effect",
      "released",
    ])
  },
}

/** Una de cada estado (las fixtures del contrato): solo las activas ofrecen «Revocar». */
export const EveryState: Story = {
  args: { delegations: Object.values(DELEGATIONS) },
  play: async ({ canvas }) => {
    const active = Object.values(DELEGATIONS).filter((item) => item.state === "active")
    await expect(canvas.getAllByRole("button", { name: "Revocar" })).toHaveLength(active.length)
    await expect(canvas.getByRole("button", { name: "Comprar desde el chat" })).toBeVisible()
  },
}

export const Empty: Story = {
  args: { delegations: [] },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Todavía no tienes delegaciones.")).toBeVisible()
    await expect(canvas.queryByRole("button", { name: "Revocar todo" })).toBeNull()
  },
}

export const Loading: Story = {
  args: { delegations: [], loading: true },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Cargando tus delegaciones…")).toBeVisible()
    await expect(canvas.getByTestId("delegation-list")).toHaveAttribute("aria-busy", "true")
  },
}

export const LoadFailed: Story = {
  args: { delegations: [], error: "No pude comunicarme con el agente." },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("alert")).toHaveTextContent("No pude comunicarme con el agente.")
  },
}

/** A 320 px la cabecera con «Revocar todo», cada tarjeta y los recibos caben sin recortarse. */
export const Phone320: Story = {
  args: { delegations: [...BEFORE, ...AFTER.slice(0, 1)], revocations: RECEIPTS },
  render: (args) => (
    <div className={PHONE_COLUMN} data-testid="phone-column">
      <DelegationList {...args} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectFitsPhoneColumn(canvasElement)
  },
}
