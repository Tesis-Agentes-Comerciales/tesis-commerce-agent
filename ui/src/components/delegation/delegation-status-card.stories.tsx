import type { Meta, StoryContext, StoryObj } from "@storybook/react-vite"
import { useState } from "react"
import { expect, fn, waitFor, within } from "storybook/test"

import { formatDateTime, formatMoney } from "@/components/agent/agent-format"
import { DelegationConsentCard } from "@/components/delegation/delegation-consent-card"
import {
  DelegationStatusCard,
  type DelegationStatusCardProps,
} from "@/components/delegation/delegation-status-card"
import {
  escalationOf,
  expectFitsPhoneColumn,
  expectKeyboardFocus,
  PHONE_DIALOG_COLUMN,
  plain,
} from "@/components/delegation/story-helpers"
import {
  parseDelegationRevokeReceipt,
  parseDelegationView,
  type DelegationView,
  type EscalationCode,
} from "@/lib/delegation-contract"
import type { PurchaseTrace } from "@/lib/trace-contract"
import {
  DELEGATION_PROPOSAL,
  DELEGATIONS,
  REVOKE_RECEIPTS,
} from "@/storybook/fixtures/delegation-fixtures"
import { mouseClick, runningAnimations } from "@/storybook/interactions"
import { withRunId } from "@/storybook/trace-fixtures"
import { HNP_TRACES } from "@/storybook/trace-fixtures/hnp"

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
  const { message } = delegation.task
  if (message) await expect(canvas.getByText(plain(message))).toBeVisible()
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

/** El mensaje del backend trae el producto, el total con la notación de la tarjeta (K16) y la orden. */
export const Purchased: Story = {
  args: { delegation: DELEGATIONS.purchased },
  play: async (context) => {
    await expectStatus(context, "Compra completada", false)
    await expect(DELEGATIONS.purchased.task.message).toContain(formatMoney(192_400_000, "COP"))
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
 * `in_flight` dice que esa compra continúa. La revocación es la última actividad, como en la vista
 * que relee el backend; la tarea conserva su hora.
 */
export const RevokedWhileInFlight: Story = {
  args: {
    delegation: parseDelegationView({
      ...DELEGATIONS.running,
      state: "revoked",
      revoked_reason: "owner",
      last_activity_at: IN_FLIGHT.revoked_at,
    }),
    revokeReceipt: { ...IN_FLIGHT, grant_id: DELEGATIONS.running.grant_id },
  },
  play: async (context) => {
    await expectStatus(context, "Delegación revocada", false)
    const receipt = context.canvas.getByTestId("delegation-revoke-receipt")
    await expect(receipt).toHaveAttribute("data-effect", "in_flight")
    await expect(receipt).toHaveTextContent(IN_FLIGHT.message)
    // La revocación es la última actividad: la fila y el recibo dicen la misma hora.
    const revokedAt = plain(formatDateTime(IN_FLIGHT.revoked_at) ?? "")
    await expect(context.canvas.getAllByText(revokedAt)).toHaveLength(2)
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
    <div className={`${PHONE_DIALOG_COLUMN} space-y-3`} data-testid="phone-column">
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

// La gobernanza de la compra delegada: la traza de `task.run_id` con la fila de actividad del chat.
// Las trazas son las fixtures HNP con el `run_id` de la tarea, como las devuelve el backend.

const GOVERNANCE = "Gobernanza de la compra"

/** La traza de una fixture HNP para la compra de esta delegación. */
function traceFor(delegation: DelegationView, trace: PurchaseTrace) {
  if (!delegation.task.run_id) throw new Error(`${delegation.grant_id} has no purchase`)
  return withRunId(trace, delegation.task.run_id)
}

/** Una delegación escalada por `reason`, con el texto que escribe el backend (§8.3). */
function escalatedBy(reason: EscalationCode, message: string) {
  const view = DELEGATIONS.escalated
  return parseDelegationView({
    ...view,
    task: { ...view.task, outcome_code: reason },
    escalation: { ...escalationOf(view), reason, message },
  })
}

const SUFFIX =
  "No firmé ni presenté ninguna autorización de pago, no se hizo ningún cobro y cerré la delegación. Si todavía quieres comprar, hazlo desde el chat y apruébala tú."

/** La compra delegada en marcha: D2 ya verificó y falta reservar, cerrar y despachar. */
const RUNNING_STEPS = HNP_TRACES.enforceCompleted.steps.slice(0, 6)
const RUNNING_TRACE = traceFor(DELEGATIONS.running, {
  ...HNP_TRACES.enforceCompleted,
  status: "in_progress",
  updated_at: RUNNING_STEPS.at(-1)?.at ?? HNP_TRACES.enforceCompleted.started_at,
  steps: RUNNING_STEPS,
})

/** D2-HNP negó por POL-08 y la decisión volvió al comprador (`terms_out_of_scope`). */
const POLICY_BLOCKED = escalatedBy(
  "terms_out_of_scope",
  `El total del comercio, con envío, o algún término quedó fuera de lo que autorizaste. ${SUFFIX}`,
)
/** La prueba del mandato abierto quedó sin resolver: D2 se detuvo sin reglas que fallen. */
const ESCALATED = escalatedBy(
  "authorization_profile_unresolved",
  `No pude comprobar que la compra encaje en tu autorización. ${SUFFIX}`,
)

/** El revisor de intención no confirmó la elección (docs/design/hnp-intent-review.md). */
const REVIEW_NOT_CONFIRMED = escalatedBy(
  "choice_not_confirmed",
  `No pude confirmar que la opción elegida sea la que pediste. ${SUFFIX}`,
)

/** El selector falló o no respondió: no hubo elección que revisar ni compra que trazar. */
const SELECTION_UNAVAILABLE = escalatedBy(
  "selection_unavailable",
  `No pude elegir entre las opciones que autorizaste porque el servicio que hace la elección falló o no respondió. ${SUFFIX}`,
)

/** La sección de la gobernanza y su fila; al abrirla, los títulos de su línea de tiempo. */
async function openGovernance({ canvas, userEvent }: PlayContext, summary: RegExp | string) {
  const governance = within(canvas.getByRole("group", { name: GOVERNANCE }))
  const row = governance.getByRole("button", { name: summary })
  await userEvent.click(row)
  await expect(row).toHaveAttribute("aria-expanded", "true")
  // El contenido se abre con un fundido: visible cuando termina.
  const timeline = governance.getByRole("list", { name: "Controles de la compra" })
  await waitFor(() => expect(timeline).toBeVisible())
  return Array.from(timeline.querySelectorAll(":scope > li")).map(
    (item) => item.querySelector("p")?.textContent,
  )
}

/**
 * Compra en curso: la fila gira con «Comprando…» y ya muestra lo que pasó (consentimiento, D1,
 * checkout, frescura y D2), sin inventar los controles que faltan.
 */
export const TraceRunning: Story = {
  args: { delegation: DELEGATIONS.running, trace: RUNNING_TRACE },
  play: async (context) => {
    await expectStatus(context, "Compra en curso", true)
    const titles = await openGovernance(context, /^Comprando…/)
    await expect(titles).toEqual([
      "Delegación autorizada",
      "Compra delegada en marcha",
      "Política de compra: permitida",
      "Checkout creado en el comercio",
      "Oferta vigente al cerrar",
      "Autorización delegada verificada",
    ])
    await expect(context.canvas.queryByText("No se ejecutó")).toBeNull()
  },
}

/**
 * Comprada: la misma traza que verá la auditoría. Las tres decisiones de `b2-hnp-v1`, el uso único
 * reservado y consumido, el pago, la orden y el detalle técnico con sus identificadores.
 */
export const TracePurchased: Story = {
  args: {
    delegation: DELEGATIONS.purchased,
    trace: traceFor(DELEGATIONS.purchased, HNP_TRACES.enforceCompleted),
  },
  play: async (context) => {
    await expectStatus(context, "Compra completada", false)
    const titles = await openGovernance(context, /^Compra completada en /)
    await expect(titles).toEqual([
      "Delegación autorizada",
      "Compra delegada en marcha",
      "Política de compra: permitida",
      "Checkout creado en el comercio",
      "Oferta vigente al cerrar",
      "Autorización delegada verificada",
      "Uso único reservado",
      "Autorización cerrada por el agente",
      "Despacho autorizado",
      "Pago autorizado",
      "Orden confirmada",
      "Uso único consumido",
    ])
    const governance = within(context.canvas.getByRole("group", { name: GOVERNANCE }))
    await expect(governance.getByText("Activa: detiene la compra si no cumple")).toBeVisible()
    await expect(governance.getByText("Detalle técnico")).toBeVisible()
  },
}

/** La política detuvo la compra antes de reservar: qué regla no se cumplió y lo que no pasó. */
export const TracePolicyBlocked: Story = {
  args: {
    delegation: POLICY_BLOCKED,
    trace: traceFor(POLICY_BLOCKED, HNP_TRACES.d2Denied),
  },
  play: async (context) => {
    await expect(context.canvas.getByRole("heading", { name: "Necesito tu decisión" })).toBeVisible()
    const titles = await openGovernance(context, /^Bloqueada por la política de compra/)
    await expect(titles.slice(5, 7)).toEqual(["La política no autorizó la compra", "Necesito tu decisión"])
    await expect(context.canvas.getByText("Compra dentro de lo que autorizaste")).toBeVisible()
    // Ni reserva, ni cierre, ni pago, ni orden: no se hizo ningún cobro.
    await expect(titles.slice(7)).toEqual([
      "Reservar el uso único",
      "Cerrar la autorización",
      "Autorizar el pago",
      "Confirmar la orden",
    ])
  },
}

/** Escalada sin una regla que falle: la autorización no se pudo verificar y vuelve al comprador. */
export const TraceEscalated: Story = {
  args: { delegation: ESCALATED, trace: traceFor(ESCALATED, HNP_TRACES.escalated) },
  play: async (context) => {
    const { args, canvas, userEvent } = context
    await expect(canvas.getByRole("heading", { name: "Necesito tu decisión" })).toBeVisible()
    const titles = await openGovernance(context, /^Detenida por precaución/)
    await expect(titles.slice(5, 7)).toEqual([
      "No se pudo verificar la autorización",
      "Necesito tu decisión",
    ])
    await userEvent.click(canvas.getByRole("button", { name: "Comprar desde el chat" }))
    await expect(args.onBuyFromChat).toHaveBeenCalledWith(ESCALATED.escalation?.prefill_request)
  },
}

/**
 * El revisor de intención no confirmó la elección: la compra se detuvo antes de la política, sin
 * checkout ni cobro, y la decisión vuelve al comprador con el mismo CTA de cualquier escalamiento.
 */
export const TraceReviewNotConfirmed: Story = {
  args: {
    delegation: REVIEW_NOT_CONFIRMED,
    trace: traceFor(REVIEW_NOT_CONFIRMED, HNP_TRACES.enforceReviewNotConfirmed),
  },
  play: async (context) => {
    const { args, canvas, userEvent } = context
    await expect(canvas.getByRole("heading", { name: "Necesito tu decisión" })).toBeVisible()
    await expect(canvas.getByText(escalationOf(REVIEW_NOT_CONFIRMED).message)).toBeVisible()
    const titles = await openGovernance(context, /^Detenida: elección sin confirmar/)
    await expect(titles.slice(0, 6)).toEqual([
      "Delegación autorizada",
      "Compra delegada en marcha",
      "Revisión de la elección",
      "Necesito tu decisión",
      "Revisar la política de compra",
      "Crear el checkout",
    ])
    await expect(canvas.getByText("No confirmada: contradice tu indicación.")).toBeVisible()
    await userEvent.click(canvas.getByRole("button", { name: "Comprar desde el chat" }))
    await expect(args.onBuyFromChat).toHaveBeenCalledWith(REVIEW_NOT_CONFIRMED.escalation?.prefill_request)
  },
}

/**
 * El selector falló o no respondió antes de elegir: nada se creó ni se cobró y la decisión vuelve
 * al comprador con el CTA de cualquier escalamiento. Como no hubo compra, no hay traza que mostrar.
 */
export const EscalatedSelectionUnavailable: Story = {
  args: { delegation: SELECTION_UNAVAILABLE, trace: null },
  play: async ({ args, canvas, userEvent }) => {
    const escalation = escalationOf(SELECTION_UNAVAILABLE)
    await expect(canvas.getByRole("heading", { name: "Necesito tu decisión" })).toBeVisible()
    await expect(canvas.getByText(escalation.message)).toBeVisible()
    await expect(canvas.queryByRole("group", { name: GOVERNANCE })).toBeNull()
    await expect(canvas.queryByRole("button", { name: REVOKE })).toBeNull()
    await userEvent.click(canvas.getByRole("button", { name: "Comprar desde el chat" }))
    await expect(args.onBuyFromChat).toHaveBeenCalledWith(escalation.prefill_request)
  },
}

/** Sin la traza (todavía no hay compra, o no se pudo leer) la tarjeta no muestra la gobernanza. */
export const WithoutTrace: Story = {
  args: { delegation: DELEGATIONS.running, trace: null },
  play: async ({ canvas }) => {
    await expect(canvas.queryByRole("group", { name: GOVERNANCE })).toBeNull()
  },
}

/** A 320 px las tarjetas con su gobernanza abierta caben en el diálogo, reglas y detalle incluidos. */
export const Phone320WithTrace: Story = {
  render: (args) => (
    <div className={`${PHONE_DIALOG_COLUMN} space-y-3`} data-testid="phone-column">
      <DelegationStatusCard {...args} delegation={DELEGATIONS.running} trace={RUNNING_TRACE} />
      <DelegationStatusCard
        {...args}
        delegation={DELEGATIONS.purchased}
        trace={traceFor(DELEGATIONS.purchased, HNP_TRACES.enforceCompleted)}
      />
      <DelegationStatusCard
        {...args}
        delegation={POLICY_BLOCKED}
        trace={traceFor(POLICY_BLOCKED, HNP_TRACES.d2Denied)}
      />
      <DelegationStatusCard
        {...args}
        delegation={ESCALATED}
        trace={traceFor(ESCALATED, HNP_TRACES.escalated)}
      />
    </div>
  ),
  play: async ({ canvas, canvasElement, userEvent }) => {
    for (const region of canvas.getAllByRole("group", { name: GOVERNANCE })) {
      await userEvent.click(within(region).getAllByRole("button")[0]!)
      const details = region.querySelector("details")
      if (details) details.open = true
    }
    await waitFor(() => expect(canvas.getAllByRole("list", { name: "Controles de la compra" })).toHaveLength(4))
    // Medido con todo quieto: a medio giro, la flecha de la fila ocupa más que su caja. Solo
    // siguen las que no terminan (el giro y el brillo de la compra en curso).
    const finite = () =>
      runningAnimations(canvasElement).filter(
        (animation) => animation.effect?.getComputedTiming().iterations !== Infinity,
      )
    await waitFor(() => expect(finite()).toEqual([]))
    await expectFitsPhoneColumn(canvasElement)
  },
}
