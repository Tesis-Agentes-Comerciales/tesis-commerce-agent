import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, within } from "storybook/test"

import { formatMoney } from "@/components/agent/agent-format"
import { ConditionalBranches } from "@/components/agent/conditional-branches"
import { expectFitsPhoneColumn, PHONE_COLUMN, plain } from "@/components/delegation/story-helpers"
import { parseConditionalView, type ConditionalView } from "@/lib/agent-contract"
import FIXTURES from "@/storybook/fixtures/conditional-views.json"

// Las ramas de una intención condicional (docs/design/p0-conditional-intent.md §3.10), con las
// fixtures del contrato que también valida el backend. Las vistas que la fixture no trae (la
// tercera opción, el límite por unidad y «si A se agota») se derivan de ella y pasan por el mismo
// validador.

const VIEWS = Object.fromEntries(
  Object.entries(FIXTURES).map(([name, view]) => [name, parseConditionalView(view)]),
) as Record<keyof typeof FIXTURES, ConditionalView>

const [SONY, ONEPLUS] = VIEWS.B.branches as [
  ConditionalView["branches"][number],
  ConditionalView["branches"][number],
]
const PIXEL = {
  index: 2,
  variant_id: "GOO-P5-128-BLK",
  title: "Google Pixel 5 128 GB negro",
  status: "eligible",
  total_minor: 182_400_000,
  limit_minor: 190_000_000,
  items_total_minor: 179_900_000,
  unit_limit_minor: null,
} as const

const derived = (view: object) => parseConditionalView(view)
const cop = (minor: number | null) => plain(formatMoney(minor ?? Number.NaN, "COP"))

const meta = {
  title: "Agent/ConditionalBranches",
  component: ConditionalBranches,
  tags: ["autodocs"],
  args: { view: VIEWS.A },
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ConditionalBranches>

export default meta
type Story = StoryObj<typeof meta>

type Canvas = Parameters<NonNullable<Story["play"]>>[0]["canvas"]

/** Las filas de la lista, en el orden del comprador. */
function rows(canvas: Canvas) {
  const list = canvas.getByRole("list", { name: "Tus opciones, en orden" })
  return within(list).getAllByRole("listitem")
}

/** Cada fila con su título y su estado, en orden; la elegida lo dice en texto, no solo en color. */
async function expectRows(canvas: Canvas, expected: [title: string, status: string][]) {
  const items = rows(canvas)
  await expect(items).toHaveLength(expected.length)
  for (const [index, [title, status]] of expected.entries()) {
    await expect(items[index]).toHaveTextContent(title)
    await expect(items[index]).toHaveTextContent(status)
  }
}

/** Rama A: la primera opción cumple con el total del comercio y la segunda no se revisa. */
export const BranchA: Story = {
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("heading", { name: "Tus opciones, en orden" })).toBeVisible()
    await expectRows(canvas, [
      [SONY.title, "Elegida"],
      [ONEPLUS.title, "Sin revisar"],
    ])
    const [first, second] = rows(canvas)
    await expect(first).toHaveTextContent(`Total con envío ${cop(SONY.total_minor)}`)
    await expect(first).toHaveTextContent(`Tu límite ${cop(VIEWS.A.branches[0]!.limit_minor)}`)
    await expect(second).toHaveTextContent("Ya elegí una opción anterior.")
    // De B no se leyó la oferta: no hay total que mostrar.
    await expect(second).not.toHaveTextContent("Total con envío")
    await expect(first).toHaveAttribute("data-chosen", "true")
    await expect(second).not.toHaveAttribute("data-chosen")
  },
}

/** Rama B: A supera su límite por el total con envío y se elige B. */
export const BranchB: Story = {
  args: { view: VIEWS.B },
  play: async ({ canvas }) => {
    await expectRows(canvas, [
      [SONY.title, "Supera tu límite"],
      [ONEPLUS.title, "Elegida"],
    ])
    const [first, second] = rows(canvas)
    await expect(first).toHaveTextContent(
      `Total con envío ${cop(SONY.total_minor)} · Tu límite ${cop(SONY.limit_minor)}`,
    )
    await expect(second).toHaveTextContent(
      `Total con envío ${cop(ONEPLUS.total_minor)} · Tu límite ${cop(ONEPLUS.limit_minor)}`,
    )
    await expect(second).toHaveAttribute("data-chosen", "true")
  },
}

/** Rama C: A supera su límite, B está agotada y se elige la tercera. */
export const BranchC: Story = {
  args: {
    view: derived({
      ...VIEWS.B,
      chosen_index: 2,
      branches: [SONY, { ...ONEPLUS, status: "out_of_stock" }, PIXEL],
    }),
  },
  play: async ({ canvas }) => {
    await expectRows(canvas, [
      [SONY.title, "Supera tu límite"],
      [ONEPLUS.title, "Sin stock"],
      [PIXEL.title, "Elegida"],
    ])
    await expect(rows(canvas)[2]).toHaveTextContent(`Total con envío ${cop(PIXEL.total_minor)}`)
  },
}

/** `none`: ninguna cumple; cada fila dice su total y su límite, y ninguna se elige. */
export const None: Story = {
  args: { view: VIEWS.none },
  play: async ({ canvas, canvasElement }) => {
    await expectRows(canvas, [
      [SONY.title, "Supera tu límite"],
      [ONEPLUS.title, "Supera tu límite"],
    ])
    await expect(canvas.queryByText("Elegida")).toBeNull()
    await expect(canvasElement.querySelector("[data-chosen]")).toBeNull()
    await expect(rows(canvas)[1]).toHaveTextContent(`Tu límite ${cop(VIEWS.none.branches[1]!.limit_minor)}`)
  },
}

/** `undetermined`: A no se pudo verificar y por eso B no se revisó. Ninguna se elige. */
export const Undetermined: Story = {
  args: { view: VIEWS.undetermined },
  play: async ({ canvas }) => {
    await expectRows(canvas, [
      [SONY.title, "Sin verificar"],
      [ONEPLUS.title, "Sin revisar"],
    ])
    const [first, second] = rows(canvas)
    await expect(first).toHaveTextContent("No pude verificarla con el comercio.")
    await expect(second).toHaveTextContent(`No la revisé sin saber si ${SONY.title} cumplía.`)
    await expect(canvas.queryByText(/Total con envío/)).toBeNull()
  },
}

/**
 * `over_limit` por precio unitario: el total con envío cabe en el límite, pero el precio sin envío
 * pasa el límite por unidad del pedido. La fila dice ese precio y ese límite.
 */
export const UnitPriceOverLimit: Story = {
  args: {
    view: derived({
      ...VIEWS.B,
      branches: [
        { ...SONY, limit_minor: 300_000_000, unit_limit_minor: 289_899_999 },
        { ...ONEPLUS, unit_limit_minor: 289_899_999 },
      ],
    }),
  },
  play: async ({ canvas }) => {
    await expectRows(canvas, [
      [SONY.title, "Supera tu límite"],
      [ONEPLUS.title, "Elegida"],
    ])
    await expect(rows(canvas)[0]).toHaveTextContent(
      `Precio sin envío ${cop(SONY.items_total_minor)} · Tu límite por unidad ${cop(289_899_999)}`,
    )
    await expect(rows(canvas)[0]).not.toHaveTextContent("Total con envío")
  },
}

/** «Si A se agota, compra B»: A está agotada y se compra B. */
export const FallbackBought: Story = {
  args: {
    view: derived({ ...VIEWS.B, branches: [{ ...SONY, status: "out_of_stock" }, ONEPLUS] }),
  },
  play: async ({ canvas }) => {
    await expectRows(canvas, [
      [SONY.title, "Sin stock"],
      [ONEPLUS.title, "Elegida"],
    ])
  },
}

/** «Si A se agota, compra B», pero A no se agotó (superó su límite): B no aplicaba. */
export const FallbackNotTriggered: Story = {
  args: {
    view: derived({
      ...VIEWS.none,
      branches: [
        VIEWS.none.branches[0],
        { ...ONEPLUS, status: "not_triggered", total_minor: null, items_total_minor: null },
      ],
    }),
  },
  play: async ({ canvas }) => {
    await expectRows(canvas, [
      [SONY.title, "Supera tu límite"],
      [ONEPLUS.title, "No aplicaba"],
    ])
    await expect(rows(canvas)[1]).toHaveTextContent(`Solo si ${SONY.title} se agotaba.`)
  },
}

/**
 * `offer_changed`: el checkout de B no tuvo el total de su oferta. La fila elegida lo dice con los
 * dos totales; el texto del turno explica que se canceló.
 */
export const OfferChanged: Story = {
  args: { view: VIEWS.offer_changed },
  play: async ({ canvas }) => {
    await expectRows(canvas, [
      [SONY.title, "Supera tu límite"],
      [ONEPLUS.title, "El total cambió"],
    ])
    await expect(rows(canvas)[1]).toHaveTextContent(
      `Verificado ${cop(ONEPLUS.total_minor)} · En el checkout ${cop(VIEWS.offer_changed.checkout_total_minor)}`,
    )
    await expect(canvas.queryByText("Elegida")).toBeNull()
  },
}

/** Dos turnos condicionales en el mismo hilo: listas con nombre, sin regiones repetidas. */
export const TwoTurns: Story = {
  render: (args) => (
    <div className="space-y-3">
      <ConditionalBranches {...args} />
      <ConditionalBranches view={VIEWS.B} />
    </div>
  ),
  play: async ({ canvas }) => {
    await expect(canvas.getAllByRole("list", { name: "Tus opciones, en orden" })).toHaveLength(2)
    await expect(canvas.queryAllByRole("region")).toHaveLength(0)
  },
}

/** A 320 px las tres filas, con sus montos y etiquetas, caben sin desbordar. */
export const Phone320: Story = {
  args: {
    view: derived({
      ...VIEWS.offer_changed,
      chosen_index: 2,
      checkout_total_minor: 183_400_000,
      branches: [SONY, { ...ONEPLUS, status: "over_limit", limit_minor: 202_399_999 }, PIXEL],
    }),
  },
  render: (args) => (
    <div className={PHONE_COLUMN} data-testid="phone-column">
      <ConditionalBranches {...args} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectFitsPhoneColumn(canvasElement)
  },
}
