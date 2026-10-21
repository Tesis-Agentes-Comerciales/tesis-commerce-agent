# Cadena de evidencia decisión–consentimiento–efecto (P2)

Estado: propuesta del 21-oct-2026 para la ola «HP al 100 %» (contrato de coordinación, punto 4).
Se entrega en tres PR apilados: A (webhooks), B (recibos enlazados) y C (verificador y H2).

## Problema

H2 dice que B2 aumenta la proporción de episodios en los que un verificador puede enlazar
intención, decisión de política, checkout, aprobación, mandatos, recibos y resultado
(`docs/b2-governance/landscape-research-question.md`). En el piloto v1 la traza y la evidencia
quedaron sin evaluar, y hoy faltan tres piezas para medirlo:

1. **El webhook del comercio nunca llega.** El comercio guarda cada `order_create` en su outbox
   (`webhook_deliveries`) en la misma transacción que la orden, pero no lo envía solo: hay que
   correr su dispatcher (`python -m commerce_lab.db webhook-dispatch`, README del comercio).
   `demo-stack` y el runner del piloto ya le pasan `WEBHOOK_RECEIVER_URL` y
   `MERCHANT_WEBHOOK_SECRET`, pero nadie despacha, así que `webhook_events` del agente queda vacía
   y la traza no muestra «El comercio registró la orden».
2. **El consentimiento y el efecto no quedan enlazados.** `approvals` guarda los mandatos AP2
   completos y se sobrescribe (`ON CONFLICT DO UPDATE`); `completion_results` guarda los recibos.
   Ninguna fila de solo inserción une la aprobación con la frescura que la habilitó, ni los hashes
   de los mandatos con los recibos del PSP y del comercio. Una fila cambiada después no se nota.
3. **No hay verificador.** Nada recalcula los digests ni revisa que cada eslabón referencie al
   anterior.

## Alcance

| PR | Rama | Qué entrega |
| --- | --- | --- |
| A | `feat/evidence-chain-webhooks` | `demo-stack` despacha el outbox del comercio cada 2 s; el runner del piloto lo despacha cuando la compra llegó a una orden, antes de apagar al agente. Prueba live: el `order_create` firmado llega a `webhook_events` del run y a su traza |
| B | `feat/evidence-chain-receipts` | Tabla `ap2_evidence` de solo inserción y encadenada por digest, con un registro `consent` al aprobar y uno `effect` al completar. Solo hashes, identificadores y referencias |
| C | `feat/evidence-chain-verifier` | `uv run verify-evidence` (miembro `evaluation`): JSON por eslabón y la métrica H2 agregada |

**Fuera de alcance:** el eslabón D2 verificado (llega cuando P3 esté en `main`, en un PR posterior),
filtrar el despacho del comercio por corrida (necesita un cambio en el comercio, ver Riesgos),
verificar firmas JWS sin conexión (ver Decisiones) y cambios en la traza o la UI: el paso
`order_event` ya existe en el contrato.

## PR A: webhooks del comercio

- `demo-stack`: el lanzador corre el dispatcher del comercio en su checkout cada 2 s
  (`webhook-dispatch --limit 20`) mientras vigila los servicios. Solo le pasa
  `WEBHOOK_RECEIVER_URL` (el origen del agente) y `MERCHANT_WEBHOOK_SECRET` (el mismo del
  agente). Las entregas van a `webhooks.log` del arranque; una falla se avisa una vez y la demo
  sigue.
- Runner del piloto: si `GET /purchases/{run_id}` trae `order_id`, despacha en lotes hasta vaciar
  el outbox, con el agente de la repetición todavía arriba, y guarda en la fila
  `merchant_webhooks` (`count`, `by_outcome`). Sin orden no despacha y el campo queda `null`. El
  piloto v1 nunca completa, así que sus filas no cambian.

## PR B: contrato de `ap2_evidence`

Se crea con las mismas condiciones que `hp_evidence` (gobernanza `shadow`, o `enforce` con el flujo
`governed`); en `off` no existe. Una fila por registro y como máximo un registro de cada tipo por
run, encadenados como `hp_evidence` (`sequence`, `previous_digest`, `digest` con dominio
`ap2-evidence/v1`). Triggers impiden `UPDATE`, `DELETE` y reemplazos.

`consent`, escrito justo después de guardar la aprobación:

| Campo | Contenido |
| --- | --- |
| `run_id`, `checkout_id`, `approved_at`, `checkout_fingerprint` | Los de la fila de `approvals` |
| `actor_digest` | `sha256:` del `actor_id` (no se guarda el sujeto) |
| `freshness_digest` | Digest del último registro de `hp_evidence` del run (la frescura `approve`) o `null` |
| `checkout_hash` | `base64url(sha256(checkout_jwt))`, el `transaction_id` de los mandatos |
| `checkout_mandate_sd_hash`, `payment_mandate_sd_hash` | `sd_hash` de cada mandato: lo que referencian los recibos |

`effect`, escrito justo después de registrar la orden:

| Campo | Contenido |
| --- | --- |
| `run_id`, `checkout_id`, `order_id` | De la completación |
| `checkout_receipt` | `digest` (`sha256:` del JWT), `issuer`, `status`, `reference`, `order_id` |
| `payment_receipt` | `digest`, `issuer`, `status`, `reference`, `payment_id` |

Nunca se guardan mandatos, recibos, el token delegado (`vt_…`), secretos ni datos del comprador:
`checkout_jwt` lleva el checkout con buyer y dirección, así que solo entra su hash. Si el registro
no se puede escribir, la compra sigue como hoy y queda el aviso `ap2_evidence_unavailable`: el
verificador lo verá como `missing`.

## PR C: verificador

`uv run verify-evidence --agent-db agent.db [--agent-db …] [--run RUN_ID] [--psp-db payments.db]`
lee cada base en solo lectura y escribe JSON. Cada run de `purchases` produce nueve eslabones, en
el orden del contrato; cada uno es `ok`, `missing`, `mismatch` o `not_applicable`, con un `detail`
corto y las referencias (ids y digests, nunca tokens):

| Eslabón | Qué recalcula y qué referencia revisa |
| --- | --- |
| `intent` | `sha256` del cuerpo público de la fuente `intent` de `d1_provenance_sources` |
| `d1_decision` | El ledger D1 (`governance_evidence`): digest del cuerpo, del input y del artefacto de política. Su `intent_ref` es el eslabón anterior. La reejecución es diagnóstica: solo un resultado distinto es `mismatch` |
| `checkout_created` | La operación D1 apunta al `evidence_id`, al digest del cuerpo y al del input de la decisión; su resultado es `confirmed` con el `checkout_id` de la compra; digest del sobre |
| `freshness` | Cadena de `hp_evidence` recalculada; `present` y, si hubo aprobación o intento, `approve` del mismo checkout. Una aprobación guardada exige `approve` en `allow` |
| `d2_decision` | `not_applicable` con la política `b2-pilot-v1` o sin registro D2 |
| `approval` | `consent` coincide con `approvals` y su `freshness_digest` es el `approve` |
| `mandates` | Hash del `checkout_jwt`, digests de las divulgaciones SD-JWT, `checkout_hash` y `transaction_id` de ambos mandatos, monto del pago = total firmado, `sd_hash` = `consent` |
| `receipts` | Digest de cada recibo = `effect`; `reference` = `sd_hash` del mandato; `order_id` del recibo del comercio = orden; `effect` sigue a `consent` en la cadena. Con `--psp-db`, el PSP tiene ese pago para ese mandato |
| `order_webhook` | Un `order_create` de esa orden y ese checkout, con el `sha256` del cuerpo recalculado |

**Aplicabilidad.** Un eslabón aplica si la compra llegó a ese paso según su propio estado
(`purchases.stage`, `approvals`, `completion_results`, la cancelación del comprador). Lo que no
pasó es `not_applicable`: un S04 bloqueado al aprobar termina en `freshness` (con `approve` en
`block`) y su cadena puede estar completa. `complete` es verdadero cuando ningún eslabón es
`missing` ni `mismatch`.

**H2.** `h2` resume los runs verificados: `effects` (compras con orden: `n`, `complete`, `rate`)
es la métrica de H2; `all_runs` usa la misma regla sobre todas las compras, y `by_link` cuenta
cada estado por eslabón para ver dónde se corta la cadena. B0' no guarda intención, decisión ni
registros enlazados, así que sus eslabones salen `missing`: es la diferencia que mide H2.

## Criterios de aceptación

- A: con el comercio real y Postgres efímero, el `order_create` firmado de una compra B2 llega a
  `webhook_events` del run y la traza tiene el paso `order_event`. Pruebas sin servicios del
  lanzador y del runner.
- B: una compra B2 AP2 completa deja `consent` y `effect` encadenados, sin tokens, mandatos,
  recibos ni datos del comprador; `off` no crea la tabla; un fallo al escribir no rompe la compra.
- C: con fixtures de cadena completa, incompleta (B0', S04 bloqueado, sin webhook) y manipulada
  (mandato, recibo, webhook, `hp_evidence` y `consent` alterados), cada eslabón sale con el estado
  esperado; la prueba live del PR A verifica una cadena completa con el comercio real.

## Decisiones

- **Despacho con la CLI del comercio**, sin código del comercio en el agente ni en la evaluación.
- **Tabla nueva en vez de ampliar `hp_evidence`:** su `CHECK` de `record_kind` no admite tipos
  nuevos en bases existentes y la traza rechaza cuerpos que no conoce.
- **Firmas.** El agente ya verifica la firma del `checkout_jwt` y de ambos recibos al recibirlos.
  El verificador recalcula hashes y referencias; no vuelve a verificar firmas porque las claves
  públicas no se guardan. Queda como límite declarado.

## Riesgos

- **El dispatcher no filtra por corrida.** En la base compartida entrega cualquier entrega vencida,
  también las de arranques anteriores; el agente las guarda, pero ninguna traza ni el verificador
  las usa porque no corresponden a una orden suya. Filtrar exige `webhook-dispatch --run-id` en el
  comercio: se propone, no se cambia aquí.
- **Orden de fusión.** P1 y P3 cambian los modelos D1. El verificador usa el ledger del agente para
  leer D1, así que hereda su compatibilidad con la evidencia `b2-pilot-v1`.
