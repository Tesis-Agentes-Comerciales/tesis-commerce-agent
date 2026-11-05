import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, fn, within } from "storybook/test"

import { formatDateTime, formatMoneyParts } from "@/components/agent/agent-format"
import { DelegationConsentCard } from "@/components/delegation/delegation-consent-card"
import {
  expectFitsPhoneColumn,
  expectKeyboardFocus,
  PHONE_DIALOG_COLUMN,
  plain,
} from "@/components/delegation/story-helpers"
import { parseDelegationProposal } from "@/lib/delegation-contract"
import { DELEGATION_PROPOSAL } from "@/storybook/fixtures/delegation-fixtures"
import { DELEGATION_REPLAYS, replayed } from "@/storybook/fixtures/delegation-replays"
import { mouseClick } from "@/storybook/interactions"

const { scope } = DELEGATION_PROPOSAL
const CONSENT = {
  proposal_id: DELEGATION_PROPOSAL.proposal_id,
  scope_digest: DELEGATION_PROPOSAL.scope_digest,
}

const meta = {
  title: "Delegation/ConsentCard",
  component: DelegationConsentCard,
  tags: ["autodocs"],
  args: { proposal: DELEGATION_PROPOSAL, onAuthorize: fn(), onCancel: fn() },
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof DelegationConsentCard>

export default meta
type Story = StoryObj<typeof meta>

/**
 * La propuesta de `POST /delegations/proposals`: alcance, tope, vigencia y comercio exactos, con
 * los textos fijos de §8.3. «Autorizar» devuelve el digest de lo que se mostró.
 */
export const Proposed: Story = {
  play: async ({ args, canvas, userEvent }) => {
    await expect(
      canvas.getByRole("heading", { name: "Autorizar una compra sin ti" }),
    ).toBeVisible()
    await expect(canvas.getByText(scope.merchant_name)).toBeVisible()
    await expect(canvas.getByText("El total máximo incluye envío")).toBeVisible()
    const total = formatMoneyParts(scope.total_max.minor, scope.total_max.currency)
    await expect(canvas.getByText(plain(total.amount))).toBeVisible()
    for (const { title } of scope.variants) await expect(canvas.getByText(title)).toBeVisible()
    await expect(canvas.getByText(`«${scope.preference_text}»`)).toBeVisible()
    await expect(canvas.getByText(scope.destination_label)).toBeVisible()
    await expect(canvas.getByText(scope.instrument_label)).toBeVisible()
    await expect(canvas.getByText("1 unidad")).toBeVisible()
    await expect(canvas.getByText("Un solo uso")).toBeVisible()
    const expires = plain(`Vence el ${formatDateTime(scope.expires_at)}`)
    await expect(canvas.getByText(expires)).toBeVisible()
    // La propuesta vence antes que la delegación: se autoriza hasta el vencimiento de la propuesta.
    const deadline = `Puedes autorizarla hasta el ${formatDateTime(DELEGATION_PROPOSAL.expires_at)}`
    await expect(canvas.getByText(plain(deadline))).toBeVisible()
    await expect(
      canvas.getByText("Puedes revocarla antes de que el agente comprometa el pago"),
    ).toBeVisible()
    // Sin orden ni revisión prometida, la tarjeta no habla de ninguno de los dos (D15).
    await expect(canvas.queryByText(IN_ORDER)).toBeNull()
    await expect(canvas.queryByText(REVIEWED)).toBeNull()
    await expect(canvas.queryByText(NOT_REVIEWED)).toBeNull()
    // Ni el digest ni los ids: son para el servidor, no para el comprador.
    await expect(canvas.queryByText(DELEGATION_PROPOSAL.scope_digest)).toBeNull()
    await expect(canvas.queryByText(DELEGATION_PROPOSAL.proposal_id)).toBeNull()
    await expect(canvas.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Cancelar",
      "Autorizar",
    ])
    await userEvent.click(canvas.getByRole("button", { name: "Autorizar" }))
    await expect(args.onAuthorize).toHaveBeenCalledWith(CONSENT)
    await expect(args.onCancel).not.toHaveBeenCalled()
  },
}

// Comprar en orden y la revisión de la indicación (p0-conditional-intent.md §3.10, D15).

const IN_ORDER = "Compra la primera opción disponible dentro del total, en este orden:"
const REVIEWED =
  "Antes de comprar, el agente revisa tu indicación. Si no confirma que la opción elegida la respeta, no compra y te avisa."
const NOT_REVIEWED =
  "Tu indicación queda guardada, pero no cambia la elección ni se revisa antes de comprar."
const [BOSE, SONY] = scope.variants
const SONY_FIRST = [SONY.variant_id, BOSE.variant_id]

const withScope = (change: object) =>
  parseDelegationProposal({ ...DELEGATION_PROPOSAL, scope: { ...scope, ...change } })

/** Los títulos del orden consentido, como se leen. */
function orderOf(canvas: Parameters<NonNullable<Story["play"]>>[0]["canvas"]) {
  const list = canvas.getByRole("list", { name: IN_ORDER })
  return within(list).getAllByRole("listitem").map((item) => item.textContent)
}

/**
 * Con orden y sin indicación: la tarjeta dice el orden que se consiente, no el alfabético del
 * alcance, y no habla de revisión.
 */
export const InOrder: Story = {
  args: { proposal: withScope({ priority: SONY_FIRST, preference_text: null }) },
  play: async ({ args, canvas, userEvent }) => {
    await expect(canvas.getByText(IN_ORDER)).toBeVisible()
    await expect(orderOf(canvas)).toEqual([SONY.title, BOSE.title])
    await expect(canvas.queryByText(REVIEWED)).toBeNull()
    await expect(canvas.queryByText(NOT_REVIEWED)).toBeNull()
    await userEvent.click(canvas.getByRole("button", { name: "Autorizar" }))
    await expect(args.onAuthorize).toHaveBeenCalledWith(CONSENT)
  },
}

/**
 * D15-tarjeta-rev: con orden, indicación y `preference_review`, la tarjeta promete la revisión: el
 * backend la calculó porque el revisor está configurado.
 */
export const InOrderWithReview: Story = {
  args: { proposal: withScope({ priority: SONY_FIRST, preference_review: true }) },
  play: async ({ canvas }) => {
    await expect(orderOf(canvas)).toEqual([SONY.title, BOSE.title])
    await expect(canvas.getByText(`«${scope.preference_text}»`)).toBeVisible()
    await expect(canvas.getByText(REVIEWED)).toBeVisible()
    await expect(canvas.queryByText(NOT_REVIEWED)).toBeNull()
  },
}

/**
 * D15-tarjeta-sin-rev: con orden e indicación, pero sin `preference_review`, la tarjeta no promete
 * lo que no va a pasar: la indicación no cambia la elección ni se revisa.
 */
export const InOrderWithoutReview: Story = {
  args: { proposal: withScope({ priority: SONY_FIRST }) },
  play: async ({ canvas }) => {
    await expect(orderOf(canvas)).toEqual([SONY.title, BOSE.title])
    await expect(canvas.getByText(NOT_REVIEWED)).toBeVisible()
    await expect(canvas.queryByText(REVIEWED)).toBeNull()
  },
}

/** Sin orden y con revisión: el selector usa la indicación y el revisor la revisa antes de comprar. */
export const ReviewWithoutOrder: Story = {
  args: { proposal: withScope({ preference_review: true }) },
  play: async ({ canvas }) => {
    await expect(canvas.queryByText(IN_ORDER)).toBeNull()
    for (const { title } of scope.variants) await expect(canvas.getByText(title)).toBeVisible()
    await expect(canvas.getByText(REVIEWED)).toBeVisible()
    await expect(canvas.queryByText(NOT_REVIEWED)).toBeNull()
  },
}

/** A 320 px el orden, la indicación y la frase de revisión caben sin desbordar. */
export const InOrderPhone320: Story = {
  args: { proposal: withScope({ priority: SONY_FIRST, preference_review: true }) },
  render: (args) => (
    <div className={PHONE_DIALOG_COLUMN} data-testid="phone-column">
      <DelegationConsentCard {...args} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectFitsPhoneColumn(canvasElement)
  },
}

/** Cancelar no autoriza nada. */
export const Cancel: Story = {
  play: async ({ args, canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Cancelar" }))
    await expect(args.onCancel).toHaveBeenCalledOnce()
    await expect(args.onAuthorize).not.toHaveBeenCalled()
  },
}

/** Una sola variante y sin indicación del comprador: no aparece el bloque de la indicación. */
export const SingleVariantWithoutPreference: Story = {
  args: {
    proposal: parseDelegationProposal({
      ...DELEGATION_PROPOSAL,
      scope: { ...scope, variants: [scope.variants[1]], preference_text: null },
    }),
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("listitem")).toHaveTextContent(scope.variants[1].title)
    await expect(canvas.queryByText(scope.variants[0].title)).toBeNull()
    await expect(canvas.queryByText("Tu indicación")).toBeNull()
  },
}

/**
 * UX10 (D3): el comprador pidió dos unidades en su indicación. La tarjeta la cita tal cual y dice lo
 * que autoriza: 1 unidad, un solo uso.
 */
export const TwoUnitsRequested: Story = {
  args: {
    proposal: parseDelegationProposal({
      ...DELEGATION_PROPOSAL,
      scope: { ...scope, preference_text: "Quiero dos unidades, una para mi hermana." },
    }),
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("«Quiero dos unidades, una para mi hermana.»")).toBeVisible()
    await expect(canvas.getByText("1 unidad")).toBeVisible()
    await expect(canvas.getByText("Un solo uso")).toBeVisible()
  },
}

/**
 * Respuesta real con `valid_for_seconds = 60`: la propuesta vale 10 min (TTL), pero la delegación
 * vence al minuto, y el servidor rechaza autorizar después. Se autoriza hasta lo que llegue primero.
 */
export const DelegationLapsesBeforeProposal: Story = {
  args: {
    proposal: parseDelegationProposal(
      replayed(DELEGATION_REPLAYS.proposalExpired, "POST /delegations/proposals").body,
    ),
  },
  play: async ({ args, canvas }) => {
    const { expires_at: proposalExpiry, scope: lapsing } = args.proposal
    await expect(Date.parse(lapsing.expires_at)).toBeLessThan(Date.parse(proposalExpiry))
    const deadline = `Puedes autorizarla hasta el ${formatDateTime(lapsing.expires_at)}`
    await expect(canvas.getByText(plain(deadline))).toBeVisible()
    await expect(canvas.getByText(plain(deadline)).closest("time")).toHaveAttribute(
      "dateTime",
      lapsing.expires_at,
    )
  },
}

/**
 * Sin `onAuthorize` (la propuesta venció o el alcance no coincide): «Autorizar» se ve, pero
 * deshabilitado y sin efecto; «Cancelar» vuelve al formulario para pedir otra.
 */
export const WithoutAuthorize: Story = {
  args: {
    onAuthorize: undefined,
    error: "La propuesta venció antes de que la autorizaras. Crea una nueva.",
  },
  play: async ({ args, canvas, userEvent }) => {
    const authorize = canvas.getByRole("button", { name: "Autorizar" })
    await expect(authorize).toHaveAttribute("aria-disabled", "true")
    authorize.focus()
    await userEvent.keyboard("{Enter}")
    await expect(canvas.getByRole("alert")).toHaveTextContent("Crea una nueva")
    await userEvent.click(canvas.getByRole("button", { name: "Cancelar" }))
    await expect(args.onCancel).toHaveBeenCalledOnce()
  },
}

const SUPERSEDED = DELEGATION_REPLAYS.proposalSuperseded

/**
 * Respuesta real: otra pestaña o conversación del mismo comprador preparó otra propuesta, y solo la
 * última se puede autorizar (409 `delegation_proposal_superseded`, D2). Como la vencida, no se
 * reintenta: «Autorizar» queda deshabilitado y «Cancelar» vuelve al formulario.
 */
export const ProposalSuperseded: Story = {
  args: {
    proposal: parseDelegationProposal(replayed(SUPERSEDED, "POST /delegations/proposals").body),
    onAuthorize: undefined,
    error: (replayed(SUPERSEDED, "POST /delegations").body as { message?: string }).message,
  },
  play: async ({ args, canvas, userEvent }) => {
    await expect(canvas.getByRole("alert")).toHaveTextContent(
      "Esta propuesta fue reemplazada por una más reciente. Revisa la última.",
    )
    const authorize = canvas.getByRole("button", { name: "Autorizar" })
    await expect(authorize).toHaveAttribute("aria-disabled", "true")
    authorize.focus()
    await userEvent.keyboard("{Enter}")
    await userEvent.click(canvas.getByRole("button", { name: "Cancelar" }))
    await expect(args.onCancel).toHaveBeenCalledOnce()
  },
}

/** Dos propuestas en la misma página: son `article`, así que axe no ve regiones repetidas. */
export const TwoProposals: Story = {
  render: (args) => (
    <div className="space-y-3">
      <DelegationConsentCard {...args} />
      <DelegationConsentCard {...args} />
    </div>
  ),
  play: async ({ canvas }) => {
    await expect(canvas.getAllByRole("article")).toHaveLength(2)
    await expect(canvas.queryAllByRole("region")).toHaveLength(0)
  },
}

/** Mientras `POST /delegations` responde, ningún botón actúa y el que se pulsó sigue ahí. */
export const Authorizing: Story = {
  args: { authorizing: true },
  play: async ({ args, canvas, canvasElement, userEvent }) => {
    await expect(
      canvasElement.querySelector("[data-testid=delegation-consent-card]"),
    ).toHaveAttribute("aria-busy", "true")
    for (const button of canvas.getAllByRole("button")) {
      await expect(button).toHaveAttribute("aria-disabled", "true")
      button.focus()
      await userEvent.keyboard("{Enter}")
    }
    await expect(args.onAuthorize).not.toHaveBeenCalled()
    await expect(args.onCancel).not.toHaveBeenCalled()
  },
}

/** Autorizar falló: el mensaje de la API y el mismo botón para reintentar. */
export const AuthorizeFailed: Story = {
  args: { error: "No pude comunicarme con el agente." },
  play: async ({ args, canvas, userEvent }) => {
    await expect(canvas.getByRole("alert")).toHaveTextContent("No pude comunicarme con el agente.")
    await userEvent.click(canvas.getByRole("button", { name: "Autorizar" }))
    await expect(args.onAuthorize).toHaveBeenCalledWith(CONSENT)
  },
}

/** Con el teclado: «Cancelar» y luego «Autorizar», con el anillo de foco visible. */
export const KeyboardAuthorize: Story = {
  play: async ({ args, canvas, userEvent }) => {
    await userEvent.tab()
    await expectKeyboardFocus(canvas.getByRole("button", { name: "Cancelar" }))
    await userEvent.tab()
    await expectKeyboardFocus(canvas.getByRole("button", { name: "Autorizar" }))
    await userEvent.keyboard("{Enter}")
    await expect(args.onAuthorize).toHaveBeenCalledWith(CONSENT)
  },
}

/** Con el mouse el botón recibe el foco, pero sin anillo: solo el teclado lo muestra. */
export const MouseAuthorize: Story = {
  play: async ({ args, canvas }) => {
    const authorize = canvas.getByRole("button", { name: "Autorizar" })
    mouseClick(authorize)
    await expect(authorize).toHaveFocus()
    await expect(authorize.matches(":focus-visible")).toBe(false)
    await expect(args.onAuthorize).toHaveBeenCalledOnce()
  },
}

/** A 320 px el total, las variantes, el vencimiento y los botones caben sin desbordar. */
export const Phone320: Story = {
  render: (args) => (
    <div className={PHONE_DIALOG_COLUMN} data-testid="phone-column">
      <DelegationConsentCard {...args} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectFitsPhoneColumn(canvasElement)
  },
}
