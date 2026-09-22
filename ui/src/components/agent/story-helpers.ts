import { useEffect, useState } from "react"

// Simulaciones locales para historias: temporizadores, sin red. Las historias montan el componente
// con `key` para reiniciar la simulación cuando cambian los args.

/** Texto que crece por tramos, como las operaciones `append-text` del stream. */
export function useStreamingText(fullText: string, charsPerTick = 5, intervalMs = 45) {
  const [length, setLength] = useState(0)

  useEffect(() => {
    const timer = setInterval(() => {
      setLength((current) => Math.min(fullText.length, current + charsPerTick))
    }, intervalMs)
    return () => clearInterval(timer)
  }, [fullText, charsPerTick, intervalMs])

  return { text: fullText.slice(0, length), done: length >= fullText.length }
}

/** Avanza de 0 a `lastPhase` cada `intervalMs`; con `loop` vuelve a empezar tras una pausa. */
export function usePhase(lastPhase: number, intervalMs: number, loop = false) {
  const [phase, setPhase] = useState(0)

  useEffect(() => {
    const timer = setInterval(() => {
      setPhase((current) => (current < lastPhase ? current + 1 : loop ? 0 : current))
    }, intervalMs)
    return () => clearInterval(timer)
  }, [lastPhase, intervalMs, loop])

  return phase
}
