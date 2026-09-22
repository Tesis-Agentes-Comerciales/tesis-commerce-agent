import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { ProductCard, ProductCardSkeleton } from "@/components/agent/product-card"
import {
  BRISA,
  ESTUDIO,
  PULSO,
  SELECT_RATIONALE,
  SONORA,
  SONORA_INSECURE_IMAGE,
  SONORA_LONG,
} from "@/storybook/agent-fixtures"

const meta = {
  title: "Agent/ProductCard",
  component: ProductCard,
  tags: ["autodocs"],
  args: {
    product: SONORA,
    variant: "hero",
    rationale: SELECT_RATIONALE,
  },
  decorators: [
    (Story, { args }) => (
      <div className={args.variant === "compact" ? "w-56" : "w-[min(42rem,calc(100vw-2rem))]"}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ProductCard>

export default meta
type Story = StoryObj<typeof meta>

export const Hero: Story = {
  play: async ({ canvas }) => {
    const link = canvas.getByRole("link", { name: /Ver en la tienda/ })
    await expect(link).toHaveAttribute("target", "_blank")
    await expect(link).toHaveAttribute("rel", "noopener noreferrer")
    await expect(canvas.getByText(/Precio de catálogo/)).toBeVisible()
  },
}

export const HeroWithoutRationale: Story = {
  args: { rationale: null },
}

export const Compact: Story = {
  args: { variant: "compact" },
}

export const CompactSelected: Story = {
  args: { variant: "compact", selected: true },
}

/** `image: null` en el contrato: el placeholder toma un tono estable por producto. */
export const MissingImage: Story = {
  args: { product: ESTUDIO, rationale: null },
}

/** La URL no carga: se reemplaza por el placeholder en vez de un ícono roto. */
export const BrokenImage: Story = {
  args: { product: PULSO },
  play: async ({ canvas }) => {
    await expect(await canvas.findByTestId("product-image-placeholder")).toBeVisible()
  },
}

/** Una URL http no se pide: el contrato solo admite imágenes https. */
export const InsecureImageRejected: Story = {
  args: { product: SONORA_INSECURE_IMAGE },
  play: async ({ canvas }) => {
    await expect(canvas.queryByRole("img")).toBeNull()
  },
}

export const Unavailable: Story = {
  args: { product: BRISA, rationale: null },
}

export const CompactMissingImage: Story = {
  args: { variant: "compact", product: ESTUDIO },
}

export const LongText: Story = {
  args: {
    product: SONORA_LONG,
    rationale: `${SELECT_RATIONALE} También tiene la mejor relación entre batería y peso de las opciones que quedaron después de aplicar tus condiciones.`,
  },
}

export const CompactLongText: Story = {
  args: { variant: "compact", product: SONORA_LONG },
}

/** Sin `product_url` válido no se muestra el enlace. */
export const WithoutLink: Story = {
  args: { product: { ...SONORA, product_url: "javascript:alert(1)" } },
  play: async ({ canvas }) => {
    await expect(canvas.queryByRole("link")).toBeNull()
  },
}

export const Loading: Story = {
  render: (args) => <ProductCardSkeleton variant={args.variant} />,
}

export const CompactLoading: Story = {
  args: { variant: "compact" },
  render: (args) => <ProductCardSkeleton variant={args.variant} />,
}

export const Mobile: Story = {
  globals: { viewport: { value: "mobile", isRotated: false } },
}

export const MobileLongText: Story = {
  args: { product: SONORA_LONG },
  globals: { viewport: { value: "mobile", isRotated: false } },
}
