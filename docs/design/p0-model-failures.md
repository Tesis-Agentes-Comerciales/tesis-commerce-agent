# P0 — Fallos del proveedor del modelo (INF27, INF28, INF29, INF33)

Estado: propuesta para aprobación de Yair (5-nov-2026). Base: `main` adbe120. Escenarios de la
matriz `evaluation/scenarios/p0/matrix.json` que quedaron del lado de Yair en el reparto con Maldo.

## 1. Objetivo

Un fallo del proveedor del LLM nunca produce una compra, no se reintenta cuando reintentar no sirve,
queda registrado con su causa y se le explica al comprador con la causa correcta.

## 2. Comportamiento actual (verificado en adbe120)

- Los tres clientes del modelo — router HP (`llm/conversation_router.py`), selector HNP
  (`llm/delegated_choice.py`) y revisor HNP (`llm/intent_review.py`) — crean el SDK con
  `max_retries=1` y convierten cualquier `OpenAIError` o `ValidationError` en
  `IntentInterpretationError`.
- **HP:** el turno termina `intent_model_error` (HTTP 502) con «No pude interpretar la solicitud
  con el modelo en este momento.», sea cual sea la causa.
- **HNP, selector:** cualquier fallo termina `selection_unavailable` antes de cualquier efecto
  (escalated en B2-HNP, failed en B0-HNP; #154).
- **HNP, revisor:** un fallo o una salida inválida da `REVIEW_UNAVAILABLE`
  (`not_confirmed` / `review_unavailable`) y la tarea termina `choice_not_confirmed`, escalada. La
  UI ya muestra «la revisión no estuvo disponible».
- **Reintentos:** el SDK reintenta todo 429, también la cuota agotada (`insufficient_quota`), que
  no se recupera reintentando: dos intentos HTTP inútiles por llamada. La única excepción es que el
  servidor responda `x-should-retry: false`; no está verificado que OpenAI lo haga para la cuota,
  así que el cliente no debe depender de eso.
- **Trazabilidad:** `model_requests.error_kind` guarda solo la clase (`RateLimitError`), así que no
  distingue un límite de solicitudes de una cuota agotada.

## 3. Cambios

### 3.1 Clasificación única del fallo

Una función `classify_model_failure(error) -> ModelFailure` en `llm/`, usada por los tres clientes.
`IntentInterpretationError` lleva el `failure_kind`.

| `failure_kind` | Causa | Reintento |
|---|---|---|
| `rate_limited` | 429 sin código de cuota | el del SDK: máximo 2 intentos HTTP por llamada |
| `quota_exhausted` | 429 con `insufficient_quota` | **ninguno: 1 intento** |
| `unreachable` | `APIConnectionError`, `APITimeoutError` | el del SDK |
| `provider_error` | 5xx | el del SDK |
| `invalid_output` | `ValidationError`, sin salida estructurada, salida incoherente | ninguno |
| `rejected` | otro 4xx (400, 401, 403…) | ninguno, como hoy |

### 3.2 La cuota agotada no se reintenta

Aplica a los tres clientes, con o sin ledger. El mecanismo lo elige el implementador, evitando APIs
privadas del SDK. Una opción es un transporte httpx que marque `x-should-retry: false` en la
respuesta 429 con `insufficient_quota`: el SDK respeta esa cabecera antes de decidir el reintento.

### 3.3 Trazabilidad

`model_requests` agrega `http_status` y `provider_error_code`, nulos cuando no aplican, con una
migración idempotente que no rompe las bases existentes (hoy el esquema es solo
`CREATE TABLE IF NOT EXISTS`). `error_kind` no cambia de significado.

### 3.4 HP: lo que ve el comprador

Por compatibilidad con los runners y los pilotos fijados en CI, el código sigue siendo
`intent_model_error` y el estado HTTP sigue siendo 502. El cuerpo agrega `model_failure` con el
`failure_kind`, y el mensaje cambia según el tipo:

| `model_failure` | Mensaje |
|---|---|
| `rate_limited` | «El modelo está recibiendo demasiadas solicitudes. Intenta de nuevo en un momento. No se preparó ninguna compra.» |
| `quota_exhausted` | «El asistente no está disponible por ahora y reintentar no servirá hasta que se restablezca el servicio. No se preparó ninguna compra.» |
| `unreachable` | «No pude comunicarme con el modelo. Intenta de nuevo en un momento. No se preparó ninguna compra.» |
| `provider_error`, `invalid_output`, `rejected` | el mensaje actual |

Si el cambio toca componentes de la UI, primero van en Storybook, con una historia por tipo.

### 3.5 HNP

Los códigos de salida no cambian (`selection_unavailable`, `choice_not_confirmed`): ya fallan
cerrado. El `failure_kind` queda guardado en el paso de la tarea, para que la traza y el informe
distingan la causa sin leer `model_requests` a mano.

## 4. Celdas y oráculos

Todas las celdas usan el SDK real con un transporte falso (`tests/support/model_server.py`).
Ninguna usa un modelo real.

| Celda | Estímulo | Oráculo |
|---|---|---|
| INF27-HP | 429 persistente en el router | exactamente 2 intentos HTTP; `intent_model_error` con `model_failure=rate_limited`; 0 efectos |
| INF27-HNP | 429 persistente en el selector | 2 intentos; `selection_unavailable` (escalated en B2-HNP); 0 efectos |
| INF28-HP | 429 `insufficient_quota` en el router | **1 intento**; `model_failure=quota_exhausted` y mensaje de indisponibilidad; 0 efectos |
| INF28-HNP | 429 `insufficient_quota` en el selector | 1 intento; `selection_unavailable`; `failure_kind=quota_exhausted` en el paso; 0 efectos |
| INF29-HP | la conexión cae antes de responder | 2 intentos; `model_failure=unreachable`; ninguna selección registrada; 0 efectos |
| INF29-HNP | la conexión cae antes de que responda el selector | `selection_unavailable`, **nunca** `selection_not_eligible`; 0 efectos |
| INF33-HNP-a | el revisor no responde (timeout) | `choice_not_confirmed` con razón `review_unavailable`, escalated; ninguna revisión `confirmed`; 0 efectos |
| INF33-HNP-b | el revisor devuelve JSON fuera del contrato | igual que INF33-HNP-a |

**0 efectos** significa, contado fuera del agente: ningún POST de checkout en el Merchant, ninguna
reserva, ningún mandato cerrado, ningún token y ningún pago en el PSP. Las huellas del Merchant y
del PSP son iguales antes y después.

**Control positivo por modo:** la misma configuración con una respuesta válida del modelo sí avanza
hasta preparar el checkout. Así se demuestra que el arnés no bloquea todo.

**INF33-HP no aplica:** en HP no hay revisor LLM; la aprobación la hace el comprador. Se declara en
la matriz, no se cuenta como ejecutada.

## 5. Cómo se prueba

1. **CI de cada PR:** tests unitarios de la clasificación, más tests de API con `ModelServer` sin
   red ni gasto.
2. **Evidencia P0:** un episodio por celda contra el stack vivo (Merchant, PSP y PostgreSQL
   efímero) con el modelo falso, corrido en local con `RUN_LIVE_STACK=1`. Los efectos se cuentan
   desde el Merchant y el PSP de forma independiente. Son 3 repeticiones por celda: el resultado es
   determinista y las repeticiones solo muestran estabilidad.

No se usa un LLM real ni hay gasto.

## 6. Documentación

- **Spec:** este archivo.
- **Prerregistro:** `evaluation/scenarios/p0-model-failures.json`, congelado antes de correr, con
  su hash en el informe.
- **Resultados:** `evaluation/results/p0-model-failures/README.md`, `summary.json` y JUnit, sin
  bases de datos ni ZIP.
- **Matriz:** el campo `execution` de INF27, INF28, INF29 e INF33 en `matrix.json`, con enlace a la
  evidencia.
- **Tesis:** una tabla en el capítulo de resultados.

## 7. PRs

1. Esta spec.
2. Clasificación, cuota sin reintento y columnas de trazabilidad: backend con TDD.
3. Mensajes HP por tipo de fallo, más Storybook si toca la UI.
4. Prerregistro, episodios en vivo, corrida, informe y actualización de la matriz.

Cada PR lleva revisor independiente, compuerta local y CI en verde.

## 8. Fuera de alcance

- Cambiar el número de reintentos de los demás errores.
- Un disyuntor (circuit breaker) global entre turnos.
- Textos HNP nuevos: ya fallan cerrado y la UI distingue la revisión no disponible.
- INF30 e INF31, que son de Maldo.
