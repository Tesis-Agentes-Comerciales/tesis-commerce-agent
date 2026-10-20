import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, mocked } from "storybook/test"

import { SessionGate } from "@/components/session-gate"
import { authClient } from "@/lib/auth-client"
import { BUYER_SESSION } from "@/storybook/fixtures"

// `authClient` es el doble de src/lib/__mocks__/auth-client.ts: el parámetro `session` decide qué
// devuelve `useSession` y el formulario nunca llama al servicio de acceso real.

const meta = {
  title: "Auth/SessionGate",
  component: SessionGate,
  args: {
    children: (session) => (
      <main className="p-8 text-sm">
        <h1 className="text-xl font-semibold">Contenido protegido</h1>
        <p>Sesión de {session.user.email}</p>
      </main>
    ),
  },
  argTypes: { children: { control: false } },
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof SessionGate>

export default meta
type Story = StoryObj<typeof meta>

const PROTECTED = /Contenido protegido/
const CREDENTIALS = { email: "ana@example.test", password: "contraseña-de-prueba" }

/** Better Auth aún no responde: ni formulario ni contenido. */
export const SessionPending: Story = {
  parameters: { session: "pending" },
  play: async ({ canvas, canvasElement }) => {
    await expect(canvas.getByText("Cargando sesión…")).toBeVisible()
    await expect(canvas.queryByRole("button")).toBeNull()
    await expect(canvasElement).not.toHaveTextContent(PROTECTED)
  },
}

/** Sin sesión: el servicio rechaza las credenciales y el formulario lo dice sin detalles. */
export const SignedOut: Story = {
  parameters: { session: "anonymous" },
  play: async ({ canvas, canvasElement, userEvent }) => {
    await expect(canvasElement).not.toHaveTextContent(PROTECTED)
    await userEvent.type(canvas.getByLabelText("Correo"), CREDENTIALS.email)
    await userEvent.type(canvas.getByLabelText("Contraseña"), CREDENTIALS.password)
    await userEvent.click(canvas.getByRole("button", { name: "Entrar" }))
    await expect(authClient.signIn.email).toHaveBeenCalledWith(CREDENTIALS)
    await expect(await canvas.findByRole("alert")).toHaveTextContent("No pude iniciar sesión")
    await expect(canvasElement).not.toHaveTextContent(PROTECTED)
  },
}

/** Crear cuenta pide el nombre y usa `signUp`, no `signIn`. */
export const SignUp: Story = {
  parameters: { session: "anonymous" },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Crear cuenta" }))
    await userEvent.type(canvas.getByLabelText("Nombre"), "Ana Demo")
    await userEvent.type(canvas.getByLabelText("Correo"), CREDENTIALS.email)
    await userEvent.type(canvas.getByLabelText("Contraseña"), CREDENTIALS.password)
    await userEvent.click(canvas.getByRole("button", { name: "Crear cuenta" }))
    await expect(authClient.signUp.email).toHaveBeenCalledWith({ name: "Ana Demo", ...CREDENTIALS })
    await expect(authClient.signIn.email).not.toHaveBeenCalled()
    await expect(canvas.getByRole("button", { name: "Ya tengo cuenta" })).toBeVisible()
  },
}

/** El servicio de acceso no responde: otro mensaje, y el botón vuelve a quedar disponible. */
export const ServiceUnavailable: Story = {
  parameters: { session: "anonymous" },
  beforeEach: () => {
    mocked(authClient.signIn.email).mockRejectedValue(new TypeError("Failed to fetch"))
  },
  play: async ({ canvas, userEvent }) => {
    await userEvent.type(canvas.getByLabelText("Correo"), CREDENTIALS.email)
    await userEvent.type(canvas.getByLabelText("Contraseña"), CREDENTIALS.password)
    await userEvent.click(canvas.getByRole("button", { name: "Entrar" }))
    await expect(await canvas.findByRole("alert")).toHaveTextContent("No pude conectar con el servicio")
    await expect(canvas.getByRole("button", { name: "Entrar" })).toBeEnabled()
  },
}

/** Con sesión el contenido recibe la sesión de Better Auth. */
export const SignedIn: Story = {
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("heading", { name: "Contenido protegido" })).toBeVisible()
    await expect(canvas.getByText(`Sesión de ${BUYER_SESSION.user.email}`)).toBeVisible()
    await expect(canvas.queryByRole("textbox")).toBeNull()
  },
}
