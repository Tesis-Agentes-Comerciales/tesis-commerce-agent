import { expect } from "storybook/test"

import type { DelegationView, EscalationView } from "@/lib/delegation-contract"

// Ayudantes de las historias de delegación: sin red ni temporizadores propios.

/** Como el normalizador de Testing Library: los espacios duros de `Intl` cuentan como espacios. */
export const plain = (text: string) => text.replace(/\s+/g, " ")

/** Un teléfono de 320 px deja 288 px al hilo del chat. */
export const PHONE_COLUMN = "w-[288px]"

/**
 * Las tarjetas de delegación no van en el hilo sino en el diálogo «Delegaciones»: en ese teléfono
 * mide 288 px (`calc(100% - 2rem)`) y, con su borde y su `p-4`, les deja 254.
 */
export const PHONE_DIALOG_COLUMN = "w-[254px]"

/** Un nodo legible en el mensaje de un fallo: etiqueta, test id y el inicio de su texto. */
const describe = (element: HTMLElement) =>
  `<${element.tagName.toLowerCase()}${element.dataset.testid ? ` ${element.dataset.testid}` : ""}> ${element.textContent?.slice(0, 40) ?? ""}`

/**
 * Nada de la columna se sale por la derecha: ni scroll horizontal, ni texto cortado, ni contenido
 * que se mete en el padding de su contenedor. Un `overflow-hidden` (la superficie de las tarjetas,
 * `truncate`) esconde lo que se sale sin mover ningún borde, así que también se mira por dentro.
 */
export async function expectFitsPhoneColumn(canvasElement: HTMLElement) {
  const column = canvasElement.querySelector<HTMLElement>("[data-testid='phone-column']")
  await expect(column).not.toBeNull()
  const right = column?.getBoundingClientRect().right ?? 0
  await expect(column?.scrollWidth).toBeLessThanOrEqual(column?.clientWidth ?? 0)
  for (const element of column?.querySelectorAll<HTMLElement>("*") ?? []) {
    if (element.closest(".sr-only")) continue
    const box = element.getBoundingClientRect()
    await expect(box.right, describe(element)).toBeLessThanOrEqual(right + 1)
    const style = getComputedStyle(element)
    if (style.overflowX !== "visible") {
      await expect(element.scrollWidth, describe(element)).toBeLessThanOrEqual(element.clientWidth)
    }
    const parent = element.parentElement
    if (!parent || style.position === "absolute" || style.position === "fixed") continue
    const parentBox = parent.getBoundingClientRect()
    const contentRight = parentBox.right - parseFloat(getComputedStyle(parent).paddingRight)
    await expect(box.right, describe(element)).toBeLessThanOrEqual(contentRight + 1)
  }
}

/** Foco que llegó con el teclado: `:focus-visible` y el anillo con la tinta del chat. */
export async function expectKeyboardFocus(element: HTMLElement) {
  await expect(element).toHaveFocus()
  await expect(element.matches(":focus-visible")).toBe(true)
  await expect(getComputedStyle(element).boxShadow).toContain("rgb(13, 13, 13)")
}

/** El contrato garantiza el escalamiento en una tarea escalada; aquí se exige. */
export function escalationOf(delegation: DelegationView): EscalationView {
  if (!delegation.escalation) throw new Error(`${delegation.grant_id} has no escalation`)
  return delegation.escalation
}
