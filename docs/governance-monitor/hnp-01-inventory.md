# HNP-01: inventario de reutilización sin cherry-pick

Fuente inspeccionada: `Maldinho18/tesis-commerce-agent@a633a043337286406f9a3e859809556ce46bcba1`. El commit agrega 16 archivos sobre su base histórica `678ce5f7f3c3132afc3f1fa27a5e287f5af0df5d`. Se leyó desde los objetos Git existentes; no se cambió la rama HNP de Yair ni se ejecutó su programa.

| Componente | Reutilización y límites |
|---|---|
| models.py | Contratos estrictos `hnp-review/0.1`, digests separados, salida sin campos de autoridad. Candidato reutilizable para HNP-02; no equivale a PolicyDecision operativo |
| ports.py | BuyerAgent.propose y ValidatorAgent.review independientes, sin capacidad comercial. Buen punto de extensión posterior |
| adapters.py | Roles programados y comparación del validador; reutilizable como control determinista, nunca etiquetar como inferencia real |
| context.py | Fuente y resolver de fixtures, proyección de candidatos/delegación; no sustituye autoridad AP2 real o registro comercial |
| backend.py | Once comprobaciones deterministas experimentales. Mantenerlas separadas del validador y sin relajarlas; no son PDP POL-03–10 |
| coordinator.py | Control de un uso, evidencia previa al sink y revisión de versión en memoria; no es UoW/CAS SQL ni reserva durable |
| evidence.py | Cadena con separación de dominios, persistencia/readback y recibos ficticios; no firma ni almacenamiento inviolable |
| sink.py | Destino exclusivamente ficticio con permiso interno; no conectar a checkout/orden/pago |
| harness.py, evaluation.py | Corpus pareado A/B, oráculo separado y métricas sin atribuir al validador la contención backend. No ejecutados nuevamente aquí |
| __init__.py | Namespace aislado |
| scripts/run_hnp_review_demo.py | Runner offline que escribe solo bajo su .local; no se portó ni ejecutó |
| tests/hnp_experiment/fixtures/cases.json, oracle.json | 27 casos y expectativas separadas; candidatos a corpus/regresión futura, sin entregar oráculos a los roles |
| tests/hnp_experiment/test_review_harness.py | 115 parametrizaciones históricas reportadas: bindings, autoridad, redacción, permisos, fallos de evidencia/recibo y contención de accept forzado. Son pruebas reutilizables por revisar, no 115 tests ejecutados en este incremento |
| docs/hnp-experiment/ec-hnp-01-results.md | Corte histórico: 54 ejecuciones guionadas; cero LLM. Sus resultados no se suman a las pruebas nuevas de monitor |

## Compatibilidad comprobada y posibles conflictos

Las seis dependencias compartidas inspeccionadas (`domain/__init__.py`, `domain/intent.py`, `domain/selection.py`, `acp/feed.py`, `governance/models.py`, `governance/canonical.py`) son idénticas por SHA-256 entre HNP-01 y Agent organizacional 83510f8. La comparación exacta está en `hnp-shared-imports.json`. El namespace `hnp_experiment` no está incorporado en ese main. No se observó un conflicto directo en estos archivos; no es una validación transitiva completa ni una prueba de ejecución del paquete sobre main.

El cambio era aditivo, pero un cherry-pick automático no resolvería las diferencias semánticas: las fixtures HNP modelan consentimiento/revocación/uso sintéticos; D17/D2-HP ahora disponen de evidencia real distinta; `executed_policy_ids=[]` no debe convertirse en D1/D2/D3 ejecutado por integración de UI. El resolver usa un lock local, no una reserva transaccional. Un accept del validador sigue sin autorizar negocio.

Este incremento reutiliza **artefactos y contratos de lectura**, no importa/copía el runtime HNP a la aplicación actual. La prueba de contención se muestra desde el registro histórico real del programa offline: condición B / validator-false-accept, total 13000 frente a límite 12500, accept, BACKEND_DENIED/budget, cero efecto. Se verifican los 54 resultados contra la cadena 65; no se inventa una nueva ejecución.

Para Yair: revisar primero contrato de monitor, fuente y los 69 tests actuales del monitor (los 115 históricos HNP no se cuentan como pruebas nuevas); después decidir si portar manualmente el namespace aislado y sus 115 pruebas a un incremento HNP-02. Mantener checkout/PSP fuera de los procesos de inferencia y revisar la integración del entrypoint UI si hay trabajo paralelo. Lab #19 es otro repositorio/carril y no se usa como base de desarrollo.
