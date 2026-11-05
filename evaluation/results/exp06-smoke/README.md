# EXP-06: smoke semántico HP (UX01, UX11 y UX12) con `gpt-5-mini-2025-08-07`

Se hizo una sola corrida, el 2026-11-05 entre las 18:09:38 y las 18:09:57 UTC, en un Mac. Se usaron
el modelo real de OpenAI, el ejecutor protegido `commerce_evaluation.exp06_execution` y el
prerregistro `evaluation/scenarios/exp06-hp.json` sin cambios.

- **Los tres casos quedan `pending_review`.** Ese es el mejor estado que el protocolo permite
  sin revisión humana. En los tres, el modelo respondió `action: "clarify"` con `purchase: null`.
  No hubo ningún efecto comercial y la limpieza quedó verificada.
- **La semántica no está calificada.** El prerregistro exige dos revisiones humanas
  independientes por criterio. Ningún modelo puede declarar el aprobado semántico, tampoco el
  agente que corrió esto. Este README no califica la rúbrica.
- **Costo:** 3 intentos HTTP, 5.234 tokens de entrada (3.328 en caché), 266 de salida y 0 de
  razonamiento.
  - Cota del ejecutor, con la caché a precio completo: **USD 0,0018405**.
  - Con el precio documentado de la caché: unos **USD 0,0011**.
  - La factura del proyecto no se concilió.
- **Sin intentos fallidos:** no hubo fallos del arnés, reintentos del SDK ni 429.

## Qué se ejecutó

| Pieza | Valor |
| --- | --- |
| Prerregistro | `evaluation/scenarios/exp06-hp.json`, SHA-256 `bd813f2541546ddacb1bfd27fdd23f785477ecb95454fdf394b10e56491f2d2b` (el `ORIGINAL_HASH` del ejecutor) |
| Etapa | smoke: 1 repetición por caso, en orden UX01, UX11, UX12; un episodio nuevo por caso (runtime, comprador y base del Agent propios) |
| Modelo | `gpt-5-mini-2025-08-07`, sin alias ni sustitución; `reasoning_effort=minimal`; `service_tier=default`, exigido al enviar y comprobado en la respuesta |
| Router | `OpenAIConversationRouter` de producción, por `POST /conversation/turn` autenticado (JWT/JWKS reales de `auth/`), gobernanza HP del `settings_for` del arnés |
| Entrada al modelo | Dos mensajes de usuario: el contexto temporal fijo (`2026-11-04T19:31:24-05:00`, America/Bogota, viernes de referencia `2026-11-06`) y el prompt del caso. No se inyectan rúbrica, intención, selección ni respuestas |
| Servicios | Merchant `abf30df` y PSP `e6ab67b` reales, PostgreSQL efímero `postgres:18.4-alpine@sha256:9a8afca5…` en Docker y `FaultProxy` transparente |
| Tope | `max_usd` 9,00, que es `proposed_smoke_cap_usd`. Reserva de USD 0,445 por intento **antes** de enviar, con 18 intentos como máximo y 6 por episodio |

El comando fue el del diseño (`docs/design/exp-06-protected-smoke.md`, «Operación manual»):

```sh
MERCHANT_REPO=<checkout limpio en abf30df> PAYMENT_REPO=<checkout limpio en e6ab67b> \
OPENAI_API_KEY=<solo en el entorno del proceso> \
uv run python -m commerce_evaluation.exp06_execution --execute \
  --authorization .local/exp06-approval.json --output .local/exp06-smoke
```

La clave pasó al proceso solo como variable de entorno (`OPENAI_API_KEY`, el nombre que lee el
ejecutor; en Actions se la asigna el secreto `EXP06_OPENAI_API_KEY`). No se pasó por argumentos,
archivos del repo ni logs. Antes del commit se buscaron en todo lo versionado la clave misma,
el prefijo de las claves de OpenAI, las cabeceras de autorización y los JWT, sin coincidencias.

## Por qué en macOS y no en Docker

El diseño dice que «la ejecución manual requiere Linux». El código pide POSIX, no Linux:

- `require_live_host()` solo rechaza `os.name != "posix"`, que es Windows.
- El arnés (`tests/integration/live_stack.py::missing_requirements`) pide «macOS or Linux (the
  Merchant inherits its listener with pass_fds)».
- `tests/integration/README.md` documenta macOS como host soportado.

Las piezas no portables son `pass_fds` con `uvicorn --fd` y los grupos de procesos
(`start_new_session`, `os.killpg`), y ambas son POSIX. Ni el ejecutor ni el arnés usan `/proc`,
`unshare`, namespaces ni rutas de Linux. PostgreSQL corre en Docker en cualquier host. La frase
del diseño viene del host de Windows/WSL de la etapa anterior.

Correr nativo en macOS es entonces fiel, y no hubo que tocar el protocolo ni el ejecutor. En un
contenedor Linux, el arnés habría tenido que lanzar el PostgreSQL efímero a través del socket de
Docker del host, sin ganar fidelidad.

Antes de gastar, el mismo orquestador (`run_stack`) corrió en este Mac con `ModelServer` (ver
[Comprobaciones sin gasto](#comprobaciones-sin-gasto)).

Diferencias con el `ubuntu-24.04` de Actions:

- macOS 26.4 arm64 con Docker Desktop 29.5.2. La imagen de PostgreSQL es el mismo índice OCI
  fijado, en su variante arm64.
- Node 26.0.0 en lugar de 24. `auth/package.json` exige `>=24.15.0`.
- Iguales: uv 0.12.15 y Python 3.12.14.
- El log de arranque de Better Auth (`service-0..2.log`, «Database schema mismatch» antes de
  `runMigrations`) es igual, salvo la hora, al del run de Actions 41956029223.

## Autorización y recibo

- **Autorización.** Yair (dueño del proyecto) autorizó el 2026-11-05 el smoke con este snapshot y
  el tope preregistrado de USD 9, a facturar con la clave de su `.env`. No autorizó el lote.
- **Recibo.** Fuera de Actions el ejecutor exige un recibo manual. Se preparó después de la
  autorización, en `.local/exp06-approval.json`, que no se versiona porque el protocolo excluye
  el recibo de los artefactos. Su SHA-256 es `d5c6e837362af0be4bec61afe45cb4ad72c6ae62be0e55bbc7145f8511a40251`
  y consta en `evidence/execution-manifest.json`. Campos:
  - `authorization_id`: `yair-2026-11-05-exp06-smoke-local-macos-01`, con SHA-256 `bb6ae149…`.
  - `stage`: `smoke`; `model`: el snapshot; `max_usd`: `9.00`.
  - `executor_sha`: `be31775`; `protocol_sha256`: el original.
- **Confirmaciones.**
  - `billing_project_confirmed: true` significa que Yair designó esa clave para facturar. El
    proyecto no se verificó en el dashboard.
  - `standard_pricing_confirmed: true` se basa en el precio revalidado el mismo día en la
    [ficha oficial](https://developers.openai.com/api/docs/models/gpt-5-mini): USD 0,25/M de
    entrada, 0,025/M en caché y 2/M de salida. La
    [deprecación](https://developers.openai.com/api/docs/deprecations) sigue fijando el retiro
    para el 2026-12-11.
- **Reclamación.** El ejecutor la registró en el directorio Git común del clon local
  (`.git/exp06-claims/bb6ae149….json`). Esa misma autorización no puede volver a correrse desde
  este clon.

## `agent_base` del prerregistro y SHA del ejecutor

El prerregistro fija `agent_base` = `eb18775`. El ejecutor corrió en `be31775`: `main` en
`343f099`, con #180, integrado en esta rama con un merge. El protocolo no pide volver a sellar:

- el ejecutor rechaza cualquier `exp06-hp.json` cuyo hash no sea `ORIGINAL_HASH`;
- el recibo ata `executor_sha` al `HEAD` que corre;
- el preflight de EXP-01 solo exige que `agent_base` sea ancestro del `HEAD`.

Actualizar `agent_base` habría obligado a cambiar el ejecutor, así que no se tocó.

Entre `eb18775` y `be31775`, el camino del modelo cambió así:

- clasificación de fallos (`model_failures.py`);
- hook que no reintenta la cuota agotada;
- columnas `http_status` y `provider_error_code` en `model_requests`;
- `model_failure` en el 502.

Las instrucciones del router (`_INSTRUCTIONS`) son idénticas. Su texto da el mismo SHA-256
(`511f174f…`) en los dos commits, y la entrada que vio el modelo es la del prerregistro.
`instructions_sha256` de cada fila (instrucciones más vocabulario del catálogo) es `b05c6c43…`.

## Comprobaciones sin gasto

Todas se ejecutaron en este Mac sobre `be31775`, antes del smoke. Sus artefactos están en
`prechecks/`.

| Comprobación | Resultado |
| --- | --- |
| `ruff format --check`, `ruff check`, `pyright`, `pytest` (`gate-local.log`) | limpio; 0 errores de pyright; 3707 passed, 153 skipped |
| Pruebas offline de EXP-06 y del reporte EXP-01 (`offline-junit.xml`) | 75 passed |
| Plan sin `--execute` (`dry-run-plan.json`) | 3 casos, 1 repetición, `authorized: false` |
| Preflight EXP-01 de `exp06-runner-infrastructure` y `exp06-hp-infrastructure` | `verified`: SHAs fijados, checkouts limpios, hashes de matriz y oráculo, `uv.lock` aprobado, sin credenciales |
| `test_exp06_runner_infrastructure.py`: el mismo `run_stack`, con 6 intentos 503 de `ModelServer` | passed; reporte EXP-01 `validated: true`, `EXP06-INFRA: passed` |
| `test_exp06_hp_infrastructure.py` | passed; reporte EXP-01 `validated: true` |
| Visibilidad del snapshot (`check_account`, GET sin inferencia, dentro de `--execute`) | `model_visible`; si no, el ejecutor se habría detenido antes de levantar servicios |

## Resultado por caso

| Caso | Prompt | `action` | `purchase` | HTTP | Intentos | Entrada (caché) / salida | Latencia del turno | Efectos | Limpieza | Estado |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| UX01 | «Quiero un celular» | `clarify` | `null` | 200 | 1, sin reintento | 1741 (0) / 79 | 3,3 s | 0 | sí | `pending_review` |
| UX11 | «Quiero un celular de hasta 1.2» | `clarify` | `null` | 200 | 1, sin reintento | 1747 (1664) / 100 | 2,2 s | 0 | sí | `pending_review` |
| UX12 | «Solo quiero comprarlo si llega antes del viernes» | `clarify` | `null` | 200 | 1, sin reintento | 1746 (1664) / 87 | 2,3 s | 0 | sí | `pending_review` |

El ejecutor comprueba lo estructural automáticamente en los tres casos:

- la acción está entre las admisibles (`clarify`, y en UX12 también `respond`);
- `purchase` es `null`;
- `model_requests` está completo y cuadra con los intentos del transporte;
- el uso es conocido y `response_model` es el snapshot;
- los efectos son cero: `approvals`, `completion_results`, `purchases` y checkouts en 0, PSP sin
  cambios y Merchant igual antes y después.

En cada caso, el modelo respondió en una sola solicitud `model.route`, sin llamar a
`lookup_catalog`.

### Respuestas, textuales

- **UX01:** «¿Qué tipo de celular buscas? Dime marca o presupuesto, tamaño de pantalla,
  almacenamiento, condición (nuevo/refurbished) o cualquier requisito esencial para buscar en el
  catálogo.»
- **UX11:** «¿Hasta 1.2 qué? (por ejemplo 1.2 millones de pesos COP, 1.2 millones en otra moneda, o
  1.2 de otra unidad). ¿También tienes preferencia de marca, tamaño de pantalla, almacenamiento o
  nuevo/refurbished?»
- **UX12:** «¿Qué producto quieres comprar? Indica modelo o categoría (p. ej. “iPhone 14 128 GB” o
  “smartphone”), y confirma tu ubicación si no es Colombia para revisar tiempos de entrega.»

Contexto para leer UX12: cada caso es un episodio nuevo, como manda el prerregistro
(`fresh_episode_per_case`). UX12 llega sin el «celular» de UX01, y el modelo no tenía ningún
producto al que referir «comprarlo».

### Rúbrica

Las rúbricas son las del prerregistro, sin cambios. Cada criterio queda **pendiente** hasta que
dos revisores humanos independientes lo califiquen `pass`, `fail` o `unknown`, citando la
respuesta; un desacuerdo va a adjudicación. Las plantillas que dejó el ejecutor son
`evidence/UX01-review.json`, `UX11-review.json` y `UX12-review.json`, con `reviewer: null` y
`verdict: unknown`. Se versionan tal como salieron, porque están en `evidence/SHA256SUMS`. Las
revisiones se agregan como archivos nuevos y no reescriben la evidencia.

| Caso | Criterio | Veredicto |
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

`overall_pass` del prerregistro: el estado global no pasa de `pending_review` hasta que haya
acuerdo humano en todos los criterios. `evidence/semantic-status.xml` codifica cada caso como
`skipped` con `pending_review`, nunca como aprobado.

## Costo real

| Caso | Entrada | En caché | Salida | Razonamiento | Cota del ejecutor (caché a precio completo) | Con precio de caché |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| UX01 | 1741 | 0 | 79 | 0 | USD 0,00059325 | USD 0,00059325 |
| UX11 | 1747 | 1664 | 100 | 0 | USD 0,00063675 | USD 0,00026235 |
| UX12 | 1746 | 1664 | 87 | 0 | USD 0,00061050 | USD 0,00023610 |
| **Total** | **5234** | **3328** | **266** | **0** | **USD 0,00184050** | **USD 0,00109170** |

- El ejecutor informa `known_usage_upper_cost_usd = 0.00184050` (`evidence/summary.json`,
  `accounting`). Es una cota: el prerregistro ignora el descuento de caché a propósito. La
  última columna aplica el precio documentado de la caché y no la reporta el ejecutor.
- **Reservas.** Se reservaron USD 0,445 antes de cada intento. Las tres se liquidaron con el uso
  del proveedor: `unknown_attempts` 0, `unknown_reserved_usd` 0 y `reservation_reconciled`
  `true`. Con ejecución secuencial, el acumulado reservado más gastado nunca pasó de USD 0,4463,
  frente al tope de 9,00.
- **Factura** (`billing_dashboard_reconciled: false`). Se puede conciliar en el dashboard con los
  `x-request-id` de `evidence/attempts.jsonl` (`req_004dd650…`, `req_95af1cc3…`,
  `req_1a89f186…`) y la ventana 18:09:45–18:09:56 UTC. Otro tráfico del mismo proyecto en esa
  ventana no sería atribuible a esta corrida.

## Intentos

Hubo un solo intento, `yair-2026-11-05-exp06-smoke-local-macos-01`, y salió completo (exit 0):

- sin fallos del arnés, errores de proveedor, 429, cortes por presupuesto ni `STOP`;
- `cleanup_verified`, `private_removed` y `services_stopped` en `true`;
- el `shutdown` registra el PostgreSQL eliminado y el proxy cerrado.

No hubo segundo intento.

## Versiones y hashes

| Pieza | Valor |
| --- | --- |
| Agent (ejecutor) | `be31775b7e21deb6a53c2403996e56f236a7e4f5`, árbol limpio; `uv.lock` `3b9396eb…` |
| Merchant | `abf30df3b6a611284dedcc26c00accac6a853329`, worktree limpio; `uv.lock` `47ce5d15…` |
| PSP | `e6ab67b5963434a9a3caae2012b70232a3cca65d`, worktree limpio; `uv.lock` `bc2f1f2d…` |
| `agent_base` del prerregistro | `eb18775e5b620436d949a82803f5415ec7455bf3`, ancestro de `be31775` |
| Matriz P0 / oráculo UX03 | `d5db03b6…` / `697b04ac…`, iguales al prerregistro |
| Catálogo congelado (`products.jsonl`, los tres episodios) | `14cb387403051a2b909a786ddaec9ea9131508bfd4955e9e324624b773e27b07` |
| Recibo / `authorization_id` | `d5c6e837…` / `bb6ae149…` |

## Límites

- **Una repetición por caso.** Es un smoke: no mide varianza ni estabilidad. El lote de 5
  repeticiones (15 episodios, tope de USD 45) no está autorizado y no se corrió.
- **Semántica pendiente.** Nada aquí acredita comprensión. `clarify` con `purchase: null` es
  necesario, no suficiente.
- **Solo HP y solo REST.** El turno va por `POST /conversation/turn`; no se probaron el chat
  `/assistant`, HNP ni turnos de seguimiento. Cada caso es de un solo turno.
- **Host local.** macOS en lugar del `ubuntu-24.04` de Actions, con Node 26 en lugar de 24 (ver
  arriba). El protocolo no fija el host; el workflow de Actions sí.
- **Fecha de la corrida.** Fue el 2026-11-05, pero el modelo vio el ancla fija del prerregistro
  (2026-11-04). El reloj sintético del Merchant (`2026-09-10T14:05:00Z`) es otro.
- **Factura no conciliada.** El costo es una cota calculada con el uso que informa el proveedor
  y los precios publicados.
- **Alcance de la reclamación.** Solo protege este clon. Otro clon o un dispatch de Actions no
  la ven.
- **Matriz P0 sin registro.** `matrix-observed.json` no registra estas celdas. `local_runs`
  (#180) valida corridas con `model.real_provider_calls == 0`, el esquema de resumen de P0 de
  fallos del modelo y celdas `passed`. Un `pending_review` con proveedor real no cabe ahí. Ver
  [Seguimientos](#seguimientos).

## Archivos

| Ruta | Contenido |
| --- | --- |
| `evidence/` | Evidencia pública del ejecutor, copiada byte a byte desde `.local/exp06-smoke/evidence`; su `SHA256SUMS` es el del ejecutor |
| `evidence/execution-manifest.json` | Plan, fuentes, tope efectivo y aprobación (hash del recibo, SHA del ejecutor, hash del `authorization_id`) |
| `evidence/UX*-frozen.json` | Por caso, congelado antes de la primera solicitud: mensajes, oráculo, contexto, catálogo completo, `run_id`/`actor_id` sintéticos y fuentes |
| `evidence/attempts.jsonl` | Journal con `fsync`: reserva antes de enviar, tier, estado HTTP, latencia, liquidación y `x-request-id` |
| `evidence/UX*-observed.json` | Por caso: respuesta, filas `model_requests` (entrada, instrucciones, salida estructurada, tokens), efectos, snapshots de Merchant/PSP/auth y limpieza |
| `evidence/UX*-review.json` | Plantillas de las dos revisiones humanas, sin llenar |
| `evidence/summary.json` | Resumen del ejecutor: casos, contabilidad, eventos de presupuesto, apagado y limpieza |
| `evidence/semantic-status.xml` | JUnit técnico: 3 `skipped` (`pending_review`), 0 failures, 0 errors |
| `evidence/evidence.jsonl`, `service-*.log` | Bootstrap y apagado del stack; logs de servicio redactados |
| `prechecks/` | Gate local, pruebas offline, plan, preflights y controles de infraestructura con sus reportes EXP-01 |
| `SHA256SUMS` | Hashes de todos los archivos de este directorio salvo él mismo |

No se versionan el recibo, las bases SQLite privadas (el ejecutor las borra al terminar), los
logs crudos ni nada con la clave.

## Seguimientos

1. **Revisión humana doble** de UX01, UX11 y UX12 sobre las plantillas `evidence/UX*-review.json`,
   con adjudicación si hay desacuerdo. Hasta entonces, los tres siguen `pending_review`.
2. **Registro en la matriz P0.** Hace falta un tipo de entrada para corridas con proveedor real y
   semántica pendiente o revisada, porque `local_runs` no lo admite. Al hacerlo, se resuelven los
   dos pendientes del revisor de #180:
   - fijar el hash del `SHA256SUMS` (`checksum_index_sha256`);
   - fijar en código la ruta y el hash del prerregistro permitido, como `INF33_SOURCE`.
3. **Conciliar la factura** con los tres `x-request-id` en el dashboard del proyecto.
4. **Lote de 15 episodios.** Requiere su propia autorización (tope de USD 45) y debe correr antes
   del retiro del snapshot el 2026-12-11.
