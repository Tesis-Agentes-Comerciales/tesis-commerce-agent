import { describe, expect, it } from "vitest";

import {
  parseConditionalView,
  type ConditionalView,
} from "@/lib/agent-contract";
import views from "@/storybook/fixtures/conditional-views.json";

// `SelectProductResult.conditional` (docs/design/p0-conditional-intent.md §3.10). El backend valida
// las mismas fixtures con `ConditionalView` y comprueba que son lo que produce el evaluador para las
// celdas UX03 (tests/test_conditional_contract.py); estos casos de rechazo son los de `REJECTED`.

type Item = Record<string, unknown>;
const FIXTURES = views as Record<string, unknown>;

const at = (item: Item, ...keys: (string | number)[]) =>
  keys.reduce<unknown>((value, key) => (value as Item)[key], item) as Item;

function failureOf(name: string, mutate: (item: Item) => void) {
  const item = structuredClone(FIXTURES[name]) as Item;
  mutate(item);
  try {
    parseConditionalView(item);
    return null;
  } catch (error) {
    return (error as Error).message;
  }
}

describe("ConditionalView", () => {
  it("valida una fixture por resultado", () => {
    expect(Object.keys(FIXTURES).sort()).toEqual([
      "A",
      "B",
      "none",
      "offer_changed",
      "undetermined",
    ]);
    for (const [name, value] of Object.entries(FIXTURES)) {
      expect(() => parseConditionalView(value), name).not.toThrow();
    }
    const chosen = (name: string) =>
      (FIXTURES[name] as ConditionalView).chosen_index;
    expect(
      ["A", "B", "none", "undetermined", "offer_changed"].map(chosen),
    ).toEqual([0, 1, null, null, 1]);
  });

  const rejected: [
    id: string,
    fixture: string,
    mutate: (item: Item) => void,
    path: string,
  ][] = [
    [
      "extra-key",
      "A",
      (item) => (item.rationale = "x"),
      "conditional.rationale",
    ],
    [
      "float-total",
      "A",
      (item) => (at(item, "branches", 0).total_minor = 292_400_000.5),
      "conditional.branches[0].total_minor",
    ],
    [
      "unknown-status",
      "A",
      (item) => (at(item, "branches", 1).status = "skipped"),
      "conditional.branches[1].status",
    ],
    [
      "items-without-total",
      "B",
      (item) => (at(item, "branches", 0).total_minor = null),
      "conditional.branches[0].items_total_minor",
    ],
    [
      "items-over-total",
      "B",
      (item) => (at(item, "branches", 0).items_total_minor = 292_400_001),
      "conditional.branches[0].items_total_minor",
    ],
    [
      "branch-without-choice",
      "A",
      (item) => (item.chosen_index = null),
      "conditional.chosen_index",
    ],
    [
      "choice-not-eligible",
      "B",
      (item) => (item.chosen_index = 0),
      "conditional.chosen_index",
    ],
    [
      "none-with-choice",
      "none",
      (item) => (item.chosen_index = 0),
      "conditional.chosen_index",
    ],
    [
      "checkout-total-outside-offer-changed",
      "A",
      (item) => (item.checkout_total_minor = 1),
      "conditional.checkout_total_minor",
    ],
    [
      "offer-changed-without-checkout-total",
      "offer_changed",
      (item) => (item.checkout_total_minor = null),
      "conditional.checkout_total_minor",
    ],
    [
      "branches-out-of-order",
      "A",
      (item) => (at(item, "branches", 0).index = 1),
      "conditional.branches[0].index",
    ],
    [
      "no-branches",
      "none",
      (item) => (item.branches = []),
      "conditional.branches",
    ],
    [
      "other-currency",
      "A",
      (item) => (item.currency = "USD"),
      "conditional.currency",
    ],
  ];

  it.each(rejected)(
    "rechaza %s en el campo que cambió",
    (_, fixture, mutate, path) => {
      expect(failureOf(fixture, mutate)).toBe(
        `Invalid conditional view at ${path}`,
      );
    },
  );
});
