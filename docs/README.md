# Documentación

Lo vigente describe el agente tal como está en `main`. Lo histórico con valor está en el
[archivo](archive/README.md). Los resultados de la tesis y cómo verificarlos están en
[`evaluation/README.md`](../evaluation/README.md).

## Vigente

| Documento | Qué es |
|---|---|
| [Contrato de la interfaz con el agente](agent-ui-contract.md) | El estado que el agente le envía a la interfaz web por Assistant Transport: endpoint, comandos, campos, el modo de compra delegada y los fixtures. |
| [Perfil de AP2](ap2-profile.md) | Cómo usa el agente AP2 v0.2 con el comprador presente y en la compra delegada: fronteras de confianza, mandatos, verificaciones y recibos. |
| [Compatibilidad y seguridad](compatibility.md) | Las versiones fijadas de ACP y del transporte, las fronteras de autoridad, los controles implementados y el criterio de fallo seguro. |
| [Activar la gobernanza](d1-enforce-runtime-optin.md) | Las variables que encienden la política de compra y el flujo gobernado con el comprador presente, y sus límites. Por defecto está apagada. |
| [Specs de diseño](design/README.md) | Una spec por cambio, rotulada como vigente, como respaldo de la tesis o como histórica. |
| [Diagramas](diagrams/README.md) | El contexto y los componentes en C4, las secuencias de la compra y su ciclo de vida. |
| [Estrategia de pruebas](testing.md) | Qué se prueba, dónde vive cada prueba, cuándo corre y cómo correrla en local. |
| [Flujo de trabajo](workflow.md) | Los pasos de cada cambio: spec, contrato, Storybook, pruebas, integración y PR. |

## Evidencia que se queda en su lugar

[`governance-monitor/`](governance-monitor/README.md) guarda la evidencia de gobernanza de octubre
que mostraba un monitor ya retirado. Es histórica, pero una prueba fija sus archivos por hash, así
que no se movió al archivo.

## Archivo

[`archive/`](archive/README.md) guarda el diseño de gobernanza de octubre con los reportes de sus
tickets, los diagramas de esa época, el guion de la demo de octubre, un corte de estado, el reporte
del prototipo de compra delegada y una auditoría de septiembre. Nada de eso describe el sistema
actual.
