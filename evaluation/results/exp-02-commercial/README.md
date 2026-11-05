# EXP-02 — CT15 HP y CT14 HP

Resultado comprobado: ambos escenarios pasan en el alcance técnico HP registrado.
No implica HNP, comprensión semántica ni completar compras. Producto base Agent 65183d5;
Merchant abf30df3b6a611284dedcc26c00accac6a853329; PSP e6ab67b5963434a9a3caae2012b70232a3cca65d.
PR #163 apilado explícitamente sobre EXP-01 #162; ninguno fusionado.

| Ejecución | SHA ejecutado | Resultado |
| --- | --- | --- |
| [41895136607](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41895136607) | 243fb5fcd334a6258e90de91ab8a44c35e072260 | CT15: 1 passed, 1 failed y 1 error teardown. Proxy del arnés bloqueaba cancel; no atribuir como defecto de producto. |
| [41895460657](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41895460657) | 01698e89ca3b8de5d5e5eb90f88cea4bad57e3c1 | CT15: 2 passed, cero failed/error/skipped; validated=true, 22 hashes correctos. |
| [41895755779](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41895755779) | 01698e89ca3b8de5d5e5eb90f88cea4bad57e3c1 | CT14: 2 passed, cero failed/error/skipped; validated=true, 18 hashes correctos. |

## Datos y oráculos independientes

CT15 observó iPhone 12 mini 64 GB en el episodio Merchant: ítem 38,000,000 centavos COP,
envío 2,500,000, total 40,500,000. La subida controlada aplica otro precio observado del
mismo catálogo, 42,000,000 al ítem; total autoritativo Merchant 44,500,000. No son valores
mercantiles reales de mercado. El feed original permanece idéntico y completo (310 ofertas).
Con máximo 44,500,000 el Agent prepara y señala price_changed; con máximo 40,500,000 crea
una vez y cancela por budget_exceeded. La respuesta HTTP Merchant determina el total.

CT14 prepara y presenta términos frescos; solo entonces agota stock/revisión. La relectura
rechaza checkout_terms_changed, con changed_fields=[stock]. Precio/nombre/envío/contexto/
expiración idénticos por comparación de todo el snapshot. El control estable mantiene
fresh_terms. No se intentó aprobar: alcance de revalidación preaprobación, no complete.

Protocolo evaluation/scenarios/exp02-commercial.json congelado en 243fb5f, sin alteraciones
después de observar. Solo se corrigió la ruta permitida del proxy para cancelación real.
Cada episodio guarda inputs/oráculo/configuración/feed/hashes antes del comportamiento.
El Agent recibe presupuesto e intención, nunca el oráculo o respuesta esperada.

## Efectos, integridad y conservación

Todos los checkouts creados se observan canceled después de limpieza; snapshots de ofertas
restaurados exactamente y PSP idéntico antes/después/limpieza. Sin aprobaciones, órdenes,
intentos de completar ni pagos nuevos. run_events y huellas de todas las tablas Merchant
son parte de la evidencia; no se afirma que offer.read no escriba auditoría.
Shutdown confirma proxy cerrado y PostgreSQL eliminado en cada ejecución. Los ZIP originales
incluyen JUnit, recorder, preflight de tres fuentes limpias, feed completo, congelación,
observaciones independientes, logs sanitizados y SHA256SUMS interno. summary.json conserva
los fallos y los resultados; SHA256SUMS de esta carpeta permite verificar los archivos sin
retención de Actions. No se modificó matriz/oráculo P0, producto, locks ni evidencia anterior.

Preparación conversacional: docs/design/exp-conversation-readiness.md. Sin llamadas pagadas;
UX01/11/12 propuestos, adaptación final aún pendiente de autorización y prerregistro.
