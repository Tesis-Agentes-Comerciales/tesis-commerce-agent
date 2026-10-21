import { useLayoutEffect, useRef, useState, useSyncExternalStore } from "react"

function subscribeToFocus(onChange: () => void) {
  document.addEventListener("focusin", onChange)
  document.addEventListener("focusout", onChange)
  return () => {
    document.removeEventListener("focusin", onChange)
    document.removeEventListener("focusout", onChange)
  }
}

/**
 * El foco está en `scope` y llegó con el teclado. `:focus-visible` es la heurística del navegador:
 * un botón pulsado con el mouse no la cumple.
 */
function keyboardFocusIn(scope: Element | null) {
  const active = document.activeElement
  return !!active && !!scope?.contains(active) && active.matches(":focus-visible")
}

/**
 * Foco de una tarjeta de la compra que cambia bajo el teclado. Al completar o cancelar desaparece el
 * botón que se pulsó, y la tarjeta de términos que ya no valen ocupa el lugar de la de aprobación:
 * sin esto el foco caería al `<body>` y el lector de pantalla perdería el sitio.
 *
 * - `state` identifica lo que muestra la tarjeta; `settled` dice si en ese estado ya no quedan los
 *   controles con los que se llegó (completada, cancelada). Los estados intermedios (aprobando,
 *   cancelando) dejan el botón en su sitio con `aria-disabled`, así que el foco no se mueve.
 * - Solo si se pulsó con el teclado el foco pasa al título, que dice el estado nuevo. Con el mouse
 *   no se toca: al empezar el turno siguiente vuelve al compositor, listo para escribir.
 * - `autoFocus` hace lo mismo al montar: la tarjeta reemplaza a otra que tenía el foco.
 * - `announce` es falso solo si el foco llegó al título, que el lector lee al llegar. En cualquier
 *   otro caso avisa la región de estado.
 */
export function useCardFocus<Card extends HTMLElement>(
  state: string,
  settled: boolean,
  autoFocus = false,
) {
  const cardRef = useRef<Card>(null)
  const titleRef = useRef<HTMLHeadingElement>(null)
  // Se lee durante el render, antes de que React quite el botón que tenía el foco.
  const keyboardInside = useSyncExternalStore(
    subscribeToFocus,
    () => keyboardFocusIn(cardRef.current),
    () => false,
  )
  // Al montar, el foco sigue en la tarjeta que esta reemplaza, que aún no se quitó.
  const [shown, setShown] = useState(() => ({
    state,
    takeFocus: autoFocus && keyboardFocusIn(document.body),
  }))
  let takeFocus = shown.takeFocus
  if (shown.state !== state) {
    takeFocus = settled && keyboardInside
    setShown({ state, takeFocus })
  }

  useLayoutEffect(() => {
    if (!takeFocus) return
    const active = document.activeElement
    // Otro control (el compositor, otra tarjeta) ya tiene el foco: no se lo quita, y se anuncia.
    if (active && active !== document.body && !cardRef.current?.contains(active)) {
      setShown((current) => ({ ...current, takeFocus: false }))
      return
    }
    titleRef.current?.focus()
  }, [state, takeFocus])

  return { cardRef, titleRef, announce: !takeFocus }
}
