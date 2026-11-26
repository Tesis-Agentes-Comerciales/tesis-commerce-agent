# Archivo

Documentos y diagramas que ya no describen el agente, pero que se guardan porque cuentan cómo se
llegó a él. No se reescriben: conservan sus fechas, sus rutas y el vocabulario de entonces. Lo
vigente está en el [índice de la documentación](../README.md).

| Carpeta o archivo | Qué guarda | Por qué se archivó |
|---|---|---|
| [`b2-governance/`](b2-governance/README.md) | El diseño de la gobernanza de octubre (investigación, arquitectura, contrato de políticas, amenazas y planes), los reportes de ejecución de sus tickets y la evidencia sellada de la validación con el comprador presente | La gobernanza se implementó más simple que este diseño. La guía para activarla sigue vigente en [`d1-enforce-runtime-optin.md`](../d1-enforce-runtime-optin.md) |
| [`diagrams/`](diagrams/README.md) | Los diagramas de gobernanza de octubre y versiones anteriores de los diagramas del agente | Son de antes de la compra delegada: marcan como futuras piezas que ya existen, nombran OpenAI o muestran el primer piloto |
| [`demo/`](demo/guion-2026-10-23.md) | El guion de la demo local de octubre | Corre el agente con OpenAI; hoy el agente usa Claude |
| [`status/`](status/README.md) | Un corte del estado del proyecto | Era una foto de un momento |
| [`hnp-experiment/`](hnp-experiment/ec-hnp-01-results.md) | El reporte del prototipo offline de compra delegada, guionado y sin modelo | Lo reemplazó la compra delegada real |
| [`agent-audit-2026-09-23.md`](agent-audit-2026-09-23.md) | Una auditoría del agente de septiembre | Describe el código de entonces |

## Rutas antiguas

Los documentos fechados, algunos datos como `evaluation/data/b2-design.json` y el texto de ayuda de
`demo-stack` citan estas carpetas por su ruta de antes, sin `archive/`. Los enlaces Markdown sí se
actualizaron.

## Lo que no está aquí

- La evidencia de gobernanza que mostraba el monitor retirado sigue en
  [`docs/governance-monitor/`](../governance-monitor/README.md): una prueba fija sus archivos por
  hash.
- Las specs históricas siguen en [`docs/design/`](../design/README.md), rotuladas, porque el código y
  la evidencia las citan por su ruta.
- Los resultados exploratorios de la evaluación siguen en `evaluation/results/`, salvo el piloto de
  la suite, que pasó a `evaluation/results/suite/archivo/`; los describe [`evaluation/README.md`](../../evaluation/README.md).
