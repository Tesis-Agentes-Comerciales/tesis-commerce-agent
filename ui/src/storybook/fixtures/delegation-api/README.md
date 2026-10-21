# Respuestas reales de la API de delegaciones

Generadas por `uv run python scripts/export_delegation_replays.py`; no se editan a mano.
`tests/test_delegation_replay.py` verifica que coincidan con la salida actual.

Cada archivo es la secuencia de peticiones de un escenario con la respuesta real del
backend (`tests/delegation_replay.py`): la app de `tests/test_delegation_api.py` con
SQLite real, el feed de `tests/fixtures/assistant_feed` (el mismo de los streams del
chat), el reloj de autoridad en 2026-10-21T15:00:00Z e ids deterministas. Las tareas
avanzan por el ledger, como en esas pruebas. `ui/e2e/delegations.spec.ts` las reproduce
en orden.

## `consent.json`

Consentimiento exitoso: sin delegaciones, propuesta con la ciudad escrita «Bogotá» (el alcance dice «Bogota, DC»), «Autorizar» 30 s después (201) y la lista con la delegación activa.

## `scope-mismatch.json`

El servidor no reconoce el digest del alcance (409 `delegation_scope_mismatch`).

## `proposal-expired.json`

Propuesta con `valid_for_seconds = 60`: la propuesta vale 10 min (TTL), pero el grant vence al minuto, y «Autorizar» al minuto responde 409 `delegation_proposal_expired`.

## `destination-unsupported.json`

Un destino que el comercio no atiende (Medellín): 422 `delegation_destination_unsupported`, sin propuesta.

## `revoke.json`

Tres delegaciones activas: sin reserva, con la reserva tomada y con el pago comprometido. Revocar cada una devuelve `no_effect`, `released` e `in_flight`, y la vista releída.

## `revoke-all.json`

Las mismas tres y una ya revocada: «Revocar todo» devuelve un recibo por cada activa y la lista releída.

## `not-revocable.json`

La lista muestra una delegación activa que vence antes de revocarla: 409 `delegation_not_revocable` y la vista releída, vencida.

## `flag-off.json`

`AGENT_HNP_ENABLED=false`: `/health` sin campos `hnp_*` y las rutas de delegación en 404.
