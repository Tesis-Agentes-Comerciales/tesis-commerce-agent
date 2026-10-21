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

**Fuera de alcance:** verificar el contenido del registro D2 (llega cuando P3 esté en `main`, en
un PR posterior; este PR solo exige que exista),
filtrar el despacho del comercio por corrida (necesita un cambio en el comercio, ver Riesgos),
verificar firmas JWS sin conexión (ver Decisiones) y cambios en la traza o la UI: el paso
`order_event` ya existe en el contrato.

## PR A: webhooks del comercio

- `demo-stack`: el lanzador corre el dispatcher del comercio en su checkout cada 2 s
  (`webhook-dispatch --limit 20`) mientras vigila los servicios. Solo le pasa
  `WEBHOOK_RECEIVER_URL` (el origen del agente) y `MERCHANT_WEBHOOK_SECRET` (el mismo del
  agente). Las entregas van a `webhooks.log` del arranque; una falla se avisa una vez y la demo
  sigue. Cada pasada corre en su propio grupo de procesos con un límite de 10 s: si se cuelga,
  o al apagar con una pasada en curso, el lanzador mata el grupo entero (`uv run` deja un hijo
  `python`), así que no quedan huérfanos. Un error al lanzarlo, al leer su salida o al escribir
  `webhooks.log` tampoco detiene la demo ni retrasa la detección de un servicio caído.
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

`uv run verify-evidence --agent-db agent.db [--agent-db …] [--run RUN_ID] [--psp-db payments.db]
[--output archivo.json]` lee cada base en solo lectura y escribe JSON
(`schema_version: evidence-chain-verification/1`, `runs`, `h2`). Cada run de `purchases` produce
`database`, `run_id`, `stage`, `order_id`, `links` y `complete`; `links` trae los nueve eslabones
en el orden del contrato, cada uno con `status` (`ok`, `missing`, `mismatch` o
`not_applicable`), un `detail` corto y `refs` (ids y digests, nunca tokens). `order_id` es la
orden anclada (ver Aplicabilidad). Una base repetida en `--agent-db` (otra ruta o un enlace a la
misma) se verifica una vez. Sale con 0 si todas las cadenas están completas, 1 si alguna no y 2
si los argumentos o las bases no sirven. El runner lo llama desde Python:
`verify_database(path, run_id=…, psp_database=…)` y `h2_summary(runs)`.

| Eslabón | Qué recalcula y qué referencia revisa |
| --- | --- |
| `intent` | `sha256` del cuerpo público de la fuente `intent` de `d1_provenance_sources`. Aplica también a un D1 `deny`, que no deja selección |
| `d1_decision` | El ledger D1 (`governance_evidence`): digest del cuerpo, del input y del artefacto de política. Su `intent_ref` es el eslabón anterior. La reejecución es diagnóstica: solo un resultado distinto es `mismatch` |
| `checkout_created` | La operación D1 apunta al `evidence_id`, al digest del cuerpo y al del input de la decisión; su resultado es `confirmed` con el `checkout_id` de la compra; digest del sobre. Con D1 en `deny`, un checkout, una operación de creación o una aprobación es `mismatch` (bypass) |
| `freshness` | Cadena de `hp_evidence` recalculada; `present` y, si hubo aprobación o intento, `approve` del mismo checkout. Una aprobación guardada exige `approve` en `allow` |
| `d2_decision` | Obligatorio en una compra aprobada cuya política D1 es `b2-hp-v2` o lista reglas D2 (POL-03..06): sin registro es `missing`, nunca `not_applicable`. Un registro D2 (fase distinta de D1 en `governance_evidence`) sale `missing` con `content_verified: false` hasta que el PR posterior verifique su contenido: presente no cuenta como verificado. Sin esa política, `not_applicable` |
| `approval` | `consent` coincide con `approvals`, verifica en la cadena hasta él y su `freshness_digest` es el `approve` |
| `mandates` | Hash del `checkout_jwt`, digests de las divulgaciones SD-JWT, `checkout_hash` y `transaction_id` de ambos mandatos, monto del pago = total firmado, `sd_hash` = `consent`. El total, el monto y las monedas deben existir: dos nulos no coinciden |
| `receipts` | Digest de cada recibo = `effect`; `reference` = `sd_hash` del mandato; `order_id` del recibo del comercio = orden; `effect` sigue a `consent` en la cadena. Con `--psp-db`, el PSP tiene ese pago para ese mandato |
| `order_webhook` | Un `order_create` de esa orden y ese checkout, con el `sha256` del cuerpo recalculado |

**Aplicabilidad.** Un eslabón aplica si la compra llegó a ese paso según cualquier rastro que el
paso deja, no solo según la fila de la compra. D1 aplica si hubo selección, checkout o filas D1
(`d1_provenance_sources`, `governance_evidence`, `d1_checkout_operations`) del run. La aprobación
queda anclada por `approvals`, una etapa aprobada o `consent`; la completación, por
`completion_results`, `effect`, el `order_id` de la compra o un `order_create` del comercio, que
se busca por orden y también por `data.checkout_session_id` = checkout de la compra. Así,
`approval`, `mandates`, `receipts` y `order_webhook` aplican aunque se hayan borrado
`approvals`, `completion_results` u `order_id`, y la orden se ancla en ese orden (compra →
completación → `effect` → webhook) para contar en `h2.effects`. Lo que no pasó es
`not_applicable`: un S04 bloqueado al aprobar termina en `freshness` (con `approve` en `block`);
un D1 `deny` verifica `intent` y `d1_decision` y deja el resto en `not_applicable`; un rechazo
definitivo del comercio al crear (`definitive_failure`, `VERIFIED_NOT_CREATED`, P5a) deja
`checkout_created` y todo lo posterior en `not_applicable`, después de verificar que la operación
referencia la decisión, y una orden después de ese rechazo es `mismatch`. Una creación
`uncertain` es `missing`. Una compra cancelada sin registros de frescura no muestra si llegó a
ver los términos, así que su `freshness` sale `not_applicable`.

**Completa.** Una compra con orden o `effect` está completa solo si sus nueve eslabones son `ok`:
para ella no se acepta `not_applicable`. Cualquier otra compra está completa si ningún eslabón es
`missing` ni `mismatch`. Es la regla del pre-registro v2 (`is_complete(links, effect=…)`).
Consecuencia: mientras D2 no esté en `main` ni se verifique su registro, ninguna compra con orden
queda completa; la cadena de una compra B2 con orden sale `ok` en todo salvo `d2_decision`.

**Webhook.** El verificador no espera eventos: lee la base una vez. Quien lo llama despacha antes
el outbox del comercio con el agente arriba (PR A) y verifica después de que el agente salió.

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
- C: con fixtures de cadena completa, incompleta (B0', S04 bloqueado, sin webhook, `b2-hp-v2`
  sin D2), D1 `deny` y manipulada (mandato, recibo, webhook, `hp_evidence`, `consent`, filas
  borradas tras aprobar y un `deny` vuelto `allow` o con checkout), cada eslabón sale con el
  estado esperado; las pruebas live verifican la compra B2 con el comercio real (todo `ok` salvo
  D2, incompleta) y los dos turnos de POL-02 (`deny` y `b2` en espera de aprobación).

## Decisiones

- **Despacho con la CLI del comercio**, sin código del comercio en el agente ni en la evaluación.
- **Tabla nueva en vez de ampliar `hp_evidence`:** su `CHECK` de `record_kind` no admite tipos
  nuevos en bases existentes y la traza rechaza cuerpos que no conoce.
- **Firmas.** El agente ya verifica la firma del `checkout_jwt` y de ambos recibos al recibirlos.
  El verificador recalcula hashes y referencias; no vuelve a verificar firmas porque las claves
  públicas no se guardan. Queda como límite declarado.
- **Cadenas por prefijo.** `consent` y `effect` se verifican con la cadena hasta cada uno: un
  `effect` alterado no contamina el eslabón `approval`.
- **El ledger D1 del agente.** El verificador usa `SqliteGovernanceEvidenceStore` en solo lectura
  (digest del cuerpo, alcance y artefactos de política); la reejecución de la política es
  diagnóstica y solo un resultado distinto (`GOV_OUTPUT_INVALID`) es `mismatch`.

## Riesgos

- **El dispatcher no filtra por corrida.** En la base compartida entrega cualquier entrega vencida,
  también las de arranques anteriores; el agente las guarda, pero ninguna traza ni el verificador
  las usa porque no corresponden a una orden suya. Filtrar exige `webhook-dispatch --run-id` en el
  comercio: se propone, no se cambia aquí.
- **Orden de fusión.** P1 y P3 cambian los modelos D1. El verificador usa el ledger del agente para
  leer D1, así que hereda su compatibilidad con la evidencia `b2-pilot-v1`.
