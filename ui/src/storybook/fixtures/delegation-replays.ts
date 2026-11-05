import consent from "./delegation-api/consent.json"
import destinationUnsupported from "./delegation-api/destination-unsupported.json"
import flagOff from "./delegation-api/flag-off.json"
import notRevocable from "./delegation-api/not-revocable.json"
import proposalExpired from "./delegation-api/proposal-expired.json"
import proposalSuperseded from "./delegation-api/proposal-superseded.json"
import revokeAll from "./delegation-api/revoke-all.json"
import revoke from "./delegation-api/revoke.json"
import scopeMismatch from "./delegation-api/scope-mismatch.json"

// Secuencias reales de la API de delegaciones, generadas por el backend
// (`uv run python scripts/export_delegation_replays.py`; ver delegation-api/README.md). Las
// historias, las pruebas unitarias y el E2E las reproducen tal cual, en orden.

export type ReplayExchange = {
  method: string
  path: string
  /** El cuerpo que envió la secuencia original, o `null`. */
  request: unknown
  status: number
  body: unknown
}

export const DELEGATION_REPLAYS = {
  consent,
  scopeMismatch,
  proposalExpired,
  proposalSuperseded,
  destinationUnsupported,
  revoke,
  revokeAll,
  notRevocable,
  flagOff,
} satisfies Record<string, ReplayExchange[]>

/** La respuesta número `nth` (desde 0) a `method path` en la secuencia. */
export function replayed(replay: readonly ReplayExchange[], route: string, nth = 0): ReplayExchange {
  const [method, path] = route.split(" ")
  const found = replay.filter((exchange) => exchange.method === method && exchange.path === path)[nth]
  if (!found) throw new Error(`The replay has no answer #${nth} to ${route}`)
  return found
}
