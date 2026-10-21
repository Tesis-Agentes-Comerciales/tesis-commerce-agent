import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { formatMoneyParts } from "@/components/agent/agent-format"
import { ProductCard, ProductCardSkeleton } from "@/components/agent/product-card"
import { expectFitsPhoneColumn, PHONE_COLUMN } from "@/components/delegation/story-helpers"
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

/** La recomendación de `select_product`: precio de catálogo, opciones y por qué se eligió. */
export const Hero: Story = {
  play: async ({ args, canvas }) => {
    const { product } = args
    await expect(canvas.getByRole("heading", { name: product.title })).toBeVisible()
    await expect(canvas.getByText("Disponible")).toBeVisible()
    const price = formatMoneyParts(product.observed_unit_amount_minor, product.currency)
    await expect(canvas.getByText(price.amount.replace(/\s+/g, " "))).toBeVisible()
    // El catálogo no es el total: ese solo sale de los términos de aprobación.
    await expect(canvas.getByText(/Precio de catálogo/)).toBeVisible()
    for (const option of product.options) await expect(canvas.getByText(option.value)).toBeVisible()
    await expect(canvas.getByText("Por qué lo elegí")).toBeVisible()
    const link = canvas.getByRole("link", { name: /Ver en la tienda/ })
    await expect(link).toHaveAttribute("target", "_blank")
    await expect(link).toHaveAttribute("rel", "noopener noreferrer")
  },
}

export const HeroWithoutRationale: Story = {
  args: { rationale: null },
  play: async ({ canvas }) => {
    await expect(canvas.queryByText("Por qué lo elegí")).toBeNull()
  },
}

export const Compact: Story = {
  args: { variant: "compact" },
}

export const CompactSelected: Story = {
  args: { variant: "compact", selected: true },
  play: async ({ args, canvas }) => {
    await expect(canvas.getByText("Elegido")).toBeVisible()
    // En la variante compacta el título es el enlace a la tienda.
    await expect(canvas.getByRole("link", { name: new RegExp(args.product.title) })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    )
  },
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
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Agotado")).toBeVisible()
    await expect(canvas.queryByText("Disponible")).toBeNull()
  },
}

export const CompactMissingImage: Story = {
  args: { variant: "compact", product: ESTUDIO },
}

/** Una descripción larga se recorta y se expande con un botón que anuncia su estado. */
export const LongText: Story = {
  args: {
    product: SONORA_LONG,
    rationale: `${SELECT_RATIONALE} También tiene la mejor relación entre batería y peso de las opciones que quedaron después de aplicar tus condiciones.`,
  },
  play: async ({ canvas, userEvent }) => {
    const toggle = canvas.getByRole("button", { name: "Mostrar más" })
    await expect(toggle).toHaveAttribute("aria-expanded", "false")
    await userEvent.click(toggle)
    await expect(canvas.getByRole("button", { name: "Mostrar menos" })).toHaveAttribute("aria-expanded", "true")
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

/**
 * Teléfono de 320 px: el mensaje deja 272 px a la tarjeta. Con un precio de ocho cifras en pesos,
 * el código de la moneda baja bajo el precio en vez de salirse del padding con una fuente ancha.
 */
export const Phone320: Story = {
  args: { product: { ...SONORA, observed_unit_amount_minor: 1_249_990_000 } },
  render: (args) => (
    <div className={`${PHONE_COLUMN} px-2`} data-testid="phone-column">
      <ProductCard {...args} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectFitsPhoneColumn(canvasElement)
  },
}

export const MobileLongText: Story = {
  args: { product: SONORA_LONG },
  globals: { viewport: { value: "mobile", isRotated: false } },
}
