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

/**
 * Clic de mouse: el botón recibe el foco sin `:focus-visible`, como en un navegador. `userEvent.click`
 * enfoca por script, y Chromium trata ese foco como de teclado.
 */
export function mouseClick(element: HTMLElement) {
  element.focus({ focusVisible: false })
  element.click()
}

/** Las animaciones que siguen corriendo dentro del elemento: con la página quieta, ninguna. */
export function runningAnimations(element: HTMLElement) {
  return element
    .getAnimations({ subtree: true })
    .filter((animation) => animation.playState === "running")
}
