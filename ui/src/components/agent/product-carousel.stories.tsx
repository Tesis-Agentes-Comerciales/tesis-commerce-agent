import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, waitFor } from "storybook/test"

import { ProductCarousel } from "@/components/agent/product-carousel"
import {
  SEARCH_RESULT,
  SEARCH_RESULT_EMPTY,
  SEARCH_RESULT_MANY,
  SEARCH_RESULT_SINGLE,
  SONORA,
  SONORA_LONG,
} from "@/storybook/agent-fixtures"

const meta = {
  title: "Agent/ProductCarousel",
  component: ProductCarousel,
  tags: ["autodocs"],
  args: {
    products: SEARCH_RESULT.products,
    matches: SEARCH_RESULT.matches,
    rejections: SEARCH_RESULT.rejections,
  },
  decorators: [
    (Story) => (
      <div className="w-[min(44rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ProductCarousel>

export default meta
type Story = StoryObj<typeof meta>

export const Results: Story = {}

/** Después de `select_product` se resalta la variante elegida. */
export const WithSelection: Story = {
  args: { selectedVariantId: SONORA.variant_id },
}

/** Hay más coincidencias que las seis tarjetas que envía el backend. */
export const ManyMatches: Story = {
  args: {
    products: SEARCH_RESULT_MANY.products,
    matches: SEARCH_RESULT_MANY.matches,
    rejections: SEARCH_RESULT_MANY.rejections,
  },
  play: async ({ canvas, userEvent }) => {
    const region = canvas.getByRole("group", { name: "Resultados del catálogo" })
    // Solo recibe foco cuando hay desplazamiento; con seis tarjetas lo hay.
    await waitFor(() => expect(region).toHaveAttribute("tabindex", "0"))
    region.focus()
    await userEvent.keyboard("{ArrowRight}")
    await expect(region).toHaveFocus()
  },
}

export const SingleMatch: Story = {
  args: {
    products: SEARCH_RESULT_SINGLE.products,
    matches: SEARCH_RESULT_SINGLE.matches,
    rejections: SEARCH_RESULT_SINGLE.rejections,
  },
}

export const Empty: Story = {
  args: {
    products: SEARCH_RESULT_EMPTY.products,
    matches: SEARCH_RESULT_EMPTY.matches,
    rejections: SEARCH_RESULT_EMPTY.rejections,
  },
}

export const EmptyWithoutRejections: Story = {
  args: { products: [], matches: 0, rejections: {} },
}

export const Loading: Story = {
  args: { products: [], matches: 0, rejections: {}, loading: true },
}

export const LongText: Story = {
  args: {
    products: [SONORA_LONG, ...SEARCH_RESULT.products.slice(1)],
    rejections: { budget: 12, unavailable: 7, category: 4, option: 3, term: 2, legacy_reason: 1 },
  },
}

export const Mobile: Story = {
  args: {
    products: SEARCH_RESULT_MANY.products,
    matches: SEARCH_RESULT_MANY.matches,
    rejections: SEARCH_RESULT_MANY.rejections,
    selectedVariantId: SONORA.variant_id,
  },
  globals: { viewport: { value: "mobile", isRotated: false } },
}
