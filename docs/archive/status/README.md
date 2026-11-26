# Estado y seguimiento documental del proyecto

Este directorio reúne **cortes fechados** para que los integrantes puedan conocer el estado técnico de la tesis sin depender de Notion ni de un computador particular.

## Corte vigente

- [3 de noviembre de 2026 — estado de pilotos, P1–P3, CI-PSP-01 y próximos pasos](2026-11-03.md).

## Cómo utilizarlo

1. El corte resume **qué ocurrió, con qué SHA y bajo qué alcance**. Enlaces a PRs, ejecuciones y artefactos son la evidencia principal.
2. Los resultados y los oráculos originales pertenecen a [evaluation/](../../../evaluation/); **no** se reescriben a partir de un resumen.
3. Los contratos, especificaciones y propuestas técnicas pertenecen a [docs/design/](../../design/). Un protocolo pendiente de revisión no se registra como aprobado.
4. Los comandos y límites de CI viven en [docs/testing.md](../../testing.md), y el flujo de revisión en [docs/workflow.md](../../workflow.md).
5. Si un corte posterior corrige un dato o decisión, añade una fecha y un enlace; no borres el corte anterior ni ocultes fallos y omisiones.

**Estado de un escenario ≠ estado del CI:** las ejecuciones unitarias y de integración pueden acreditar capacidades previas, pero una celda nueva solo pasa a ejecutada cuando su propio protocolo, entradas, oráculo y evidencia están completos.

La publicación y cualquier modificación de estos resúmenes deben realizarse mediante PR en la organización y revisión humana. La documentación puede publicarse antes de su merge en main, pero debe identificarse como contenido en revisión.
