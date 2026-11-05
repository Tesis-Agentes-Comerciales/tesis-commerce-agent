# P0 — Fallos del proveedor del modelo (INF27, INF28, INF29, INF33)

Estado: aprobada por Yair el 5-nov-2026; corregida tras la revisión del PR #172. Base: `main`
adbe120. Escenarios de la matriz `evaluation/scenarios/p0/matrix.json` que quedaron del lado de
Yair en el reparto con Maldo.

## 1. Objetivo

Un fallo del proveedor del LLM nunca produce una compra, no se reintenta cuando reintentar no sirve,
queda registrado con su causa y se le explica al comprador con la causa correcta.

## 2. Comportamiento actual (verificado en adbe120)

- **Clientes:** los tres clientes del modelo — router HP (`llm/conversation_router.py`), selector
  HNP (`llm/delegated_choice.py`) y revisor HNP (`llm/intent_review.py`) — crean el SDK con
  `max_retries=1` y convierten cualquier `OpenAIError` o `ValidationError` en
  `IntentInterpretationError`.
- **HP tiene dos superficies, y las dos pierden la causa:**
  - **REST** (`/conversation/turn`): responde 502 con
    `{"code": "intent_model_error", "message": str(error)}`. El mensaje es el texto interno en
    inglés, por ejemplo «The conversation model request failed» (`api.py:177-190`).
  - **Chat** (transporte del asistente): cierra el paso con error usando
    `error_message("intent_model_error")`, que es «No pude interpretar la solicitud con el modelo en
    este momento.», y termina la corrida `incomplete`; no devuelve 502
    (`assistant_transport.py:53,501-506`). El transporte solo recibe el código del error
    (`_error_code`), no la causa.
- **HNP, selector:** cualquier fallo termina `selection_unavailable` antes de cualquier efecto
  (escalated en B2-HNP, failed en B0-HNP; #154). El selector no tiene un paso propio en el ledger:
  solo el `task_finished` final.
- **HNP, revisor:** un fallo o una salida inválida da `REVIEW_UNAVAILABLE`
  (`not_confirmed` / `review_unavailable`), queda en el evento `choice_reviewed` y la tarea termina
  `choice_not_confirmed`, escalada. La UI ya muestra «la revisión no estuvo disponible».
- **Reintentos:** el SDK (openai 2.54.0, el de `uv.lock`) reintenta todo 429, también la cuota
  agotada (`insufficient_quota`). Hay un reintento inútil por llamada, o sea, dos intentos HTTP.
  Solo hay dos excepciones: que el servidor responda `x-should-retry: false`, o que mande un
  `Retry-After` mayor que el máximo del SDK. No está verificado que OpenAI mande alguna de las dos
  para la cuota, así que el cliente no debe depender de eso.
- **Trazabilidad:** `model_requests.error_kind` guarda solo la clase (`RateLimitError`), así que no
  distingue un límite de solicitudes de una cuota agotada.

## 3. Cambios

### 3.1 Clasificación única del fallo

Una función `classify_model_failure(error) -> ModelFailure` en `llm/`, usada por los tres clientes.
Acepta el error crudo del SDK o un `IntentInterpretationError`, mirando su `__cause__`.

`IntentInterpretationError` gana un atributo `failure_kind` **opcional**, `None` por defecto, porque
también se lanza por causas que no son del modelo (por ejemplo `application/conversation.py:803`).

| `failure_kind` | Causa | Reintento |
|---|---|---|
| `rate_limited` | 429 sin código de cuota | el del SDK: máximo 2 intentos HTTP por llamada |
| `quota_exhausted` | 429 con `error.code` o `error.type` igual a `insufficient_quota` | **ninguno: 1 intento** |
| `unreachable` | `APIConnectionError`, `APITimeoutError` | el del SDK |
| `provider_error` | 5xx, cualquier otro `OpenAIError` y el `RuntimeError` del stream sin `response.completed` | el del SDK |
| `invalid_output` | `ValidationError`, sin salida estructurada, salida incoherente o límite de herramientas | ninguno |
| `rejected` | 4xx que no son 429 (400, 401, 403…) | el del SDK, sin cambios (el SDK reintenta 408 y 409) |

### 3.2 La cuota agotada no se reintenta

Aplica a los tres clientes, con o sin ledger. El recorder entrega **siempre** un
`DefaultHttpxClient`; hoy, sin ledger, devuelve `None` y el SDK arma su propio cliente. A ese
cliente se le agrega un **hook de respuesta** httpx: si la respuesta es un 429 y su cuerpo trae
`insufficient_quota`, el hook pone `x-should-retry: false`, cabecera que el SDK respeta antes de
decidir el reintento. El hook nunca lanza.

No se usa un transporte envolvente, porque choca con el arnés de pruebas:
- `ModelServer.install` pasa su propio `transport=`, así que un `transport=` en producción daría un
  argumento duplicado o el mock lo reemplazaría sin ejecutar el mecanismo;
- `AttemptBudget.attach` exige que el transporte sea un `MockTransport`.

### 3.3 Trazabilidad

`model_requests` agrega `http_status` y `provider_error_code`, nulos cuando no aplican. La migración
es idempotente: en `initialize()` lee `PRAGMA table_info` y hace `ADD COLUMN` si falta. Es
obligatoria una prueba sobre una base con el esquema viejo, porque `record()` inserta todos los
campos y, sin la columna, la fila se perdería en silencio. `error_kind` no cambia de significado.

### 3.4 HP: REST y chat

- **REST:** el código sigue siendo `intent_model_error` y el estado HTTP sigue siendo 502, por
  compatibilidad con los runners y los pilotos, que solo miran código y estado. El cuerpo agrega
  `model_failure` con el `failure_kind`. El `message` se queda como está, porque REST lo consumen
  los runners y no el comprador. La única prueba que compara el cuerpo exacto
  (`tests/test_governed_recovery.py:789`) se actualiza. Todo `intent_model_error` lleva la clave:
  vale `null` cuando el error no vino del modelo (por ejemplo, sin modelo configurado), así un
  runner lo distingue de un fallo del modelo y de un agente anterior a este cambio.
- **Chat**, que es lo que ve el comprador: el transporte recibe el `failure_kind`, no solo el
  código, y `fail()` muestra el mensaje según el tipo:

| `failure_kind` | Mensaje en el chat |
|---|---|
| `rate_limited` | «El modelo está recibiendo demasiadas solicitudes. Intenta de nuevo en un momento. No se preparó ninguna compra.» |
| `quota_exhausted` | «El asistente no está disponible por ahora y reintentar no servirá hasta que se restablezca el servicio. No se preparó ninguna compra.» |
| `unreachable` | «No pude comunicarme con el modelo. Intenta de nuevo en un momento. No se preparó ninguna compra.» |
| otro o `None` | el mensaje actual |

Si el cambio toca componentes de la UI, primero van en Storybook, con una historia por tipo.

### 3.5 HNP

Los códigos de salida no cambian (`selection_unavailable`, `choice_not_confirmed`): ya fallan
cerrado. El `failure_kind` se guarda sin crear tipos de evento nuevos, porque eso exigiría migrar
los CHECK del ledger:
- **selector:** un campo opcional en `TaskFinish`, que queda en el evento `task_finished`;
- **revisor:** un campo en el cuerpo de `choice_reviewed` cuando la revisión no estuvo disponible.

En los dos casos la clave `failure_kind` solo aparece cuando el modelo fue la causa: el tipo que
puso el cliente o, si el error es crudo del SDK, el de `classify_model_failure`. Un selector o un
revisor que falla por otra causa no la escribe, y los demás eventos conservan su cuerpo byte a byte.

Los TypedDict de la traza ignoran las claves extra, así que la UI no se rompe. La traza no expone
el campo: su contrato rechaza campos extra y exponerlo exige cambiar el contrato de la UI.

## 4. Celdas y oráculos

Todas las celdas usan el SDK real con un transporte falso (`tests/support/model_server.py`).
Ninguna usa un modelo real. **El 429 falso no trae `x-should-retry`**; de lo contrario, la prueba
de la cuota pasaría sin probar nada.

| Celda | Estímulo | Oráculo |
|---|---|---|
| INF27-HP | 429 persistente en la interpretación | exactamente 2 intentos HTTP; REST `model_failure=rate_limited` y el mensaje de `rate_limited` en el chat; 0 efectos |
| INF27-HP-control | un 429 y después respuesta válida | el reintento se recupera y se prepara **un solo** checkout |
| INF27-HNP | 429 persistente en el selector | 2 intentos; `selection_unavailable` (escalated en B2-HNP); `failure_kind=rate_limited`; 0 efectos |
| INF28-HP | 429 `insufficient_quota` en la interpretación | **1 intento**; REST `model_failure=quota_exhausted` y el mensaje de indisponibilidad en el chat; 0 efectos |
| INF28-HNP | 429 `insufficient_quota` en el selector | 1 intento; `selection_unavailable`; `failure_kind=quota_exhausted`; 0 efectos |
| INF29-HP | en B2-HP, la interpretación responde bien y la conexión cae en la **selección** (`choose_candidate`) | 2 intentos; `model_failure=unreachable`; sin explicación gobernada ni `invalid_candidate`; ninguna selección registrada; 0 efectos |
| INF29-HNP | la conexión cae antes de que responda el selector | 2 intentos; `selection_unavailable`, **nunca** `selection_not_eligible`; `failure_kind=unreachable`; 0 efectos |
| INF33-HNP-a | el revisor no responde (timeout) | `choice_not_confirmed` con razón `review_unavailable`, escalated; `failure_kind=unreachable` en la revisión; ninguna revisión `confirmed`; 0 efectos |
| INF33-HNP-b | el revisor devuelve JSON fuera del contrato | igual que INF33-HNP-a, con `failure_kind=invalid_output` |

**0 efectos** tiene dos partes:
- **fuera del agente:** ningún POST de checkout ni sesión nueva en el Merchant, ningún token ni
  pago en el PSP, y las huellas del Merchant y del PSP iguales antes y después;
- **en el ledger del agente:** ninguna reserva, ningún mandato cerrado y ninguna compra que avance
  más allá del fallo.

**Control positivo por modo:** la misma configuración con una respuesta válida del modelo sí avanza
hasta preparar el checkout. Así se demuestra que el arnés no bloquea todo.

**Fuera de la matriz, pero obligatorio:** una prueba unitaria o de API de la cuota en el revisor,
porque la §3.2 aplica a los tres clientes.

**INF33-HP no aplica:** en HP no hay revisor LLM; la aprobación la hace el comprador. Se declara en
la matriz, no se cuenta como ejecutada.

## 5. Cómo se prueba

1. **CI de cada PR:** tests unitarios de la clasificación, más tests de API con `ModelServer` sin
   red ni gasto.
2. **Evidencia P0:** un episodio por celda contra el stack vivo (Merchant, PSP y PostgreSQL
   efímero) con el modelo falso, corrido en local con `RUN_LIVE_STACK=1`. Los efectos se cuentan
   desde el Merchant y el PSP de forma independiente. Son 3 repeticiones por celda: el resultado es
   determinista y las repeticiones solo muestran estabilidad.
3. **Pieza de arnés que falta para HNP:** `HnpAgent` corre en otro proceso, exige que no haya
   variables `OPENAI_*` y fija el selector determinista (`tests/integration/live_purchase.py`).
   Como `ModelServer` solo parcha el proceso del test, no le llega. El PR 4 agrega una de dos
   piezas:
   - un agente HNP en el mismo proceso, con `AgentServer` y `ExplicitSettings`, como en
     `live_browser.py` y `test_exp06_hp_infrastructure.py`;
   - o un modelo falso por HTTP, vía `OPENAI_BASE_URL`.

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
2. Clasificación (§3.1), cuota sin reintento (§3.2) y columnas de trazabilidad (§3.3): backend con
   TDD.
3. REST y chat de HP por tipo de fallo (§3.4) y `failure_kind` en los eventos HNP (§3.5).
4. Pieza de arnés HNP, prerregistro, episodios en vivo, corrida, informe y actualización de la
   matriz.

Cada PR lleva revisor independiente, compuerta local y CI en verde.

## 8. Fuera de alcance

- Cambiar el número de reintentos de los demás errores.
- Un disyuntor (circuit breaker) global entre turnos.
- Textos HNP nuevos: ya fallan cerrado y la UI distingue la revisión no disponible.
- INF30 e INF31, que son de Maldo.
