import type { StoryContext } from "@storybook/react-vite"

/**
 * Pega y envía el mensaje; `type` espera un timer por tecla, lento en pestañas en segundo plano.
 * Antes espera a que el turno anterior termine: mientras el agente responde, el composer muestra
 * "Detener respuesta" y la tecla Enter inserta un salto de línea en vez de enviar.
 */
export async function sendComposerMessage(
  { canvas, userEvent }: Pick<StoryContext, "canvas" | "userEvent">,
  text: string,
) {
  await canvas.findByRole("button", { name: "Enviar mensaje" }, { timeout: 30000 })
  const input = await canvas.findByRole("textbox", { name: "Mensaje de compra" })
  await userEvent.click(input)
  await userEvent.paste(text)
  await userEvent.keyboard("{Enter}")
}
