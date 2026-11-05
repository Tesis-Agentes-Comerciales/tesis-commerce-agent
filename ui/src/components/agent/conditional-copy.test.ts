import { describe, expect, it } from "vitest"

import { formatMoney } from "@/components/agent/agent-format"
import { branchDetail, branchStatus } from "@/components/agent/conditional-copy"
import {
  parseConditionalView,
  type ConditionalBranchView,
  type ConditionalView,
} from "@/lib/agent-contract"
import FIXTURES from "@/storybook/fixtures/conditional-views.json"

// Lo que dice cada rama de `ConditionalView` (p0-conditional-intent.md §3.8 y §3.10), con las
// fixtures del contrato que también valida el backend.

const VIEWS = Object.fromEntries(
  Object.entries(FIXTURES).map(([name, view]) => [name, parseConditionalView(view)]),
) as Record<keyof typeof FIXTURES, ConditionalView>

const cop = (minor: number | null) => formatMoney(minor ?? Number.NaN, "COP")
const [SONY, ONEPLUS] = [VIEWS.B.branches[0]!, VIEWS.B.branches[1]!]

/** Una vista derivada de la fixture, validada con el mismo contrato. */
function viewWith(base: ConditionalView, change: Partial<ConditionalView>): ConditionalView {
  return parseConditionalView({ ...structuredClone(base), ...change })
}

const lines = (view: ConditionalView) =>
  view.branches.map((branch) => [branchStatus(view, branch), branchDetail(view, branch)])

describe("las ramas de una intención condicional", () => {
  it("A: la primera cumple y la segunda no hizo falta revisarla", () => {
    expect(lines(VIEWS.A)).toEqual([
      [
        { label: "Elegida", tone: "success" },
        `Total con envío ${cop(SONY.total_minor)} · Tu límite ${cop(292_400_000)}`,
      ],
      [{ label: "Sin revisar", tone: "neutral" }, "Ya elegí una opción anterior."],
    ])
  })

  it("B: A supera su límite por el total con envío y se elige B", () => {
    expect(lines(VIEWS.B)).toEqual([
      [
        { label: "Supera tu límite", tone: "neutral" },
        `Total con envío ${cop(SONY.total_minor)} · Tu límite ${cop(SONY.limit_minor)}`,
      ],
      [
        { label: "Elegida", tone: "success" },
        `Total con envío ${cop(ONEPLUS.total_minor)} · Tu límite ${cop(ONEPLUS.limit_minor)}`,
      ],
    ])
  })

  it("none: las dos superan su límite y ninguna se elige", () => {
    expect(lines(VIEWS.none).map(([status]) => status)).toEqual([
      { label: "Supera tu límite", tone: "neutral" },
      { label: "Supera tu límite", tone: "neutral" },
    ])
  })

  it("undetermined: A no se pudo verificar y B no se revisó sin saber si A cumplía", () => {
    expect(lines(VIEWS.undetermined)).toEqual([
      [{ label: "Sin verificar", tone: "warning" }, "No pude verificarla con el comercio."],
      [
        { label: "Sin revisar", tone: "neutral" },
        `No la revisé sin saber si ${SONY.title} cumplía.`,
      ],
    ])
  })

  it("offer_changed: la elegida dice el total verificado y el del checkout", () => {
    const [, chosen] = lines(VIEWS.offer_changed)
    expect(chosen).toEqual([
      { label: "El total cambió", tone: "warning" },
      `Verificado ${cop(ONEPLUS.total_minor)} · En el checkout ${cop(203_400_000)}`,
    ])
  })

  it("over_limit dentro del límite de total es por el precio sin envío", () => {
    const unit = 289_899_999
    const view = viewWith(VIEWS.B, {
      branches: [
        { ...SONY, limit_minor: 300_000_000, unit_limit_minor: unit },
        { ...ONEPLUS, unit_limit_minor: unit },
      ],
    })
    expect(branchDetail(view, view.branches[0]!)).toBe(
      `Precio sin envío ${cop(SONY.items_total_minor)} · Tu límite por unidad ${cop(unit)}`,
    )
    // Sin límite de total, el único que pudo fallar es el de unidad.
    const noTotal = viewWith(view, {
      branches: [{ ...view.branches[0]!, limit_minor: null }, view.branches[1]!],
    })
    expect(branchDetail(noTotal, noTotal.branches[0]!)).toMatch(/^Precio sin envío/)
  })

  it("si A se agota compra B: A sin stock y B elegida", () => {
    const view = viewWith(VIEWS.B, {
      branches: [{ ...SONY, status: "out_of_stock" }, ONEPLUS],
    })
    expect(lines(view)[0]).toEqual([{ label: "Sin stock", tone: "neutral" }, null])
  })

  it("si A no se agotó, B no aplicaba", () => {
    const view = viewWith(VIEWS.none, {
      branches: [
        VIEWS.none.branches[0]!,
        { ...ONEPLUS, status: "not_triggered", total_minor: null, items_total_minor: null },
      ],
    })
    expect(lines(view)[1]).toEqual([
      { label: "No aplicaba", tone: "neutral" },
      `Solo si ${SONY.title} se agotaba.`,
    ])
  })

  it.each([
    ["inactive", "No está a la venta"],
    ["intent_mismatch", "No cumple tu pedido"],
  ] satisfies [ConditionalBranchView["status"], string][])("%s: %s", (status, label) => {
    const view = viewWith(VIEWS.none, {
      branches: [
        { ...SONY, status, total_minor: null, items_total_minor: null },
        VIEWS.none.branches[1]!,
      ],
    })
    expect(lines(view)[0]).toEqual([{ label, tone: "neutral" }, null])
  })

  it("una rama elegible sin límite de total solo dice su total", () => {
    const view = viewWith(VIEWS.A, {
      branches: [{ ...VIEWS.A.branches[0]!, limit_minor: null }, VIEWS.A.branches[1]!],
    })
    expect(branchDetail(view, view.branches[0]!)).toBe(`Total con envío ${cop(SONY.total_minor)}`)
  })
})
