# EXP-03 — CT13 HNP y CT16 HP

Resultados reales del Agent sin LLM ni pagos/completados nuevos. Código ejecutado
b76ff7074d83a96a76aac22c5148f0fe226512ef; base de producto 65183d5, pines Merchant
abf30df3b6a611284dedcc26c00accac6a853329 y PSP e6ab67b5963434a9a3caae2012b70232a3cca65d.
Fuentes limpias comprobadas por preflight. Protocolo exp03-stock-shipping.json sin cambios
posteriores. PR #164 en borrador apilado sobre #163; no fusiones.

| Run | Resultado verificado | Integridad |
| --- | --- | --- |
| [41897313836](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41897313836) | CT13 HNP B2-HNP: 1 passed, 0 failed/error/skipped, validated=true | 15 hashes correctos |
| [41897599243](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41897599243) | CT16 HP: 2 passed, 0 failed/error/skipped, validated=true | 20 hashes correctos |

CT13 consiente un grant de una variante existente con presupuesto derivado del total
Merchant. Tras scheduled y antes del trigger agota solo stock/revisión. Merchant responde
409 out_of_stock; la tarea termina failed/checkout_failed sin checkout aceptado, outbox,
reserva, mandato cerrado, token, orden o pago. La causa comercial se conserva independientemente
del mensaje genérico de UI. No se probó fallback autorizado a otra variante ni semántica.
El proxy impone adicionalmente techo de efectos: cualquier intento inesperado de JWT cerrado,
pago o complete quedaría bloqueado y haría fallar el test; no hubo esos intentos y no se
utiliza una respuesta preparada para declarar abstención.

CT16 muestra términos de un checkout real y luego aumenta exclusivamente envío/total usando
el importe de envío Merchant como delta controlado. El feed completo reexportado es idéntico;
subtotal, stock, revisión, identidad, fechas y contexto no cambian por comparación de snapshot.
El Agent rechaza la relectura con checkout_terms_changed y registra shipping_minor/total_minor.
Control estable devuelve fresh_terms. Alcance parcial del escenario: revalidación preaprobación,
NO obtención de nuevos términos, reaprobación o rechazo de complete.

Ambos runs preservan inputs/oráculos congelados, catálogo completo, barreras temporales,
identidad de grant/tarea, JUnit, recorder, snapshots/huellas de todas las tablas Merchant,
run_events, stock y estado PSP independientes. Todos los checkouts creados están canceled,
ofertas restauradas exactamente y PSP idéntico antes/después/limpieza. Shutdown proxy_closed
true y postgres_removed true. Los ZIP originales están versionados y SHA256SUMS verifica su
integridad sin depender de retención Actions. No se cambia evidencia de EXP-01 ni EXP-02.

No modifica src/commerce_agent/, Merchant/PSP, P0/matriz/oráculos/locks ni escenarios de Jair.
La capacidad conversacional y la integración P3 condicional siguen pendientes.
