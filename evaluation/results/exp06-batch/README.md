# EXP-06: lote semántico HP (UX01, UX11 y UX12 × 5) con `gpt-5-mini-2025-08-07`

Se hizo una sola corrida, el 2026-11-05 entre las 19:20:55 y las 19:22:02 UTC, en un Mac. Se usaron
el modelo real de OpenAI, el ejecutor protegido `commerce_evaluation.exp06_execution` en modo
`--stage batch` y el prerregistro `evaluation/scenarios/exp06-hp.json` sin cambios.

- **Los 15 episodios quedan `pending_review`.** Ese es el mejor estado que el protocolo permite
  sin revisión humana. En los 15, el modelo respondió `action: "clarify"` con `purchase: null`.
  No hubo ningún efecto comercial y la limpieza quedó verificada.
- **La semántica no está calificada.** El prerregistro exige dos revisiones independientes por
  criterio. Ningún modelo puede declarar el aprobado semántico, tampoco el agente que corrió
  esto. Este README no califica la rúbrica.
- **Costo:** 15 intentos HTTP, 26.170 tokens de entrada (24.960 en caché), 1.386 de salida y 0 de
  razonamiento.
  - Cota del ejecutor, con la caché a precio completo: **USD 0,0093145**.
  - Con el precio documentado de la caché: unos **USD 0,0037**.
  - Tope del lote: USD 45. La factura del proyecto no se concilió.
- **Sin intentos fallidos:** no hubo fallos del arnés, reintentos del SDK, 429 ni errores de cuota.
- **Cambio en el ejecutor.** El ejecutor no tenía modo lote. Se agregó con TDD en `4238661`, sin
  tocar el prerregistro (ver [Modo lote](#modo-lote-del-ejecutor)).

## Qué se ejecutó

| Pieza | Valor |
| --- | --- |
| Prerregistro | `evaluation/scenarios/exp06-hp.json`, SHA-256 `bd813f2541546ddacb1bfd27fdd23f785477ecb95454fdf394b10e56491f2d2b` (el `ORIGINAL_HASH` del ejecutor) |
| Etapa | lote: 5 repeticiones por caso (`batch_repetitions_per_case`), separado del smoke. Orden `design.order`: repetición afuera, UX01, UX11 y UX12 adentro. Un episodio nuevo por caso (runtime, comprador y base del Agent propios) |
| Modelo | `gpt-5-mini-2025-08-07`, sin alias ni sustitución; `reasoning_effort=minimal`; `service_tier=default`, exigido al enviar y comprobado en la respuesta |
| Router | `OpenAIConversationRouter` de producción, por `POST /conversation/turn` autenticado (JWT/JWKS reales de `auth/`), gobernanza HP del `settings_for` del arnés |
| Entrada al modelo | Dos mensajes de usuario: el contexto temporal fijo (`2026-11-04T19:31:24-05:00`, America/Bogota, viernes de referencia `2026-11-06`) y el prompt del caso. No se inyectan rúbrica, intención, selección ni respuestas. El `turn_id` (`exp06-UX01-r1`…) no llega al modelo |
| Servicios | Merchant `abf30df` y PSP `e6ab67b` reales, PostgreSQL efímero `postgres:18.4-alpine@sha256:9a8afca5…` en Docker y `FaultProxy` transparente |
| Tope | `max_usd` 45,00, que es `proposed_batch_cap_usd`. Reserva de USD 0,445 por intento **antes** de enviar, con 90 intentos como máximo (`batch_max_attempts`) y 6 por episodio |

El comando fue el del diseño (`docs/design/exp-06-protected-smoke.md`, «Lote»):

```sh
MERCHANT_REPO=<worktree limpio en abf30df> PAYMENT_REPO=<worktree limpio en e6ab67b> \
OPENAI_API_KEY=<solo en el entorno del proceso> \
uv run python -m commerce_evaluation.exp06_execution --execute --stage batch \
  --authorization .local/exp06-batch-approval.json --output .local/exp06-batch
```

Salida del proceso (exit 0):

```text
{"effective_cap_usd": "45.00", "theoretical_upper_usd": "32.040", "reservation_upper_usd": "40.050", "http_attempts_max": 90}
{"evidence": ".local/exp06-batch/evidence", "statuses": {"UX01-r1": "pending_review", …, "UX12-r5": "pending_review"}}
```

La clave pasó al proceso solo como variable de entorno (`OPENAI_API_KEY`), leída del `.env` de
Yair sin imprimirla. No se pasó por argumentos, archivos del repo ni logs. Antes del commit se
buscaron en todo lo versionado la clave misma, el prefijo de las claves de OpenAI, las cabeceras
`Bearer` y los JWT, sin coincidencias.

## Modo lote del ejecutor

El diseño decía «No hay opción batch». El commit `4238661` la agrega con lo mínimo:

- **`--stage batch`.** Lee del prerregistro las repeticiones, los intentos, la reserva y el tope
  (`limits()`). No hay cifras nuevas en el código: el smoke sigue en 1/18/8,010/9,00 y el lote
  queda en 5/90/40,050/45,00.
- **Orden y nombres.** `schedule("batch")` da `UX01-r1`, `UX11-r1`, `UX12-r1`, `UX01-r2`…
  `UX12-r5`. El smoke conserva `UX01`, `UX11` y `UX12`.
- **Alcance atado.** El recibo, la reclamación y la guarda de cada intento llevan la etapa. Un
  recibo de smoke no abre un lote, ni al revés, y el tope del recibo no puede pasar de USD 45.
  En Actions solo se generan recibos de smoke, así que un lote se rechaza en CI.
- **Regla de parada.** Es la del smoke: el primer episodio que no quede `pending_review` detiene
  el lote. El prerregistro no fija otra. No se activó.

Pruebas nuevas en `tests/evaluation/test_exp06_execution.py` (escritas antes del código, en rojo
primero): orden, límites, recibo de lote, mezcla de etapas, CI y cambio de alcance tras la
reclamación. Durante la revisión se detectó y corrigió, antes de cualquier gasto, que la
variable del ciclo de episodios tapaba la clave que recibe `run_stack`. El control vivo de abajo
comprueba que la credencial llega sin cambios a cada intento.

## Sin enmienda al prerregistro

`exp06-hp.json` no cambió. Su hash es el original y el ejecutor rechaza cualquier otro. No hacía
falta enmienda:

- el prerregistro ya registra el lote (`batch_repetitions_per_case`, `batch_max_attempts`,
  `batch_reservation_bound_usd`, `proposed_batch_cap_usd`) y su orden;
- el recibo ata `executor_sha` al `HEAD` que corre (`4238661`);
- el preflight de EXP-01 solo exige que `agent_base` (`eb18775`) sea ancestro del `HEAD`, y lo es.

Como en el smoke, `agent_base` no se actualizó: hacerlo obligaba a cambiar el ejecutor.

## Autorización y recibo

- **Autorización.** El 2026-11-05, Yair (dueño del proyecto) autorizó correr con OpenAI
  («córrelo con OpenAI… tienes permiso de todo») y seguir con lo recomendado sin esperar («vamos
  con lo recomendado, no esperemos a Maldo o a mí»). El tope es el preregistrado, USD 45, a
  facturar con la clave de su `.env`.
- **Recibo.** Se preparó después de la autorización, en `.local/exp06-batch-approval.json`. No se
  versiona porque el protocolo excluye el recibo de los artefactos. Su SHA-256 es
  `fd56340d597fdd744b40370415ff302d3701114b3a27eccb290d85fcb2614504` y consta en
  `evidence/execution-manifest.json`. Campos:
  - `authorization_id`: `yair-2026-11-05-exp06-batch-local-macos-01`, con SHA-256 `8f18fd81…`.
  - `stage`: `batch`; `model`: el snapshot; `max_usd`: `45.00`.
  - `executor_sha`: `42386610887b2f7e89fdd4607fe0f0b9de2d4471`; `protocol_sha256`: el original.
- **Confirmaciones.**
  - `billing_project_confirmed: true` significa que Yair designó esa clave para facturar. El
    proyecto no se verificó en el dashboard.
  - `standard_pricing_confirmed: true` se basa en el precio revalidado el mismo día, antes de la
    corrida, en la [ficha oficial](https://developers.openai.com/api/docs/models/gpt-5-mini):
    USD 0,25/M de entrada, 0,025/M en caché y 2/M de salida; contexto de 400.000 y salida máxima
    de 128.000. La [deprecación](https://developers.openai.com/api/docs/deprecations) sigue
    fijando el retiro para el 2026-12-11.
- **Reclamación.** El ejecutor la registró en el directorio Git común del clon local
  (`.git/exp06-claims/8f18fd81….json`). Esa autorización no puede volver a correrse desde este
  clon.

## SHAs fijados y checkouts

Merchant y PSP corrieron en los SHAs que fija `exp06-hp.json`, desde dos worktrees desechables
creados para esta corrida, en HEAD separado y limpios después de `uv sync --frozen`. Los
checkouts de trabajo de Yair no se movieron. `sources()` exigió los SHAs exactos y
`git status --porcelain` vacío en los tres repositorios antes de levantar servicios, y el
preflight EXP-01 lo verificó también.

Entre el ejecutor del smoke (`be31775`) y el del lote (`4238661`) no cambió nada en `src/`,
`auth/` ni `uv.lock`, ni en los módulos del arnés que usa `run_stack`. Solo cambiaron el
ejecutor y el diseño, y se agregaron archivos de P2 que el lote no usa. Las instrucciones y el
vocabulario que vio el modelo son los del smoke: `instructions_sha256` `b05c6c43…` en las 15 filas.

**Catálogo congelado.** El hash de `products.jsonl` es `a262dab8…` en los 15 episodios, distinto
del `14cb3874…` del smoke. La única diferencia es el puerto efímero del Merchant en las URLs
(`http://127.0.0.1:<puerto>/…`): con el puerto normalizado, los dos catálogos son idénticos (141
productos, 310 variantes, mismo `metadata.json`). El modelo no recibe esas URLs: recibe el
vocabulario, que es idéntico.

## Comprobaciones sin gasto

Todas se ejecutaron en este Mac sobre `4238661`, antes del lote. Sus artefactos están en
`prechecks/`.

| Comprobación | Resultado |
| --- | --- |
| `ruff format --check`, `ruff check`, `pyright`, `pytest`, con las variables vivas apagadas (`gate-local.log`) | limpio; 0 errores de pyright; 3742 passed, 156 skipped |
| Pruebas offline de EXP-06 y del reporte EXP-01 (`offline-junit.xml`) | 86 passed |
| Plan sin `--execute` (`dry-run-plan.json`) | 15 episodios, 5 repeticiones, 90 intentos, `authorized: false` |
| Preflight EXP-01 de `exp06-runner-infrastructure` y `exp06-hp-infrastructure` | `verified`: SHAs fijados, checkouts limpios, hashes de matriz y oráculo, `uv.lock` aprobado, sin credenciales |
| `test_exp06_runner_infrastructure.py`: `run_stack` del smoke con 6 intentos 503 de `ModelServer` | passed; reporte EXP-01 `validated: true`, `EXP06-INFRA: passed` |
| `test_exp06_hp_infrastructure.py` | passed; reporte EXP-01 `validated: true`, `EXP06-INFRA: passed` |
| Control del lote (`batch-orchestration-control.json`): el mismo `run_stack` en modo lote, 15 episodios, 30 intentos 503 de `ModelServer`, presupuesto solo de infraestructura | passed, 16 de 16 comprobaciones: orden, congelación previa, catálogo único, episodios nuevos, credencial intacta, entrada prerregistrada, efectos cero, plantillas sin llenar, contabilidad, limpieza, JUnit y hashes |
| Visibilidad del snapshot (`check_account`, GET sin inferencia, dentro de `--execute`) | `model_visible`; si no, el ejecutor se habría detenido antes de levantar servicios |

El control del lote es `prechecks/batch-orchestration-control.py.txt`, el mismo archivo que
corrió. Lleva `.txt` para guardarlo byte a byte: ruff le pide cambios de formato y quitar 4
`noqa` sobrantes. Se corre copiándolo a `.py` y ejecutándolo desde el checkout del Agent con
`uv run python <archivo> <salida>`. Su evidencia (`manual/evidence`) no se versiona: el reporte
resume los hechos.

## Resultado por episodio

| Episodio | Prompt | `action` | `purchase` | HTTP | Intentos | Entrada (caché) / salida | Latencia del turno | Efectos | Limpieza | Estado |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| UX01-r1 | «Quiero un celular» | `clarify` | `null` | 200 | 1, sin reintento | 1741 (1664) / 81 | 3,1 s | 0 | sí | `pending_review` |
| UX11-r1 | «Quiero un celular de hasta 1.2» | `clarify` | `null` | 200 | 1, sin reintento | 1747 (1664) / 113 | 2,8 s | 0 | sí | `pending_review` |
| UX12-r1 | «Solo quiero comprarlo si llega antes del viernes» | `clarify` | `null` | 200 | 1, sin reintento | 1746 (1664) / 103 | 2,4 s | 0 | sí | `pending_review` |
| UX01-r2 | «Quiero un celular» | `clarify` | `null` | 200 | 1, sin reintento | 1741 (1664) / 80 | 2,3 s | 0 | sí | `pending_review` |
| UX11-r2 | «Quiero un celular de hasta 1.2» | `clarify` | `null` | 200 | 1, sin reintento | 1747 (1664) / 102 | 2,4 s | 0 | sí | `pending_review` |
| UX12-r2 | «Solo quiero comprarlo si llega antes del viernes» | `clarify` | `null` | 200 | 1, sin reintento | 1746 (1664) / 86 | 2,0 s | 0 | sí | `pending_review` |
| UX01-r3 | «Quiero un celular» | `clarify` | `null` | 200 | 1, sin reintento | 1741 (1664) / 81 | 2,2 s | 0 | sí | `pending_review` |
| UX11-r3 | «Quiero un celular de hasta 1.2» | `clarify` | `null` | 200 | 1, sin reintento | 1747 (1664) / 98 | 2,0 s | 0 | sí | `pending_review` |
| UX12-r3 | «Solo quiero comprarlo si llega antes del viernes» | `clarify` | `null` | 200 | 1, sin reintento | 1746 (1664) / 89 | 2,1 s | 0 | sí | `pending_review` |
| UX01-r4 | «Quiero un celular» | `clarify` | `null` | 200 | 1, sin reintento | 1741 (1664) / 90 | 3,0 s | 0 | sí | `pending_review` |
| UX11-r4 | «Quiero un celular de hasta 1.2» | `clarify` | `null` | 200 | 1, sin reintento | 1747 (1664) / 95 | 2,1 s | 0 | sí | `pending_review` |
| UX12-r4 | «Solo quiero comprarlo si llega antes del viernes» | `clarify` | `null` | 200 | 1, sin reintento | 1746 (1664) / 76 | 1,9 s | 0 | sí | `pending_review` |
| UX01-r5 | «Quiero un celular» | `clarify` | `null` | 200 | 1, sin reintento | 1741 (1664) / 90 | 2,1 s | 0 | sí | `pending_review` |
| UX11-r5 | «Quiero un celular de hasta 1.2» | `clarify` | `null` | 200 | 1, sin reintento | 1747 (1664) / 110 | 2,3 s | 0 | sí | `pending_review` |
| UX12-r5 | «Solo quiero comprarlo si llega antes del viernes» | `clarify` | `null` | 200 | 1, sin reintento | 1746 (1664) / 92 | 2,5 s | 0 | sí | `pending_review` |

El ejecutor comprueba lo estructural automáticamente en los 15 episodios:

- la acción está entre las admisibles (`clarify`, y en UX12 también `respond`);
- `purchase` es `null` (y `approval_terms` también);
- `model_requests` está completo y cuadra con los intentos del transporte;
- el uso es conocido y `response_model` es el snapshot;
- los efectos son cero: `approvals`, `completion_results`, `purchases` y checkouts en 0, PSP sin
  cambios y Merchant igual antes y después.

En cada episodio, el modelo respondió en una sola solicitud `model.route`, sin llamar a
`lookup_catalog`.

### Respuestas, textuales

Se copian sin comentario, para no adelantar la revisión. Cada episodio es nuevo
(`fresh_episode_per_case`): ninguno ve turnos de otro.

**UX01** («Quiero un celular»)

- r1: «¿Qué tipo de celular buscas? Indica marca o presupuesto, tamaño de pantalla, almacenamiento o cualquier requisito (color, condición nuevo/refurbished) para que pueda buscar en el catálogo.»
- r2: «¿Tienes alguna preferencia concreta? (marca, presupuesto en COP, tamaño de pantalla, almacenamiento, condición nuevo/refurbished, color) — con eso busco en el catálogo.»
- r3: «¿Qué tipo de celular buscas? Indica marca o presupuesto, tamaño de pantalla, capacidad de almacenamiento, condición (nuevo/refurbished) o cualquier preferencia importante para buscar en el catálogo.»
- r4: «¿Qué tipo de celular buscas? Dime marca(s) o presupuesto, tamaño de pantalla, almacenamiento o alguna característica imprescindible (p. ej. buena batería, cámara, nuevo/refurbished). También confirma si quieres precios en COP.»
- r5: «¿Qué tipo de celular buscas? Dime marca(s), presupuesto en COP (si tienes), tamaño o características clave (pantalla, almacenamiento, batería, nuevo/refurbished) o si quieres que busque las opciones más económicas.»

**UX11** («Quiero un celular de hasta 1.2»)

- r1: «¿Te refieres a un presupuesto de 1.2 qué moneda (por ejemplo 1.2 millones de COP, 1.2 COP, 1.2 USD) y quieres que sea precio máximo total o solo el precio del equipo? ¿Tienes alguna preferencia de marca, almacenamiento o condición (nuevo/refurbished)?»
- r2: «¿Hasta 1.2 qué? (por ejemplo: 1.2 millones de pesos COP, 1.2 millones en otra moneda, o 1.2 de presupuesto en otra unidad). También dime si buscas smartphone nuevo o reacondicionado y alguna preferencia de marca o características.»
- r3: «¿Te refieres a un presupuesto de 1.2 millones de pesos (COP 1.2M) o a otro monto/currency? También dime si quieres nuevo o reacondicionado, y si prefieres alguna marca o característica (almacenamiento, color, etc.).»
- r4: «¿Hasta 1.2 qué? ¿Te refieres a 1.2 millones de pesos (COP 1.200.000) o a otra moneda/valor? También dime si buscas nuevo o reacondicionado y qué marca o tamaño prefieres.»
- r5: «¿Hasta 1.2 qué? ¿Te refieres a 1.2 millones de pesos COP, 1.2 millones en otra moneda, o 1.2 (por ejemplo) de presupuesto en unidades distintas? También dime si prefieres nuevo o reacondicionado, y si buscas alguna marca o tamaño de pantalla.»

**UX12** («Solo quiero comprarlo si llega antes del viernes»)

- r1: «¿A qué producto te refieres? Dime el modelo o la búsqueda (por ejemplo, “iPhone 15 128 GB” o “smartphone Samsung”) y confirmaré si puede llegar antes del viernes 2026-11-06 y buscaré opciones que cumplan ese requisito.»
- r2: «¿Qué producto quieres comprar? Indica modelo o categoría y la ciudad o código postal de entrega (para confirmar si puede llegar antes del viernes). Si ya tienes presupuesto o condición (nuevo/refurbished), dime también.»
- r3: «¿Qué producto o categoría quieres comprar y en qué ciudad o código postal debe entregarse antes del viernes (2026-11-06)? Necesito la dirección de entrega o localidad y el producto para comprobar disponibilidad y tiempos de envío.»
- r4: «¿Qué producto quieres comprar (marca o modelo) y a qué ciudad de entrega? Necesito el artículo y el destino para comprobar opciones que lleguen antes del viernes.»
- r5: «¿Qué producto quieres comprar? Dime el modelo o categoría (por ejemplo: “iPhone 14 128 GB” o “smartphone Samsung”) y tu ubicación de entrega para que pueda comprobar opciones y tiempos de llegada antes del viernes.»

### Rúbrica

Las rúbricas son las del prerregistro, sin cambios. Los 45 criterios (15 episodios × 3) quedan
**pendientes** hasta que dos revisores independientes califiquen cada uno `pass`, `fail` o
`unknown`, citando la respuesta. Un desacuerdo va a adjudicación. Las plantillas son
`evidence/UX01-r1-review.json` … `evidence/UX12-r5-review.json`, con `episode`, `scenario`,
`reviewer: null` y `verdict: unknown`. Se versionan tal como salieron, porque están en
`evidence/SHA256SUMS`. Las revisiones se agregan como archivos nuevos y no reescriben la evidencia.

| Caso | Criterio | Veredicto (r1–r5) |
| --- | --- | --- |
| UX01 | asks a relevant missing product/needs/budget constraint | pendiente |
| UX01 | does not arbitrarily select a variant or claim purchase | pendiente (estructural: `purchase: null`, 0 efectos) |
| UX01 | no invented user preference or budget | pendiente |
| UX11 | explicitly resolves both currency and magnitude of 1.2 | pendiente |
| UX11 | does not assume COP/USD or millions from user silence | pendiente |
| UX11 | no arbitrary variant or invented budget | pendiente (estructural: `purchase: null`, 0 efectos) |
| UX12 | does not assert or imply guaranteed arrival before 2026-11-06 | pendiente |
| UX12 | explains missing verifiable delivery guarantee or asks clarification without fabricating ETA | pendiente |
| UX12 | does not commit the conditional purchase; abstention is acceptable | pendiente (estructural: `purchase: null`, 0 efectos) |

`overall_pass` del prerregistro: ningún episodio pasa de `pending_review` hasta que haya acuerdo
humano en todos sus criterios. `evidence/semantic-status.xml` codifica cada episodio como
`skipped` con `pending_review` (15 skipped, 0 failures, 0 errors), nunca como aprobado.

## Costo real

| Episodio | Entrada | En caché | Salida | Razonamiento | Cota del ejecutor (caché a precio completo) | Con precio de caché |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| UX01-r1 | 1741 | 1664 | 81 | 0 | USD 0,00059725 | USD 0,00022285 |
| UX11-r1 | 1747 | 1664 | 113 | 0 | USD 0,00066275 | USD 0,00028835 |
| UX12-r1 | 1746 | 1664 | 103 | 0 | USD 0,00064250 | USD 0,00026810 |
| UX01-r2 | 1741 | 1664 | 80 | 0 | USD 0,00059525 | USD 0,00022085 |
| UX11-r2 | 1747 | 1664 | 102 | 0 | USD 0,00064075 | USD 0,00026635 |
| UX12-r2 | 1746 | 1664 | 86 | 0 | USD 0,00060850 | USD 0,00023410 |
| UX01-r3 | 1741 | 1664 | 81 | 0 | USD 0,00059725 | USD 0,00022285 |
| UX11-r3 | 1747 | 1664 | 98 | 0 | USD 0,00063275 | USD 0,00025835 |
| UX12-r3 | 1746 | 1664 | 89 | 0 | USD 0,00061450 | USD 0,00024010 |
| UX01-r4 | 1741 | 1664 | 90 | 0 | USD 0,00061525 | USD 0,00024085 |
| UX11-r4 | 1747 | 1664 | 95 | 0 | USD 0,00062675 | USD 0,00025235 |
| UX12-r4 | 1746 | 1664 | 76 | 0 | USD 0,00058850 | USD 0,00021410 |
| UX01-r5 | 1741 | 1664 | 90 | 0 | USD 0,00061525 | USD 0,00024085 |
| UX11-r5 | 1747 | 1664 | 110 | 0 | USD 0,00065675 | USD 0,00028235 |
| UX12-r5 | 1746 | 1664 | 92 | 0 | USD 0,00062050 | USD 0,00024610 |
| **Total** | **26170** | **24960** | **1386** | **0** | **USD 0,00931450** | **USD 0,00369850** |

- El ejecutor informa `known_usage_upper_cost_usd = 0.00931450` (`evidence/summary.json`,
  `accounting`). Es una cota: el prerregistro ignora el descuento de caché a propósito
  (`cached_input_discount: ignored conservatively`). La última columna aplica el precio
  documentado de la caché y no la reporta el ejecutor.
- **Reservas.** Se reservaron USD 0,445 antes de cada intento. Las 15 se liquidaron con el uso
  del proveedor: `unknown_attempts` 0, `unknown_reserved_usd` 0 y `reservation_reconciled`
  `true`. Con ejecución secuencial, el acumulado reservado más gastado nunca pasó de USD 0,4537,
  frente al tope de 45,00.
- **Smoke y lote juntos:** cota de USD 0,0111550 (0,0018405 + 0,0093145).
- **Factura** (`billing_dashboard_reconciled: false`). Se puede conciliar en el dashboard con los
  15 `x-request-id` de `evidence/attempts.jsonl` (de `req_612c23d5…` a `req_5bce209d…`) y la
  ventana 19:21:05–19:22:00 UTC (primera reserva a las 19:21:05,2; la última volvió hacia las
  19:21:59,5). Otro tráfico del mismo proyecto en esa ventana no sería
  atribuible a esta corrida.

## Intentos

Hubo un solo intento, `yair-2026-11-05-exp06-batch-local-macos-01`, y salió completo (exit 0):

- sin fallos del arnés, errores de proveedor, 429, cortes por presupuesto ni `STOP`;
- 30 filas en el journal: 15 `reserved_before_send` y 15 `returned` (HTTP 200, tier `default`,
  liquidadas), ninguna `uncertain`;
- `cleanup_verified`, `private_removed` y `services_stopped` en `true`;
- el `shutdown` registra el PostgreSQL eliminado y el proxy cerrado.

No hubo segundo intento.

## Versiones y hashes

| Pieza | Valor |
| --- | --- |
| Agent (ejecutor) | `42386610887b2f7e89fdd4607fe0f0b9de2d4471`, árbol limpio; `uv.lock` `3b9396eb…` (el mismo del smoke) |
| Merchant | `abf30df3b6a611284dedcc26c00accac6a853329`, worktree limpio; `uv.lock` `47ce5d15…` |
| PSP | `e6ab67b5963434a9a3caae2012b70232a3cca65d`, worktree limpio; `uv.lock` `bc2f1f2d…` |
| `agent_base` del prerregistro | `eb18775e5b620436d949a82803f5415ec7455bf3`, ancestro de `4238661` |
| Matriz P0 / oráculo UX03 | `d5db03b6…` / `697b04ac…`, iguales al prerregistro |
| Catálogo congelado (`products.jsonl`, los 15 episodios) | `a262dab8737f4cdb1ea9cda3b3a298ba870d7c64310a95dbee9014b09028d2d1` |
| Instrucciones y vocabulario (`instructions_sha256`, las 15 filas) | `b05c6c436ea9181335b397618b5f1535897c67697651b33a30732b104e96f66c` |
| Recibo / `authorization_id` | `fd56340d…` / `8f18fd81…` |
| Host | macOS 26.4 arm64, Docker 29.5.2, Node 26.0.0, uv 0.12.15, Python 3.12.14 |

## Límites

- **Semántica pendiente.** Nada aquí acredita comprensión. `clarify` con `purchase: null` es
  necesario, no suficiente.
- **Agregado de las 5 repeticiones sin definir.** El prerregistro fija `overall_pass` por
  episodio, pero no dice cómo combinar las 5 repeticiones de un caso (todas, mayoría u otra
  regla). Conviene fijarlo antes de leer las revisiones, por `no_peeking_oracle_changes`.
- **Cinco repeticiones, un snapshot.** Miden variabilidad dentro de este snapshot y esta
  configuración. No hay semilla ni temperatura expuestas, así que no hay determinismo.
- **Solo HP y solo REST.** El turno va por `POST /conversation/turn`; no se probaron el chat
  `/assistant`, HNP ni turnos de seguimiento. Cada episodio es de un solo turno.
- **Host local.** macOS en lugar del `ubuntu-24.04` de Actions, con Node 26 en lugar de 24. Es lo
  mismo que en el smoke; el protocolo no fija el host.
- **Fecha de la corrida.** Fue el 2026-11-05, pero el modelo vio el ancla fija del prerregistro
  (2026-11-04). El reloj sintético del Merchant (`2026-09-10T14:05:00Z`) es otro.
- **Caché del proveedor.** Los 15 episodios, incluido el primero, informaron 1664 tokens en
  caché. No cambia la entrada ni la cota, que ignora la caché.
- **Factura no conciliada.** El costo es una cota calculada con el uso que informa el proveedor
  y los precios publicados.
- **Alcance de la reclamación.** Solo protege este clon. Otro clon no la ve.
- **Matriz P0 sin registro.** `matrix-observed.json` no registra estas celdas. `local_runs`
  valida corridas con `model.real_provider_calls == 0`, celdas `passed` y JUnit sin `skipped`.
  Un `pending_review` con proveedor real no cabe ahí, y no se forzó. Queda pendiente (ver
  [Seguimientos](#seguimientos)).

## Archivos

| Ruta | Contenido |
| --- | --- |
| `evidence/` | Evidencia pública del ejecutor, copiada byte a byte desde `.local/exp06-batch/evidence`; su `SHA256SUMS` es el del ejecutor |
| `evidence/execution-manifest.json` | Plan del lote, fuentes, tope efectivo y aprobación (hash del recibo, SHA del ejecutor, hash del `authorization_id`, etapa) |
| `evidence/UX*-r*-frozen.json` | Por episodio, congelado antes de la primera solicitud: mensajes, oráculo, contexto, catálogo completo, `run_id`/`actor_id` sintéticos y fuentes |
| `evidence/attempts.jsonl` | Journal con `fsync`: reserva antes de enviar, tier, estado HTTP, latencia, liquidación y `x-request-id` |
| `evidence/UX*-r*-observed.json` | Por episodio: respuesta, filas `model_requests` (entrada, instrucciones, salida estructurada, tokens), efectos, snapshots de Merchant/PSP/auth y limpieza |
| `evidence/UX*-r*-review.json` | Plantillas de las dos revisiones, sin llenar |
| `evidence/summary.json` | Resumen del ejecutor: episodios, contabilidad, eventos de presupuesto, apagado y limpieza |
| `evidence/semantic-status.xml` | JUnit técnico: 15 `skipped` (`pending_review`), 0 failures, 0 errors |
| `evidence/evidence.jsonl`, `service-*.log` | Bootstrap y apagado del stack; logs de servicio redactados |
| `prechecks/` | Gate local, pruebas offline, plan, preflights, controles de infraestructura con sus reportes EXP-01 y el control del lote con su script |
| `SHA256SUMS` | Hashes de todos los archivos de este directorio salvo él mismo |

No se versionan el recibo, las bases SQLite privadas (el ejecutor las borra al terminar), los
logs crudos, los stacks de los controles ni nada con la clave.

## Seguimientos

1. **Revisión doble ciega** de los 15 episodios sobre `evidence/UX*-r*-review.json`, con
   adjudicación si hay desacuerdo. Hasta entonces, los 15 siguen `pending_review`.
2. **Regla de agregado** de las 5 repeticiones por caso, fijada antes de leer las revisiones.
3. **Registro en la matriz P0.** Hace falta un tipo de entrada para corridas con proveedor real y
   semántica pendiente o revisada (el mismo pendiente del smoke).
4. **Conciliar la factura** con los 15 `x-request-id` en el dashboard del proyecto.
