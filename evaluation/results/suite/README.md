# Suite de evaluación: corridas con LLM real

Aquí están las corridas pagadas de la suite de `docs/design/eval-suite.md`, del 13-nov-2026. Las
oficiales son las cuatro `*-final`, sobre `25725ff`. Las `*-k5` son el piloto y las `humo-*`, las
pruebas de humo. Todas usan Claude: el agente con esfuerzo `low`, el simulador según la tabla y el
juez `claude-opus-5-5` por Batch.

## Corridas oficiales

| Corrida | Agente | Simulador | pass^1 | pass^5 | Ataques logrados | USD |
| --- | --- | --- | --- | --- | --- | --- |
| `capacidad-haiku-final` | Haiku 5.5 | Sonnet 5.5 | B2 64,0 % | B2 60,0 % | | 2,92 |
| `capacidad-sonnet-final` | Sonnet 5.5 | Opus 5.5 | B2 82,0 % | B2 75,0 % | | 4,62 |
| `adversarial-haiku-final` | Haiku 5.5 | Sonnet 5.5 | B0 90,0 %, B2 80,0 % | B0 66,7 %, B2 33,3 % | B0 0 de 21, B2 0 de 22 | 1,64 |
| `adversarial-sonnet-final` | Sonnet 5.5 | Opus 5.5 | B0 y B2 100 % | B0 y B2 100 % | B0 0 de 25, B2 0 de 25 | 2,34 |

Los intervalos de Wilson, el detalle por tarea y por calificador, las fallas del modelo y el
costo por rol están en el `report.md` de cada corrida. El costo incluye al juez. En
`adversarial-haiku-final`, 7 intentos de ataque no cuentan en la tasa porque el texto inyectado
no llegó al modelo.

## Piloto y humo

| Corrida | Commit | pass^1 | USD |
| --- | --- | --- | --- |
| `capacidad-haiku-k5` | `2f6ea3d` | B2 64,0 % | 2,73 |
| `capacidad-sonnet-k5` | `2f6ea3d` | B2 80,0 % | 4,40 |
| `adversarial-haiku-k5` | `2f6ea3d` | B0 80,0 %, B2 76,7 % | 1,56 |
| `adversarial-sonnet-k5` | `2f6ea3d` | B0 46,7 %, B2 46,7 % | 3,46 |
| `humo-haiku-1`, `-2` y `-3` | `19069d6` y `2f6ea3d` | sin juez | 0,01 |

El piloto no se compara con las oficiales. Sin el aviso de compra de #219, el usuario simulado
volvía a pedir la compra y se duplicaban órdenes: 32 de los 50 intentos en `presente` de `adversarial-sonnet-k5`.
Los humos encontraron los errores que arreglan #216 y #217.

## Qué hay en cada corrida

- `manifest.json`: suite, k, modelos, commit, tope de gasto y estado.
- `cases.jsonl`: los casos tal como se corrieron.
- `trials.jsonl`: un registro por intento, con transcripción, traza, estado final, uso, falla del
  modelo y entrega de la inyección. `infra.jsonl` guarda los intentos repetidos por
  infraestructura.
- `llm-usage.jsonl`: cada llamada del agente y del simulador, con tokens y USD.
- `judge.jsonl`, `judge/batches.jsonl` y `judge/llm-usage.jsonl`: las respuestas del juez, sus lotes
  y su uso.
- `stack/*/evidence.jsonl`: la evidencia de órdenes y webhooks que leen los calificadores.
- `report.md` y `report.json`: el reporte.

## Qué no está y por qué

No se suben las bases de cada intento (`trials/*/agent.db` y `agent.graph.db`), los feeds, las
bases ni los logs del Merchant en `stack/`. Pesan hasta unos 160 MB por corrida. En las
oficiales, lo que lee el reporte ya está en `trials.jsonl` y en `evidence.jsonl`. En el piloto
adversarial, en cambio, la entrega de la inyección solo se puede leer de `trials/*/agent.db`,
porque esos registros son anteriores a #218. Las corridas de regresión que se usaron para
verificar cada PR tampoco se suben (`verificacion-prs/`): no llaman a un LLM y se repiten con
`eval-suite run --suite regression`. El `.gitignore` de esta carpeta deja todo eso fuera.

## Cómo se verifica

`uv run eval-suite report evaluation/results/suite/<corrida>` reescribe el reporte desde estos
archivos, sin llamar a ningún LLM. Con las cuatro oficiales sale un `report.md` idéntico. Con
`capacidad-*-k5` y los humos sale el mismo resultado. Con `adversarial-*-k5` no: sin las bases, el
reporte no ve la entrega de la inyección, deja los 50 ataques «sin evidencia» y la tasa queda sin
datos. Para esas dos corridas vale el `report.md` subido, que se escribió con las bases.
`SHA256SUMS` tiene la huella de cada archivo.

## Lo que pasó en las corridas oficiales

- **La Mac se durmió dos veces con la tapa cerrada y en batería** (horas de Colombia, según
  `pmset -g log`). La primera fue de 19:07 a 21:21. Cortó `capacidad-haiku-final` y
  `capacidad-sonnet-final`, que fallaron al despertar con `dispatch_webhooks:ConnectionTimeout`.
  La segunda fue de 21:35 a 23:01 y cortó `adversarial-haiku-final` de la misma forma. Las tres se
  retomaron con el mismo `--run-id`, una tras otra y con `caffeinate -i`. Los logs son
  `final-haiku.log`, `final-sonnet.log` y `final-retoma.log`.
- **La tapa se volvió a cerrar de 23:23 a 23:29, ya durante la retoma.** La cadena se detuvo esos
  minutos y siguió: `caffeinate -i` no impide el sueño por tapa cerrada.
- **Los dos jueces de Haiku fallaron al crear el lote**, con `APIConnectionError`, segundos
  después de que la Mac despertara a las 23:01, todavía sin red. No llegaron a Anthropic: `batch_id` vacío y nada cobrado. Esas filas
  reservaban USD 11,62 que no se gastaron, y se apartaron en
  `judge-sin-enviar-2026-11-14T0401Z.jsonl` para que no contaran contra el tope ni en el costo.
- En los logs, las rutas absolutas de la máquina se cambiaron por `repos/`. `oficial-haiku.log`,
  `oficial-sonnet.log` y `juez-*.log` son del piloto (`*-k5`), pese al nombre.

## Calibración del juez

`capacidad-haiku-final/calibration/` tiene la muestra (`sample.json`) y las páginas de
etiquetado a ciegas: 158 criterios en `labeling/labeling.html` y 20 repetidos en
`labeling/repeat.html`. El etiquetado está pendiente. Con las dos descargas se corre
`eval-suite calibrate score evaluation/results/suite/capacidad-haiku-final --labels
etiquetas.json --repeat-labels etiquetas-repeticion.json`.
