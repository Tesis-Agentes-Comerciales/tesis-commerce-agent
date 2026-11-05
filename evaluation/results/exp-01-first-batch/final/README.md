# Cierre EXP-01 — Primera ejecución científica parcial

**Resultado: MA24 HNP passed; INF30 HP passed; INF31 HP incompleto/BLOCKED por estímulo 503 nativo.**
La automatización funciona y conserva el fallo; el lote completo tiene validated=false.
No se evaluó comprensión semántica, no se aprobaron/completaron compras ni hubo LLM pagado.

## Ejecución primaria final

[Actions 41846571407](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41846571407),
commit `8e021a50aedee023357ac76c3db34403fe53cf4d`:
**8 collected, 7 passed, 1 failed, 0 errores pytest, 0 skipped/xfail/xpass**.
La aserción fallida sigue siendo `500 == 503`: no se quitó ni se volvió xfail.
El reportador publica automáticamente MA24 passed, INF30 passed, INF31 error,
attribution_status=verified, validated=false e issues=[]. El error aquí es que no se
alcanzó el estímulo nativo prerregistrado, no una falsedad comercial ni una aprobación.

Fuentes limpias verificadas: Agent del run anterior; base aprobada
`65183d545241937c5f741dd66378e1a7f7c83dc9`; Merchant
`abf30df3b6a611284dedcc26c00accac6a853329`; PSP
`e6ab67b5963434a9a3caae2012b70232a3cca65d`.
El prerregistro tiene los mismos bytes y oráculos desde e8b7050:
SHA-256 `86598a0132c75e9e72b404a6c9e53d45e1bb3ff22a27a1892c63f020ad831dfb`.

Artefacto `exp01-first-batch-evidence`, ID **13330598787**. ZIP original en
`../runs/41846571407.zip`, SHA-256
`eea1dfe3457eeabc59e8a43f3e3c075251930935a0621c23d1e6ecd4971ceac4`.
Los **50 hashes** del manifiesto original coinciden. El archivo summary.json ofrece
un resumen legible; el ZIP retiene el informe completo, inputs congelados, JUnit,
recorder, logs, catálogo completo, snapshots de efectos y sus hashes originales.

## Observaciones verificables

| Caso | Evidencia real | Alcance |
| --- | --- | --- |
| MA24 | Grant y tarea identificados, scheduled antes de revocar; grant revoked/owner/epoch=2 antes de trigger 409. Merchant/PSP permanecen idénticos tras revocación; cero outbox, reservas, recibos y purchases. | Integración HNP por API; no comprensión conversacional ni UI. |
| INF30 | POST real aceptado, respuesta perdida; Agent uncertain, clave e identidad persistentes; retry del mismo run bloqueado sin segundo POST, lookup histórico verifica el mismo checkout/digest y otro proceso inspecciona sin red. | Servicio de compra HP real después de una selección de entrada explícita; no toda la UX pública de recuperación. |
| INF31 | Pasan 503 controlado del proxy antes de reenviar, timeout antes de reenviar, timeout tras aceptación, rechazo definitivo out_of_stock y preparación positiva sin pago. | Cinco controles aprobados; el sexto no produjo el estímulo requerido. |

En el sexto control, detener PostgreSQL hace fallar `api.trusted_context` →
`context.authenticate_lab_session`, antes de preparar checkout. La psycopg.OperationalError
produce **HTTP 500**. Agent lo deja uncertain y no se crea sesión. No se llama a eso 503.
No se afirma que sea imposible obtener un 503 con otro mecanismo: este mecanismo concreto
no lo obtiene. El manejo de indisponibilidad de Merchant requiere revisión separada si
se decide que el 503 nativo es indispensable; no se implementó cambio alguno del proveedor.

Los ocho controles tienen cleanup_verified=true. Todos los checkouts preparados quedan
canceled según lectura independiente Merchant; el control de stock restaura el snapshot.
Se observan stock, run_events, sesiones, órdenes/intentos y tablas PSP sin publicar tokens.
Shutdown confirma proxy_closed=true y postgres_removed=true. No hay bases/claves privadas
ni patrones JWT/PEM/DSN en el ZIP. Ninguna orden ni pago se completó.

## Trazabilidad de todos los intentos

Los tres primeros ZIP y su análisis previo siguen intactos en la carpeta superior:
41841403584 (bloqueo shallow checkout), 41842063615 (hook JSON/teardown),
41842686257 (7 passed/1 failed; reportador todavía no atribuía avances ante exit 1).
La carpeta superior documenta sus SHAs y digests. No se borraron resultados desfavorables.

El intento adicional [41845792685](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41845792685),
fuente `3fb12393b1c421fe588ffc1fd2f9db9a529cf126`, también dio 7 passed/1 failed,
pero Python del sistema no tenía pydantic para redactar logs y el reportador falló.
Se conserva en `../runs/41845792685.zip`, artefacto 13331571130, SHA-256
`042b2c928120f83172061397209cd1d00ed071c3c668945bdac03f5baf9d7f66`.
La ejecución primaria final usa el intérprete instalado del lock y genera el informe
automáticamente. No se cambió el oráculo nativo 503 para conseguir verde.

## Inventario y decisiones pendientes

De las 37 unidades mínimas: **2 passed, 1 error de estímulo y 34 not_executed**.
No se cuentan réplicas ni controles como escenarios adicionales. INF30/31 en HNP,
la UX pública completa y toda la comprensión semántica siguen sin evaluarse.
La proyección del antiguo contador P0 colapsa error/blocked a not_executed y por eso
se publica aparte: no sustituye estos conteos enriquecidos.

El inventario ordenado de los otros 30 escenarios está en `docs/design/exp-01-remaining.md`.
Próximo lote propuesto: CT15 y CT14, sin pagos ni LLM; antes deben congelarse sus puntos de
mutación y oráculos. Los helpers que cambian varios atributos no se deben tratar como una
mutación pura de stock o precio sin comprobar y aislar esos cambios.

Aceptar el 503 de frontera como alcance futuro, diseñar un estímulo nativo distinto o revisar
el contrato Merchant son decisiones pendientes de revisión humana. No se aplica ninguna
retroactivamente a este lote. P3 y sus cinco celdas UX03 no se desbloquean por esta evidencia.
