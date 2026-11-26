# Diagramas archivados

Diagramas que ya no describen el sistema. Los de gobernanza se hicieron para la sustentación de
octubre, con el estado de `main` antes de la compra delegada: marcan como futuras piezas que hoy
existen, varios muestran OpenAI o gpt-5-mini como modelo (hoy el agente usa Claude) y otros traen
los números del primer piloto de gobernanza, que la tesis reemplazó por el segundo. Los vigentes
están en [`docs/diagrams/`](../../diagrams/README.md).

Cada uno tiene su fuente `.drawio` y sus exportaciones `.svg` y `.png`. Los generaba
`scripts/tools/build_governance_diagrams.py`, que salió de `main` y sigue en la etiqueta
`pilotos-octubre`. Su convención visual está en
[`visual-style.md`](../b2-governance/visual-style.md).

| Diagrama | Qué mostraba |
|---|---|
| [`architecture-b0`](architecture-b0.png) | Los contenedores de la línea base, sin gobernanza. |
| [`architecture-b2-hp`](architecture-b2-hp.png) | Los mismos contenedores con cada control de la compra gobernada con el comprador presente. |
| [`b0-vs-b2-delta`](b0-vs-b2-delta.png) | Una diapositiva con los pasos de la compra y lo que la gobernanza agrega en cada uno. |
| [`purchase-sequence-governed`](purchase-sequence-governed.png) | La secuencia de la compra gobernada con el comprador presente y sus puntos de control. |
| [`purchase-sequence-slide`](purchase-sequence-slide.png) | La misma compra en una diapositiva, con las ramas que la detienen y números del primer piloto. |
| [`policy-map`](policy-map.png) | Las políticas y los controles de aplicación, con las de la compra delegada todavía como futuras. |
| [`architecture-hnp-experiment`](architecture-hnp-experiment.png) | El experimento offline de compra delegada con un validador, previo a la implementación real. |
| [`pilot-design`](pilot-design.png) | El diseño del primer piloto pareado y del experimento de compra delegada con validador. |
| [`pilot-results`](pilot-results.png) | Los resultados del primer piloto de gobernanza. |

## Versiones anteriores

En [`historico/`](historico/) quedan las vistas que ya se habían reemplazado antes de octubre.

| Diagrama | Qué era |
|---|---|
| [`agent-architecture-c4`](historico/agent-architecture-c4.png) | Una vista de contenedores anterior, retocada a mano. |
| [`checkout-protocol-sequence`](historico/checkout-protocol-sequence.png) | Una secuencia técnica anterior del checkout, con otra paleta. |
| [`b2-architecture-p1-p5`](historico/b2-architecture-p1-p5.png) | La propuesta original de gobernanza, con cinco puntos de decisión y OPA. No se implementó así. |
