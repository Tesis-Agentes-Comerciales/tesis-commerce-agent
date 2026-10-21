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
 * Foco de una tarjeta de la compra que cambia bajo el teclado. Al completar o cancelar desaparece el
 * botón que se pulsó, y la tarjeta de términos que ya no valen ocupa el lugar de la de aprobación:
 * sin esto el foco caería al `<body>` y el lector de pantalla perdería el sitio.
 *
 * - `state` identifica lo que muestra la tarjeta; `settled` dice si en ese estado ya no quedan los
 *   controles con los que se llegó (completada, cancelada). Los estados intermedios (aprobando,
 *   cancelando) dejan el botón en su sitio con `aria-disabled`, así que el foco no se mueve.
 * - Si el foco estaba en la tarjeta al asentarse, pasa a su título, que dice el estado nuevo. El
 *   lector lo lee al llegar, así que esa vez la región de estado calla (`announce` falso) para no
 *   repetirlo. Con el foco en otra parte no se toca, y avisa la región de estado.
 * - `autoFocus` hace lo mismo al montar: la tarjeta reemplaza a otra que tenía el foco.
 */
export function useCardFocus<Card extends HTMLElement>(
  state: string,
  settled: boolean,
  autoFocus = false,
) {
  const cardRef = useRef<Card>(null)
  const titleRef = useRef<HTMLHeadingElement>(null)
  // Se lee durante el render, antes de que React quite el botón que tenía el foco.
  const focusInside = useSyncExternalStore(
    subscribeToFocus,
    () => cardRef.current?.contains(document.activeElement) ?? false,
    () => false,
  )
  const [shown, setShown] = useState({ state, takeFocus: autoFocus })
  let takeFocus = shown.takeFocus
  if (shown.state !== state) {
    takeFocus = settled && focusInside
    setShown({ state, takeFocus })
  }

  useLayoutEffect(() => {
    if (!takeFocus) return
    const active = document.activeElement
    // Otro control (el compositor, otra tarjeta) ya tiene el foco: no se lo quita.
    if (active && active !== document.body && !cardRef.current?.contains(active)) return
    titleRef.current?.focus()
  }, [state, takeFocus])

  return { cardRef, titleRef, announce: !takeFocus }
}
