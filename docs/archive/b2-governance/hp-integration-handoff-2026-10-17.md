# Handoff de integración HP — Agent, Merchant y consentimiento B2

Fecha del estado verificado: 2026-10-17 (Bogotá).  
Base de Agent de la organización: `598aab7003c263aa55f84d282fd82d5952b74bc1` (`main`).

## 1. Evidencia cerrada y alcance realmente implementado

- **Agent en `main`**: fusionados PRs #2–#7; corrección CI del PR #8 incluida. Validación GitHub Actions [#46](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/39083608537): **success**, 1.035 pruebas aprobadas, 51 omitidas, 1 advertencia; Ruff, Pyright, build Python y UI correctos. Esto **no ejecuta** por defecto las pruebas de integración real opt-in.
- **B0 / HP existente**: `src/commerce_agent/api.py`, `approval.py` y la UI React ofrecen un flujo separado de preparación, aprobación y completion sintéticos. Este camino **no es** la demostración de consentimiento gobernado D2-HP del incremento 07C.
- **B2 / D1 → D17**: evidencias de preselección P, binding Q shadow, D1 aplicada y despacho/lookup D17 experimental backend-only. No se conecta automáticamente al flujo comercial público, al modelo ni al modo `enforce` de runtime.
- **B2 / D2-HP**: `d2_consent_api.py` expone una **factoría separada** `create_consent_app(store)`; no está montada en `api.py`. Los tests usan identidades sintéticas (`synthetic_test_human/1`), no una persona autenticada en la UI.
- **Merchant D17 congelado para la evaluación**: `Maldinho18/tesis-commerce-merchant`, commit `9743c9f179c7ad5413e8818766ef3dceb326a917`; permanece en un repositorio distinto del `Tesis-Agentes-Comerciales/tesis-commerce-lab`. No copiar sus archivos directamente al Lab sin acordar cómo integrar repositorios y contratos.

> **Actualización 2026-10-19.** Lo anterior describe `main` al 17-oct. Cambios posteriores:
>
> - **Enforce opt-in (PR #13).** `enforce` existe en el runtime como opt-in, con allowlist de merchants ([guía](../../d1-enforce-runtime-optin.md)). Deniega en vivo POL-02. Un allow termina en `d1_dispatch_blocked`, sin despacho de producción, así que D17 sigue sin conectarse al flujo público. POL-01 nunca deniega en el flujo integrado, porque la guarda B0 corre antes.
> - **Reintento D1 (PR #12).** Un reintento después de un deny o un fallo abre un intento D1 nuevo. Antes, el run quedaba en `d1_unavailable`.
> - **Idempotency-Key D17 (PR #12).** La clave se deriva de la intención: un HMAC sobre run, owner, origen y cuerpo ACP. Antes era aleatoria, y perder `applied.sqlite` permitía un segundo POST.
> - **Decisión D2 por términos (PR #15).** D2 admite una sola decisión terminal por (`checkout_id`, `terms_fingerprint`), no solo una por presentación.
> - **Deny de la evidencia HP/B2 v2.** El deny de esa evidencia usó una política inyectada en tests. No verificó enforce en el flujo real (ver la adenda del PR #10).

## 2. Contratos de integración comprobados

### Merchant experimental D17

La integración Agent 07B-B usa el POST comercial **una sola vez** a
`/lab/governance/checkout-creation/guarded`; la recuperación histórica invoca
`/lab/recovery/guarded-checkout-creation/lookup` (HTTP POST pero semánticamente read-only).
El contrato exige sesión backend auténtica, revisión/digest de oferta, idempotencia, prueba remota verificable y estados `found`/`rejected`/`not_found`/`unrecoverable`/`error`.
`not_found` y la incertidumbre nunca permiten reenviar la creación. No convertir la recuperación en un segundo efecto comercial.

### Consentimiento experimental D2

`create_consent_app` registra rutas separadas:

| Método y ruta | Entrada relevante | Salida / límite |
| --- | --- | --- |
| `POST /experimental/d2/presentations` | `Authorization: Bearer <sesión humana>`; JSON estricto `{}` | Propuesta sellada y challenge **de una sola entrega**; `delivery_assurance=backend_issued_not_ui_seen` |
| `POST /experimental/d2/decisions` | Bearer; `presentation_id`, `checkout_id`, `terms_fingerprint`, `future_action`, `challenge`, `decision`, `affirmative` | Recibo durable, `continuation_allowed=false` |
| `GET /experimental/d2/presentations/{presentation_id}` | Bearer; misma identidad autorizada | Estado y recibo histórico, sin recuperar challenge en claro |

`future_action` queda fijada a `request_checkout_preparation_review`; **no** autoriza `prepare_checkout`, `update`, `cancel`, `complete_purchase`, token PSP, orden o pago. No reutilizar el `ApprovalService` de B0 para un checkout D17 inicialmente `not_ready_for_payment`.

La factoría requiere una instancia `SqliteHumanConsent` configurada por backend con los stores, claves y autenticador adecuados. Este proyecto **no** entrega un adaptador de IdP de producción ni demuestra que un humano haya visto el contenido en pantalla. No montar esta factoría públicamente ni dar por implementada una UI real solo por estar definidos los endpoints.

## 3. Secuencia segura de demostración B2

1. Fijar SHAs de Agent y Merchant y registrar el entorno sintético; no exponer tokens, secretos ni datos personales.
2. Producir preselección P y binding Q con pruebas y credenciales del emisor **solo de pruebas**.
3. Verificar D1 **aplicada** separada de shadow y operación reservada/claimed durable.
4. Ejecutar como máximo **un** POST D17 por operación; comprobar respuesta remota y recuperación histórica `lookup/found`, o el bloqueo correspondiente.
5. Presentar los términos D2 desde la prueba histórica validada. Registrar `presented_at`, `valid_until`, fingerprint, nonce y sujeto sintético del escenario **sin publicar el nonce ni credenciales**.
6. Aceptar o rechazar con identidad sintética autenticada; comprobar recibo y exclusividad terminal. Incluir un caso negativo (cambio de precio/digest, nonce repetido o vencimiento).
7. Mostrar explícitamente `continuation_allowed=false` y ausencia de `complete`, PSP u orden **en el camino B2**. Conservar reportes sanitizados y SHAs.

**No presentar la demo B0 con UI/AP2 sintético como una demo de D2 gobernado**. Si se muestran las dos, separarlas y rotular sus garantías.

## 4. Condiciones para mostrar D2 con UI humana real

**Pendiente, requiere trabajo de integración explícito y nueva validación de seguridad**:

- Adaptador backend `HumanAuthenticator` para sesión humana real, revocación, ownership, episodio y origen UI; nunca aceptar identidad indicada por el LLM/frontend como autoridad.
- Mostrar la instantánea de términos exactos de D17 en la interfaz sin alterar precio, envío, moneda, checkout, vigencia o acción; registrar qué presentación/fingerprint recibió realmente la UI.
- Acto afirmativo explícito `accept`/rechazo; enviar el challenge recibido por canal privado una sola vez y protegerlo de logs, URLs y model prompts.
- Invalidar vistas vencidas, distintas de checkout/owner/run y duplicadas; conservar `continuation_allowed=false`.
- Pruebas HTTP y UI de aceptación y rechazo con una **sesión real de prueba** y casos adversariales; distinguirlas de identidades sintéticas unitarias.
- Coordinar con el dueño de la UI antes de tocar React o `src/commerce_agent/api.py`; no cambiar runtime `enforce`.

> **Actualización 2026-10-19.** El runtime ya tiene `enforce` como opt-in experimental (PR #13). El default sigue en `off`. Ver la [guía](../../d1-enforce-runtime-optin.md). La indicación de no cambiarlo regía para este handoff. Cualquier uso posterior sigue esa guía. El opt-in no habilita despacho D17 ni D2 con UI real.

## 5. Decisiones y dependencias que quedan abiertas

| Prioridad | Decisión / evidencia pendiente | Criterio de cierre |
| --- | --- | --- |
| Alta | Destino de Merchant D17 en la organización | Repositorio y revisión del commit congelado; no mezclarlo ciegamente con Lab |
| Alta | Demo B2 backend-only reproducible en entorno compartido | Un escenario positivo + uno adversarial con eventos y pruebas de ausencia de efectos no autorizados |
| Alta | Identidad y UI D2 real | Adaptador autenticado, términos mostrados y acto humano verificable; de lo contrario reportar alcance sintético |
| Alta | Ensamble final de evidencias para tesis | SHAs, comandos, fechas, suites, omisiones, resultados y límites; sin secretos |
| Separada | Demo comercial B0/UI/PSP | Documentar como flujo distinto, no como enforcement gobernado |
| Separada | HNP / D3, runtime público `enforce`, dinero real | Fuera del incremento 07C y de la habilitación actual |

> **Actualización 2026-10-19.**
>
> - **Runtime `enforce`.** Pasó a opt-in experimental (PR #13). Es solo deny y no tiene despacho de producción.
> - **Demo B2 reproducible.** La evidencia live (06D-C, 07A, D17 y los tres casos finales del paquete HP/B2 v2) se ejecutó una vez en Windows. Los casos finales usaron un worker de inyección de fallos que no está en ningún repositorio. 06D-C y 07A exigen además Windows y su commit auditado. Por eso esa evidencia no es reproducible desde `main`.
> - **Suites live.** El PR #15 quitó las rutas `C:/` fijas: las suites leen `MERCHANT_REPO` y `PG_BIN_DIR` ([tests/integration/README.md](../../../tests/integration/README.md)). 07B-P y 07B-B recolectan en macOS y Linux, todavía sin una corrida real. Otra rama está construyendo una suite live portable con Docker (en curso).

## 6. Validaciones mínimas antes de dar por integrada la demo

- Ejecutar CI del Agent sobre el commit consumido: `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyright`, `uv run pytest`, `uv build`, `npm run lint` y `npm run build` en `ui/`.
- Separar los tests opt-in y los escenarios reales Agent ↔ Merchant (PostgreSQL descartable) de la suite ordinaria. Reportar qué se ejecutó realmente, qué se omitió y en qué SO.
- Fijar el contrato Merchant congelado y verificar la compatibilidad del checkout/recovery con la versión exacta usada en la demo.
- No declarar aprobado el consentimiento humano real hasta tener la evidencia de autenticación e interacción efectiva.

## 7. Documentación de respaldo

- [07B-P — preselección](ec-gov-07b-p-preselection-authority-results.md)
- [07B-Q — shadow binding](ec-gov-07b-q-preselection-d1-binding-results.md)
- [07B-B — D1 aplicado / D17](ec-gov-07b-b-d17-integration-results.md)
- [07C — D2-HP acotado](ec-gov-07c-d2-hp-consent-results.md)

Este documento es un **handoff de integración**, no una afirmación de que la integración/UI real ya esté implementada. No habilita ninguna capacidad comercial.
