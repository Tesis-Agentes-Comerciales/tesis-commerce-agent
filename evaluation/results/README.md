# Resultados experimentales

El runner (`uv run governance-pilot`) crea aquí un archivo JSONL y un resumen JSON por lote. Cada
línea corresponde a una repetición y no contiene bearer tokens, claves privadas ni tokens de pago.

El artefacto completo de cada corrida (bases del agente, feed, log redactado y `MANIFEST.json`)
nunca se guarda aquí: queda en `.local/runs/`, fuera de Git, porque lleva JWT y mandatos del
sandbox. Cada fila lo nombra en `run_artifact` con su ruta relativa y sus sha256
([`../README.md`](../README.md#artefacto-de-cada-corrida-governance-pilot-y-hnp-pilot)).

Los resultados del piloto se conservan aunque incumplan el oráculo: una desviación es evidencia
experimental, no un error que deba borrarse o reejecutarse silenciosamente.

El lote canónico de B0 es `b0-pilot-v1.jsonl`; `b0-pilot-v1-report.md` resume sus resultados. Los
archivos con prefijo `b0-smoke-` corresponden únicamente a la validación previa del arnés y no se
incluyen en las métricas del piloto.

Edición manual: en los tres `b0-*.summary.json` el campo `output` se reescribió a mano en el PR #24
(commit `dea6245`). El runner había guardado la ruta absoluta del JSONL en la máquina que lo
produjo; ahora es relativa a la raíz del repositorio (`evaluation/results/<lote>.jsonl`). Ningún otro
campo, el orden de las claves ni el formato cambiaron, y los JSONL no se tocaron. Ningún digest,
manifiesto ni prueba fija los bytes de esos resúmenes.

El lote canónico del piloto B2 gobernado (B0' frente a B2, manifiesto `B2-PILOT-v1`) es
`b0prime-b2-20261020T201055Z.jsonl`, con su `.summary.json`. Conserva el nombre que le dio el runner
para que el campo `output` del resumen siga siendo exacto. `b2-pilot-v1-report.md` resume sus
resultados frente a los criterios pre-registrados.

El notebook ejecutado `b0-pilot-v1-analysis.ipynb` reconstruye los agregados directamente desde el
JSONL y exporta las figuras de `figures/`. La versión HTML permite revisar el resultado sin ejecutar
un kernel.

`hnp02/<run_id>/` guarda corridas de EC-HNP-02 (`uv run hnp-matrix`), cada una con su `README.md`
de configuración, resultados y lectura, más `SHA256SUMS`. La primera corrida en vivo es
`hnp02/hnp02-live-20261020T180845Z-s20261022/`.

El piloto B2 gobernado v2 (manifiesto `B2-PILOT-v2`) está en `b2-pilot-v2/`.
- Contiene el lote 1, el lote 2 (S05, S06 y S08, una invocación por escenario) y la combinación que cuenta, con `SHA256SUMS`.
- `b2-pilot-v2-report.md` resume los resultados frente a los criterios pre-registrados y explica la regla del lote de completitud.
- En estos resúmenes, el campo `output` conserva la ruta absoluta que guardó el runner. No se reescribió.
