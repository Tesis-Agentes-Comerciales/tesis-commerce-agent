import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, fn } from "storybook/test"

import { DelegationForm } from "@/components/delegation/delegation-form"
import {
  expectFitsPhoneColumn,
  expectKeyboardFocus,
  PHONE_COLUMN,
} from "@/components/delegation/story-helpers"
import { seenOffers } from "@/features/seen-offers"
import { DEMO_PROFILE } from "@/lib/commerce-types"
import type { DelegationDraft } from "@/lib/delegation-draft"
import { fixtureState } from "@/storybook/assistant-transport"

/** Las tres opciones que el chat mostró en el stream real de una compra (`purchase.sse`). */
const OFFERS = seenOffers(fixtureState("purchase").messages)
const BLANK: DelegationDraft = {
  variantIds: [],
  total: "",
  validForSeconds: 3_600,
  addressLine: DEMO_PROFILE.addressLine,
  city: DEMO_PROFILE.city,
  state: DEMO_PROFILE.state,
  preference: "",
}

const meta = {
  title: "Delegation/Form",
  component: DelegationForm,
  tags: ["autodocs"],
  args: { offers: OFFERS, draft: BLANK, onSubmit: fn(), onCancel: fn() },
  decorators: [
    (Story) => (
      <div className="w-[min(30rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof DelegationForm>

export default meta
type Story = StoryObj<typeof meta>

/**
 * Las opciones vistas en el chat, el tope, la vigencia, la entrega del perfil y una indicación.
 * Enviar devuelve lo escrito tal cual: la ciudad con tilde no se normaliza aquí.
 */
export const Filled: Story = {
  play: async ({ args, canvas, userEvent }) => {
    await expect(canvas.getByRole("heading", { name: "Delegar una compra" })).toBeVisible()
    const options = canvas.getAllByRole("checkbox")
    await expect(options).toHaveLength(OFFERS.length)
    await userEvent.click(canvas.getByRole("checkbox", { name: /Bose QuietComfort Ultra negro/ }))
    await userEvent.click(canvas.getByRole("checkbox", { name: /Sony WH-1000XM6 negro/ }))
    await userEvent.type(canvas.getByLabelText("Total máximo con envío (pesos)"), "2.000.000")
    await userEvent.selectOptions(canvas.getByLabelText("Vigencia"), "24 horas")
    const city = canvas.getByLabelText("Ciudad")
    await expect(city).toHaveValue(DEMO_PROFILE.city)
    await userEvent.clear(city)
    await userEvent.type(city, "Bogotá")
    await expect(canvas.getByLabelText("Departamento")).toHaveValue("DC")
    await expect(canvas.getByText("Por ahora el comercio solo entrega en Bogotá D.C.")).toBeVisible()
    await userEvent.type(canvas.getByLabelText("Tu indicación (opcional)"), "Prefiero los Sony.")
    await userEvent.click(canvas.getByRole("button", { name: "Preparar la autorización" }))
    await expect(args.onSubmit).toHaveBeenCalledWith({
      variantIds: ["BOS-QCU-BLK", "SON-XM6-BLK"],
      total: "2.000.000",
      validForSeconds: 86_400,
      addressLine: DEMO_PROFILE.addressLine,
      city: "Bogotá",
      state: "DC",
      preference: "Prefiero los Sony.",
    })
  },
}

/** Volver de la propuesta: el formulario trae lo último que se envió. */
export const KeepsTheDraft: Story = {
  args: {
    draft: {
      ...BLANK,
      variantIds: ["SON-XM6-BLK"],
      total: "2000000",
      validForSeconds: 21_600,
      preference: "Solo si llega esta semana.",
    },
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("checkbox", { name: /Sony WH-1000XM6 negro/ })).toBeChecked()
    await expect(canvas.getByRole("checkbox", { name: /Bose/ })).not.toBeChecked()
    await expect(canvas.getByLabelText("Total máximo con envío (pesos)")).toHaveValue("2000000")
    await expect(canvas.getByLabelText("Vigencia")).toHaveValue("21600")
    await expect(canvas.getByLabelText("Tu indicación (opcional)")).toHaveValue(
      "Solo si llega esta semana.",
    )
  },
}

/** Sin búsquedas en el chat no hay qué delegar: lo dice, y preparar no actúa. */
export const NoOffers: Story = {
  args: { offers: [] },
  play: async ({ args, canvas, userEvent }) => {
    await expect(canvas.getByText(/Primero busca en el chat/)).toBeVisible()
    await expect(canvas.queryAllByRole("checkbox")).toHaveLength(0)
    const submit = canvas.getByRole("button", { name: "Preparar la autorización" })
    await expect(submit).toHaveAttribute("aria-disabled", "true")
    submit.focus()
    await userEvent.keyboard("{Enter}")
    await expect(args.onSubmit).not.toHaveBeenCalled()
    await userEvent.click(canvas.getByRole("button", { name: "Cancelar" }))
    await expect(args.onCancel).toHaveBeenCalledOnce()
  },
}

/** Mientras `POST /delegations/proposals` responde, ningún botón actúa. */
export const Submitting: Story = {
  args: { submitting: true },
  play: async ({ args, canvas, canvasElement, userEvent }) => {
    await expect(canvasElement.querySelector("form")).toHaveAttribute("aria-busy", "true")
    for (const button of canvas.getAllByRole("button")) {
      await expect(button).toHaveAttribute("aria-disabled", "true")
      button.focus()
      await userEvent.keyboard("{Enter}")
    }
    await expect(args.onSubmit).not.toHaveBeenCalled()
    await expect(args.onCancel).not.toHaveBeenCalled()
  },
}

/** El comercio no entregaría ahí (422 `delegation_destination_unsupported`): el texto de la UI. */
export const DestinationUnsupported: Story = {
  args: {
    error:
      "Por ahora el comercio solo entrega en Bogotá D.C. (departamento DC, código postal 110111). Revisa la dirección de entrega.",
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("alert")).toHaveTextContent("solo entrega en Bogotá D.C.")
  },
}

/** Con el teclado: la primera opción, y el foco con el anillo de la tinta del chat. */
export const Keyboard: Story = {
  play: async ({ canvas, userEvent }) => {
    await userEvent.tab()
    const first = canvas.getAllByRole("checkbox")[0]
    await expect(first).toHaveFocus()
    await userEvent.keyboard(" ")
    await expect(first).toBeChecked()
    // Dos opciones más, cinco campos, la indicación y «Cancelar».
    for (let step = 0; step < 10; step += 1) await userEvent.tab()
    await expectKeyboardFocus(canvas.getByRole("button", { name: "Preparar la autorización" }))
  },
}

/** A 320 px las opciones, los campos y los botones caben sin desbordar ni recortarse. */
export const Phone320: Story = {
  args: { error: "Elige al menos una opción." },
  render: (args) => (
    <div className={PHONE_COLUMN} data-testid="phone-column">
      <DelegationForm {...args} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    await expectFitsPhoneColumn(canvasElement)
  },
}
