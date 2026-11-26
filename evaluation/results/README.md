# Resultados

Los resultados que respaldan la tesis están arriba; los exploratorios, abajo. Nada de aquí se
edita: una desviación es evidencia, no un error que se borre o se vuelva a correr en silencio. Cómo
verificar cada resultado sin llamar a un LLM está en [`../README.md`](../README.md).

Los corredores de los pilotos de octubre salieron de `main`: su código, y los comandos que nombran
estos documentos, se reproducen desde la etiqueta `pilotos-octubre`. Los README de cada corrida no
se editan: son evidencia archivada, y varios están en su `SHA256SUMS`. El artefacto completo de
cada corrida de los pilotos (bases del agente, feed, log y `MANIFEST.json`) nunca se guardó aquí:
queda en `.local/runs/`, fuera de Git, porque lleva JWT y mandatos del sandbox.

## Respaldan la tesis

| Resultado | Dónde |
| --- | --- |
| La línea base (capítulo 4) | `b0-pilot-v1.jsonl` con sus 25 ejecuciones, `b0-pilot-v1.summary.json` y el informe `b0-pilot-v1-report.md`, con las figuras que muestra en `figures/`. |
| La gobernanza con el comprador presente (capítulo 6) | `b2-pilot-v2/` y el informe `b2-pilot-v2-report.md`. La carpeta tiene el lote 1, el lote 2 (una invocación por escenario) y la combinación que cuenta, con `SHA256SUMS`. |
| La compra delegada (capítulo 6) | `hnp-pilot-v1/` y el informe `hnp-pilot-v1-report.md`. La carpeta tiene la corrida registrada, una réplica completa que se reporta aparte y una prueba de humo que no cuenta, con `SHA256SUMS`. |
| El revisor de intención (capítulo 6) | `hnp-review-pilot-v1/` y el informe `hnp-review-pilot-v1-report.md`. La carpeta tiene la corrida registrada y una prueba de humo que no cuenta, con `SHA256SUMS`. |
| La suite sobre Claude: manipulación y capacidad (capítulos 6 y 7) | [`suite/`](suite/README.md), con su propio README. |

Lo que conviene saber al leerlos:

- En `b0-pilot-v1.summary.json` el campo `output` se reescribió a mano en el PR #24: el corredor
  había guardado la ruta absoluta del JSONL en su máquina y ahora es relativa a la raíz. Ningún
  otro campo cambió y el JSONL no se tocó.
- El informe de la línea base cita un cuaderno de análisis que se retiró de `main` porque dependía
  de `nbformat`, que no está en el lock. Sigue en la etiqueta `pilotos-octubre`; las dos figuras
  que el informe muestra siguen en `figures/`.
- En los resúmenes de `b2-pilot-v2/` el campo `output`, y en los de `hnp-pilot-v1/` y
  `hnp-review-pilot-v1/` el campo `results`, conservan la ruta absoluta y el nombre que les dio el
  corredor. No se reescribieron: los archivos se copiaron byte por byte.

## Históricos

Exploratorios, previos a la suite o de una campaña de pruebas que la tesis no reporta como
resultado. Casi todos se quedan en su lugar porque pruebas o hashes los fijan.

| Grupo | Dónde |
| --- | --- |
| El primer piloto de gobernanza con el comprador presente; lo reemplazó el segundo | `b0prime-b2-20261020T201055Z.jsonl`, su `.summary.json` y el informe `b2-pilot-v1-report.md`, que publica sus huellas. Conserva el nombre que le dio el corredor para que el campo `output` siga siendo exacto. |
| La campaña de escenarios P0, contra el comercio y el proveedor de pagos reales | `exp-01-first-batch/` a `exp-05/`, `exp-08-inf31-hp/`, `exp-09-ct19-hnp/`, `ct19-hp/`, `p0-model-failures/`, `p0-ux08-ux10/`, [`p0-cierre-yair/`](p0-cierre-yair/README.md), `p2-merchant-provenance/`, `p3-point5/` y `p3-v01-offer-checkout/`. Cada una tiene su README y casi todas su `SHA256SUMS`; `../scenarios/p0/matrix-observed.json` y las pruebas de `tests/evaluation/` fijan las que respaldan la matriz observada. |
| Los escenarios conversacionales con gpt-5-mini | `exp06-smoke/`, `exp06-batch/`, `exp06-hp/`, `exp06-actions-smoke/` y `exp06-protected-smoke/`, con la revisión oficial en [`exp06-review/`](exp06-review/README.md). |
| El validador de la compra delegada (HNP-02), offline | [`archive/hnp02/`](archive/hnp02/hnp02-live-20261020T180845Z-s20261022/README.md), con su README y su `SHA256SUMS`. |
| El piloto de la suite | [`suite/archivo/`](suite/README.md#piloto-en-archivo), las cuatro corridas `*-k5` y sus logs. |
