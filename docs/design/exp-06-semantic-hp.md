# EXP-06 — preparación conversacional HP

Estado: infraestructura por validar; UX01/UX11/UX12 **no ejecutados** con modelo real.

Actualización 2026-11-05: el smoke autorizado corrió una vez, en local (macOS), con
`gpt-5-mini-2025-08-07` y este prerregistro sin cambios. Los tres casos quedan `pending_review`
hasta la revisión humana doble. Evidencia y costo: `evaluation/results/exp06-smoke/`.
El mismo día corrió el lote (5 repeticiones, 15 episodios) con `--stage batch`, también sin
cambiar el prerregistro. Los 15 quedan `pending_review`. Evidencia y costo:
`evaluation/results/exp06-batch/`.
Ese mismo día, la enmienda A hizo oficial la revisión por agentes de los 18 episodios
(`agent_reviewed`). En el lote, UX01 queda aprobado (5/5) y UX11 y UX12 no (3/5 y 1/5):
`evaluation/results/exp06-review/`.
Base: `eb18775e5b620436d949a82803f5415ec7455bf3`, CI posterior al merge
[41940776855](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41940776855),
cuatro jobs satisfactorios. No se modifican producto, pines hermanos ni evidencia anterior.

## Decisión y contratos

Se conservan `conversation.py`, `AuthClient`, JWT/JWKS, `create_app`,
`OpenAIConversationRouter`, `ModelRequestRecorder`, `LiveStack`, `PurchaseLane`,
`FaultProxy` y el reporte EXP-01. No se añade otro cliente de modelos ni un servicio.
La prueba de infraestructura instala el `ModelServer` existente con dos errores 503:
el router y el SDK reales realizan su intento y retry, sin respuestas conversacionales
guionadas. Un éxito de este control no acredita ningún escenario P0.

El router no configura `max_output_tokens`. Por ello, estimar sólo una respuesta corta
no garantiza un límite efectivo. Se añade únicamente al código de evaluación una
reserva previa a cada intento HTTP del cliente que ya proporciona el recorder. Se
rechazan modelo, endpoint o herramientas distintos; cada retry consume una reserva.
Una respuesta perdida, un timeout o uso desconocido conservan la reserva completa.
Se reutiliza `TokenPrices`; no se cuentan dos veces los tokens de razonamiento.
La infraestructura exige `MockTransport` antes de instalar los hooks: no puede enviar
solicitudes reales. No existe un comando de ejecución pagada en este incremento.

## Congelación y evaluación

`evaluation/scenarios/exp06-hp.json` fija las tres entradas, acciones admisibles,
rúbrica, orden, una repetición por escenario para smoke y cinco para el lote posterior.
Cada episodio futuro debe congelar el catálogo completo y sus metadatos antes del
turno, junto con contexto, protocolo, SHAs, configuración y hashes. No se preselecciona
variante ni se transmite la rúbrica al modelo. El vocabulario y `lookup_catalog` son
los del router real; el ledger permite distinguir qué recibió efectivamente el modelo.

Se utiliza un contexto temporal explícito reproducible: 4 de noviembre de 2026,
19:31:24 America/Bogota; viernes 6 de noviembre. Es una fecha de referencia del
experimento, no una garantía de entrega. El reloj sintético comercial del laboratorio
se registra por separado. Cambiar esta entrada exige una nueva versión antes de ejecutar.

La acción estructurada se comprueba automáticamente, pero una respuesta admisible
queda `pending_review`: dos revisores independientes califican cada criterio con
`pass`, `fail` o `unknown`, citando el texto; desacuerdos requieren adjudicación.
Un error técnico queda `error`, nunca falsedad comercial. Se observan independientemente
sesiones, órdenes, intentos, tokens y todas las tablas PSP mediante el arnés existente;
se comprueban también compras, aprobaciones, reservas y outbox locales. La auditoría
`offer.read` se conserva aunque no haya checkout. Cero órdenes por sí solo no basta.

## Modelo, coste y autorización pendiente

La [ficha oficial](https://developers.openai.com/api/docs/models/gpt-5-mini) documenta
Responses, structured outputs, contexto de 400.000 y salida máxima de 128.000 tokens,
con USD 0,25/M de entrada y USD 2/M de salida. La
[deprecación oficial](https://developers.openai.com/api/docs/deprecations) anuncia
retirada el 11 de diciembre de 2026. El acceso de esta cuenta no se ha probado.
No se sustituye `gpt-5-mini-2025-08-07` por un alias u otro modelo.

La reserva conservadora por intento es USD 0,445: máximo documentado de entrada y
salida, entrada en caché al precio completo y margen del 25%. No es una predicción del
consumo habitual. Con seis intentos máximos por episodio y ejecución secuencial:

| Etapa propuesta | Episodios | Intentos máximos | Reserva máxima | Tope propuesto |
| --- | ---: | ---: | ---: | ---: |
| Smoke | 3 | 18 | USD 8,01 | USD 9 |
| Lote posterior | 15 | 90 | USD 40,05 | USD 45 |

Ningún importe está autorizado. Antes del smoke se requiere aceptación explícita del
snapshot, etapa, hash del protocolo y máximo USD. Hay que revalidar precio y vigencia,
comprobar acceso durante la ejecución autorizada, instalar la guarda en todos los
clientes del recorder y archivar cada intento y su conciliación con `model_requests`.
La guarda detiene solicitudes nuevas antes de exceder el tope; el coste de una
solicitud de uso desconocido no se considera cero. Límites de gasto de la cuenta son
una defensa adicional, no sustituyen esta reserva local.

## Enmiendas

El prerregistro no tiene un campo de enmiendas, y su hash está anclado en el ejecutor. Por eso las
enmiendas se registran fuera de él, con fecha y motivo.

- **A (2026-11-05, después de las corridas; decisión de Yair).** Las dos revisiones humanas de la
  rúbrica pasan a ser dos calificadores agentes ciegos, `agent:grader-1` (Claude Opus) y
  `agent:grader-2` (Claude Sonnet), más un adjudicador agente, `agent:adjudicator` (Claude Opus).
  Las calificaciones llevan `grader_kind=agent` y el estado es `agent_reviewed`. La revisión es
  la oficial. Ninguna calificación es de una persona. Motivo, citas, regla de agregación y
  límites: [`amendment-A-agent-review.md`](../../evaluation/results/exp06-review/amendment-A-agent-review.md).

## Puertas y límites

Primero: tests offline de bloqueo, retries, conciliación y clasificación; después,
perfil `exp06-hp-infrastructure` del nightly con Merchant/PSP fijados y PostgreSQL
desechable. CI no recibe claves de modelo y el preflight rechaza credenciales reales.
Se archivan JUnit, manifest, hashes, catálogo, ledger sanitizado y limpieza. Una prueba
omitida o evidencia faltante bloquea el control. El scope full no incorpora este control.

La infraestructura no prueba disponibilidad efectiva, comprensión semántica ni
comportamiento HNP. El ejecutor pagado completo y la revisión humana siguen pendientes
de autorización; este PR prepara los contratos y adaptadores necesarios, no habilita
consumo automático. CT19 y el trabajo INF de Jair siguen separados; P3 no se desbloquea.
