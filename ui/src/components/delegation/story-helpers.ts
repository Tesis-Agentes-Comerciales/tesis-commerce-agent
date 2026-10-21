import { expect } from "storybook/test"

import type { DelegationView, EscalationView } from "@/lib/delegation-contract"

// Ayudantes de las historias de delegación: sin red ni temporizadores propios.

/** Como el normalizador de Testing Library: los espacios duros de `Intl` cuentan como espacios. */
export const plain = (text: string) => text.replace(/\s+/g, " ")

/** Un teléfono de 320 px deja 288 px al hilo del chat. */
export const PHONE_COLUMN = "w-[288px]"

/** Nada de la columna se sale por la derecha: ni scroll horizontal ni texto cortado. */
export async function expectFitsPhoneColumn(canvasElement: HTMLElement) {
  const column = canvasElement.querySelector<HTMLElement>("[data-testid='phone-column']")
  await expect(column).not.toBeNull()
  const right = column?.getBoundingClientRect().right ?? 0
  await expect(column?.scrollWidth).toBeLessThanOrEqual(column?.clientWidth ?? 0)
  for (const element of column?.querySelectorAll<HTMLElement>("*") ?? []) {
    if (element.classList.contains("sr-only")) continue
    await expect(element.getBoundingClientRect().right).toBeLessThanOrEqual(right + 1)
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
