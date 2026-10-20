# Resultados experimentales

El runner (`uv run governance-pilot`) crea aquí un archivo JSONL y un resumen JSON por lote. Cada
línea corresponde a una repetición y no contiene bearer tokens, claves privadas ni tokens de pago.

Los resultados del piloto se conservan aunque incumplan el oráculo: una desviación es evidencia
experimental, no un error que deba borrarse o reejecutarse silenciosamente.

El lote canónico de B0 es `b0-pilot-v1.jsonl`; `b0-pilot-v1-report.md` resume sus resultados. Los
archivos con prefijo `b0-smoke-` corresponden únicamente a la validación previa del arnés y no se
incluyen en las métricas del piloto.

El notebook ejecutado `b0-pilot-v1-analysis.ipynb` reconstruye los agregados directamente desde el
JSONL y exporta las figuras de `figures/`. La versión HTML permite revisar el resultado sin ejecutar
un kernel.

`hnp02/<run_id>/` guarda corridas de EC-HNP-02 (`uv run hnp-matrix`), cada una con su `README.md`
de configuración, resultados y lectura, más `SHA256SUMS`. La primera corrida en vivo es
`hnp02/hnp02-live-20261020T180845Z-s20261022/`.
