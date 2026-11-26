# Specs de diseño

Cada cambio no trivial empieza con una spec aprobada en esta carpeta
([flujo de trabajo](../workflow.md)). Ninguna se mueve: el código, las pruebas y la evidencia las citan por su ruta. Cada una lleva un
rótulo:

- **Respalda la tesis:** el diseño de un resultado que la tesis reporta.
- **Vigente:** describe una parte del agente tal como está en `main`.
- **Histórico:** un experimento exploratorio, una auditoría o un protocolo que ya cumplió su papel.

Algunas están fijadas por hash o enlazadas desde evidencia fijada, así que su texto no se toca,
aunque su línea de estado haya quedado vieja. La tabla lo indica.

## Respaldan la tesis

Las tres de los pilotos son pre-registros: se fijaron antes de correr y por eso su estado dice que
no se han corrido. Los pilotos corrieron con gpt-5-mini; la suite, con Claude. Los resultados y cómo
verificarlos están en [`evaluation/README.md`](../../evaluation/README.md).

| Spec | Qué diseña |
|---|---|
| [`eval-suite.md`](eval-suite.md) | La suite de evaluación sobre Claude: el catálogo de tareas, las suites de capacidad, manipulación y regresión, los calificadores, el juez y su calibración. Capítulos 6 y 7. |
| [`b2-governed-pilot-v2.md`](b2-governed-pilot-v2.md) | El segundo piloto de gobernanza con el comprador presente, el de 79 de 80 frente a 32 de 80. Capítulo 6. |
| [`hnp-governed.md`](hnp-governed.md) | La compra delegada gobernada: el permiso previo, las políticas, el ejecutor, la evidencia y la traza. Es también el diseño vigente de esa parte del agente. Capítulo 6. |
| [`hnp-pilot.md`](hnp-pilot.md) | El piloto de la compra delegada. Capítulo 6. |
| [`hnp-review-pilot.md`](hnp-review-pilot.md) | El piloto del revisor de intención en la compra delegada. Capítulo 6. |

## Vigentes

| Spec | Qué diseña |
|---|---|
| [`hnp-chat-delegate.md`](hnp-chat-delegate.md) | La compra delegada dentro del chat: el modo «Delegar» y su formulario. |
| [`hnp-intent-review.md`](hnp-intent-review.md) | El revisor de intención: un segundo modelo revisa la elección en la compra delegada, detrás de un interruptor apagado por defecto. |
| [`hp-p1-budget-d1.md`](hp-p1-budget-d1.md) | La regla de presupuesto total en la decisión de política previa al checkout. |
| [`hp-p2-evidence-chain.md`](hp-p2-evidence-chain.md) | La cadena de evidencia entre decisión, consentimiento y efecto, y su verificador `verify-evidence`. |
| [`hp-p3-d2-authorization.md`](hp-p3-d2-authorization.md) | La autorización verificada en el momento en que el comprador aprueba. |
| [`hp-p5a-definitive-failures.md`](hp-p5a-definitive-failures.md) | Los rechazos definitivos del comercio al crear el checkout y los checkouts vencidos. |
| [`hp-p5b-selection-shortlist.md`](hp-p5b-selection-shortlist.md) | La elección limitada a la lista corta y el número de orden visible. |
| [`hp-shipping-before-choice.md`](hp-shipping-before-choice.md) | El envío de cada candidato, visible para el modelo antes de elegir, y los datos de entrega guardados. |
| [`p0-conditional-intent.md`](p0-conditional-intent.md) | La intención condicional, como «A si el total no pasa de X; si no, B». |
| [`p0-model-failures.md`](p0-model-failures.md) | Qué hace el agente cuando falla el proveedor del modelo. Fijada por hash. |
| [`p0-ux08-ux10.md`](p0-ux08-ux10.md) | El cambio de producto, el desistimiento y la cantidad. Fijada por hash: su estado dice «propuesta», pero está implementada. |
| [`run-traceability.md`](run-traceability.md) | La trazabilidad de cada corrida: lo que el agente decide y lo que le pide al modelo queda registrado. |
| [`trace-panel.md`](trace-panel.md) | El panel de traza y gobernanza de cada compra en la interfaz. |

## Históricas

| Spec | Qué fue |
|---|---|
| [`b2-governed-pilot.md`](b2-governed-pilot.md) | El primer piloto de gobernanza con el comprador presente, exploratorio; lo reemplazó el segundo. El código la sigue citando por los controles del flujo gobernado. |
| [`injection-pilot.md`](injection-pilot.md) | El borrador de un piloto de inyección de instrucciones que no se corrió. Sus escenarios pasaron a los casos de manipulación de la suite. |
| [`p0-llm-batch2.md`](p0-llm-batch2.md) | El segundo lote exploratorio con un LLM real (gpt-5-mini). Fijada por hash. |
| [`main-03-nightly-baseline.md`](main-03-nightly-baseline.md) | La línea base reproducible de las pruebas nocturnas. |
| [`ui-01-browser-journeys.md`](ui-01-browser-journeys.md) | El diagnóstico de los recorridos de navegador que fallaban. |

### Campaña de escenarios P0

La campaña de pruebas sobre los 33 escenarios de la matriz
[`scenarios/p0/matrix.json`](../../evaluation/scenarios/p0/matrix.json) corrió contra el comercio y
el proveedor de pagos reales, casi siempre sin modelo. Su evidencia está en
`evaluation/results/`, y varios de sus README, fijados por hash, enlazan estas specs.

| Spec | Qué fue |
|---|---|
| [`exp-01-first-batch.md`](exp-01-first-batch.md) | El primer lote de integración. |
| [`exp-01-remaining.md`](exp-01-remaining.md) | El inventario de los escenarios que quedaban después del primer lote. |
| [`exp-02-commercial-controls.md`](exp-02-commercial-controls.md) | Los controles comerciales: precio que cambia antes y después de preparar la compra. |
| [`exp-02-repository-audit.md`](exp-02-repository-audit.md) | La revisión de los tres repositorios antes de ese lote. |
| [`exp-03-stock-shipping.md`](exp-03-stock-shipping.md) | Stock agotado en la compra delegada y envío con el comprador presente. |
| [`exp-04-mandate-replacement.md`](exp-04-mandate-replacement.md) | Revocar un permiso y dar uno nuevo. |
| [`exp-05-authenticated-ux.md`](exp-05-authenticated-ux.md) | El adaptador autenticado para los escenarios conversacionales. |
| [`exp-05-ct15-hnp.md`](exp-05-ct15-hnp.md) | El cambio de precio en la compra delegada. |
| [`exp-05-ct19-feasibility.md`](exp-05-ct19-feasibility.md) | Si el comercio podía representar una variante que desaparece. |
| [`ct19-hp.md`](ct19-hp.md) | La variante que desaparece, con el comprador presente. |
| [`exp-conversation-readiness.md`](exp-conversation-readiness.md) | La preparación de los escenarios conversacionales. |
| [`exp-06-semantic-hp.md`](exp-06-semantic-hp.md) | Los escenarios conversacionales con un LLM real (gpt-5-mini). |
| [`exp-06-protected-smoke.md`](exp-06-protected-smoke.md) | El ejecutor manual protegido de esos escenarios. |

### Protocolo de la intención condicional

El protocolo de evaluación de la intención condicional, previo a su implementación. La evidencia
de la campaña y una prueba citan el protocolo.

| Spec | Qué fue |
|---|---|
| [`p3-ux03-protocol.md`](p3-ux03-protocol.md) | El protocolo técnico. |
| [`p3-ux03-dependencies.md`](p3-ux03-dependencies.md) | Sus dependencias. |
| [`p3-ux03-validation.md`](p3-ux03-validation.md) | La puerta de aceptación y las pruebas mínimas. |
| [`p3-point5-validation.md`](p3-point5-validation.md) | La validación del protocolo. |
| [`p3-v01-offer-checkout-validation.md`](p3-v01-offer-checkout-validation.md) | La validación del contrato entre oferta y checkout. |
