# Handoff de integración HP — Agent, Merchant y consentimiento B2

Fecha del estado verificado: 2026-10-17 (Bogotá).  
Base de Agent de la organización: `598aab7003c263aa55f84d282fd82d5952b74bc1` (`main`).

## 1. Evidencia cerrada y alcance realmente implementado

- **Agent en `main`**: fusionados PRs #2–#7; corrección CI del PR #8 incluida. Validación GitHub Actions [#46](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/39083608537): **success**, 1.035 pruebas aprobadas, 51 omitidas, 1 advertencia; Ruff, Pyright, build Python y UI correctos. Esto **no ejecuta** por defecto las pruebas de integración real opt-in.
- **B0 / HP existente**: `src/commerce_agent/api.py`, `approval.py` y la UI React ofrecen un flujo separado de preparación, aprobación y completion sintéticos. Este camino **no es** la demostración de consentimiento gobernado D2-HP del incremento 07C.
- **B2 / D1 → D17**: evidencias de preselección P, binding Q shadow, D1 aplicada y despacho/lookup D17 experimental backend-only. No se conecta automáticamente al flujo comercial público, al modelo ni al modo `enforce` de runtime.
- **B2 / D2-HP**: `d2_consent_api.py` expone una **factoría separada** `create_consent_app(store)`; no está montada en `api.py`. Los tests usan identidades sintéticas (`synthetic_test_human/1`), no una persona autenticada en la UI.
- **Merchant D17 congelado para la evaluación**: `Maldinho18/tesis-commerce-merchant`, commit `9743c9f179c7ad5413e8818766ef3dceb326a917`; permanece en un repositorio distinto del `Tesis-Agentes-Comerciales/tesis-commerce-lab`. No copiar sus archivos directamente al Lab sin acordar cómo integrar repositorios y contratos.

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

## 5. Decisiones y dependencias que quedan abiertas

| Prioridad | Decisión / evidencia pendiente | Criterio de cierre |
| --- | --- | --- |
| Alta | Destino de Merchant D17 en la organización | Repositorio y revisión del commit congelado; no mezclarlo ciegamente con Lab |
| Alta | Demo B2 backend-only reproducible en entorno compartido | Un escenario positivo + uno adversarial con eventos y pruebas de ausencia de efectos no autorizados |
| Alta | Identidad y UI D2 real | Adaptador autenticado, términos mostrados y acto humano verificable; de lo contrario reportar alcance sintético |
| Alta | Ensamble final de evidencias para tesis | SHAs, comandos, fechas, suites, omisiones, resultados y límites; sin secretos |
| Separada | Demo comercial B0/UI/PSP | Documentar como flujo distinto, no como enforcement gobernado |
| Separada | HNP / D3, runtime público `enforce`, dinero real | Fuera del incremento 07C y de la habilitación actual |

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
