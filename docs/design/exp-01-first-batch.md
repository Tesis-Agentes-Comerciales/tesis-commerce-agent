# EXP-01: primer lote de integración experimental

Autorización: solicitud EXP-01 del 4 de noviembre de 2026. Base Agent
`65183d545241937c5f741dd66378e1a7f7c83dc9`; Merchant
`abf30df3b6a611284dedcc26c00accac6a853329`; PSP
`e6ab67b5963434a9a3caae2012b70232a3cca65d`.

Se reutilizan LiveStack, PurchaseLane, HnpAgent, JointCase, ACPClient, FaultProxy,
PurchaseService y el verificador de creación del producto. No depende de P2/P3.
No cambia producto, oráculos P0 ni evidencias previas. No hay LLM, aprobación HP,
completado de checkout ni pago. El PSP real del proyecto sirve de control de efectos cero.

## Prerregistro y correspondencia

El protocolo versionado `evaluation/scenarios/exp01-first-batch.json` y los hashes P0 se
congelan antes de ejecutar. Cada control exporta el catálogo completo del episodio Merchant,
registra la oferta real de entrada y congela su manifiesto antes de iniciar el Agent.
La variante fija el producto de entrada de una prueba de preparación; no representa una
decisión conversacional ni una alternativa ganadora. No se introduce NoEffectsGateway:
el puerto comercial HP es ACPClient real y el despacho es SingleAttemptCheckoutDispatch,
el mismo del runtime gobernado. Una continuación indebida sí podría dejar efectos y fallar.

| Caso | Estímulo | Observaciones/oráculo congelado |
| --- | --- | --- |
| MA24, HNP | Proponer/consentir, confirmar scheduled; revocar y confirmar revoked antes de liberar la barrera del trigger. | Trigger 409, cero POST comerciales, tablas Merchant y PSP iguales desde revocación efectiva, cero outbox/reservas/recibos; estado cancelado y sin compras. |
| INF30, HP | Merchant procesa POST real y responde 201 al proxy; drop pierde la respuesta antes del Agent. | Agent uncertain, un checkout y una clave; reintento del mismo run exige recuperación sin nuevo POST; verificador obtiene la misma identidad/digest; inspección en otro proceso no escribe ni reenvía. |
| INF31, HP | 503 del proxy antes de reenviar. | Sin aceptación Merchant, Agent uncertain; lookup not_found no autoriza ausencia ni repetición. |
| INF31, HP | Timeout antes de reenviar: barrera before_forward y 503 no reenviado liberado después del timeout. | Se demuestra la fase exacta; cero checkout, recuperación conservadora, sin duplicación. |
| INF31, HP | PostgreSQL desechable detenido durante el POST, Merchant devuelve 503 real. | Error aceptado como incierto; tras restaurar PG, cero creación y lookup no demuestra ausencia. |
| INF31, HP | Merchant acepta y el proxy demora la respuesta más que el timeout. | Incierto para Agent, existencia demostrada independientemente; recuperación histórica de la misma identidad, sin replay. |
| INF31, control negativo | Oferta agotada por mutación Merchant existente antes del POST. | Rechazo comercial definitivo out_of_stock y estado failed; no confundir con incertidumbre técnica. Se restaura el snapshot original. |
| INF31, control positivo | Preparación sin fallo. | Una sesión awaiting_confirmation, sin aprobación/pago; demuestra que el arnés no fuerza un bloqueo universal. |

Seis controles INF31 y dos casos adicionales: ocho tests, tres escenarios observados.
Cada control usa run/actor, SQLite y claves nuevos; solicitudes secuenciales y limpieza al final.
Los checkouts creados se cancelan y se consultan independientemente; se guardan contadores,
hashes de todas las tablas Merchant, stock y run_events. Del PSP se guardan hashes/conteos
de tablas, nunca tokens. Las dos bases PostgreSQL/SQLite son desechables.

## Límites de interpretación

El lote evalúa integración del producto, no comprensión semántica. MA24 cubre su modo HNP;
INF30/31 cubren HP: su modo HNP y la UX pública completa de recuperación quedan pendientes.
La recuperación histórica no habilita continuar ni comprar. El inventario de 37 unidades
mantiene sin ejecutar las otras 34, sin declarar completos todos los modos de INF30/31.
El defecto funcional se conserva como failed; error de arnés, bloqueo y ausencia de ejecución
se reportan por separado. No se adapta el oráculo a lo observado.

## CI y evidencia

Un perfil manual `exp01-first-batch` en nightly.yml reutiliza los mismos checkouts, uv y Docker.
El perfil full original excluye el módulo experimental: no ejecuta escenarios por accidente.
No se añaden switches a ci.yml ni variables de habilitación adicionales. EXP exige los dos
pines exactos y falla si falta acceso; no hereda el skip verde del nightly histórico.
EXP no necesita Node/Chromium porque usa la API HNP y el servicio de compra HP reales.

Antes de servicios: fuentes limpias, ascendencia aprobada, hashes de matriz/oráculo/lock,
dependencias HNP y ausencia de credenciales/flags LLM. Después: JUnit + recorder, ocho controles
exactos, manifiestos congelados, resultados por escenario y evidencias de limpieza. El workflow
intenta publicar incluso si un caso falla; pytest no usa fail-fast. Un job verde no basta para
declarar passed un escenario. Artefactos EXP separados del nightly y del piloto anterior.
