# Evaluación

Aquí están los experimentos del agente comprador: el código que los corre, sus datos y los
resultados que respaldan la tesis. Los comandos se ejecutan desde la raíz del repositorio, después
de `uv sync --frozen --all-groups`.

## Qué hay aquí

| Carpeta | Qué guarda |
| --- | --- |
| `src/commerce_evaluation/` | El código: la suite (`eval-suite`), el stack limpio por intento, el verificador de evidencia (`verify-evidence`) y la demo local (`demo-stack`). Es el miembro `tesis-commerce-evaluation` del workspace; el agente nunca lo importa. Sus pruebas están en `../tests/evaluation/`. |
| `data/` | Los pre-registros de los pilotos (manifiestos y escenarios) y los casos de la suite, en `data/suite/`. |
| `scenarios/` | La matriz de los 33 escenarios P0 y los protocolos de la campaña que los probó. |
| `results/` | Los resultados de la tesis y, aparte y rotulados, los exploratorios. Ver [`results/README.md`](results/README.md). |
| `scripts/` | `evaluation_mutate_offer.py`, el cambio de precio que usaban los pilotos para el escenario de términos obsoletos. |

## Resultados de la tesis

Rutas relativas a `evaluation/`. Los pilotos de los capítulos 4 y 6 corrieron con gpt-5-mini y la
suite, con Claude. El código que corrió los pilotos salió de `main`: está en la etiqueta
`pilotos-octubre`, y la versión larga de este README queda en el historial de git. Ninguna
verificación de abajo llama a un LLM.

| Resultado | Archivos | Verificación |
| --- | --- | --- |
| Cap. 4: la línea base cumple 15 de 25 | `results/b0-pilot-v1.jsonl`, su `.summary.json`, su `-report.md` y las figuras que este muestra, en `results/figures/`; `data/b0-baseline.json`, `data/pilot-scenarios.json` y `data/pilot-matrix.md` | 1 |
| Cap. 6: con el comprador presente, 79 de 80 con gobernanza frente a 32 de 80 sin ella | `results/b2-pilot-v2/` (10 archivos y `SHA256SUMS`), `results/b2-pilot-v2-report.md`, `data/b2-pilot-v2.json` y `data/pilot-scenarios-v2.json` | 2 y 3 |
| Cap. 6: compra delegada | `results/hnp-pilot-v1/`, `results/hnp-pilot-v1-report.md`, `data/hnp-pilot-v1.json` y `data/hnp-scenarios.json` | 4 |
| Cap. 6: revisor de intención | `results/hnp-review-pilot-v1/`, `results/hnp-review-pilot-v1-report.md`, `data/hnp-review-pilot-v1.json` y `data/hnp-review-scenarios.json` | 3 |
| Cap. 6 y 7: manipulación | `results/suite/adversarial-haiku-final` y `adversarial-sonnet-final`, `data/suite/cases/manipulacion/` y `data/injection-scenarios-v1.json` | 5 y 6 |
| Cap. 7: capacidad, arreglos y comparación de modelos | `results/suite/capacidad-*-final` (Haiku con la calibración del juez en `calibration/`), `capacidad-*-arreglos`, `capacidad-opus`, `capacidad-haiku-medium`, `capacidad-haiku-simopus` y sus logs; `results/suite/README.md`; `data/suite/` y `scenarios/p0/matrix.json` | 5, 6 y 7 |

Las specs de esos diseños están en [`docs/design/`](../docs/design/README.md).

```sh
# 1. La línea base: 15 de 25
uv run python -c "import json; r = [json.loads(l) for l in open('evaluation/results/b0-pilot-v1.jsonl')]; print(sum(not x['oracle_errors'] for x in r), 'de', len(r))"
# 2. El piloto con el comprador presente: con gobernanza (B2) 79 de 80, sin ella (B0prime) 32 de 80
uv run python -c "import json; s = json.load(open('evaluation/results/b2-pilot-v2/pilot-v2-combinado.summary.json')); print({k: f\"{v['oracle_passes']} de {v['evaluated_runs']}\" for k, v in s['by_condition'].items()})"
# 3. Los bytes de b2-pilot-v2, hnp-review-pilot-v1 y del primer piloto
uv run pytest tests/evaluation/test_archived_results.py
# 4. Los bytes del piloto de compra delegada
(cd evaluation/results/hnp-pilot-v1 && shasum -a 256 -c SHA256SUMS)
# 5. Los bytes de todas las corridas de la suite
(cd evaluation/results/suite && shasum -a 256 -c SHA256SUMS)
```

`eval-suite report` y `eval-suite calibrate score` reescriben archivos de la corrida, así que 6 y 7
se corren sobre una copia y se comparan con lo versionado:

```sh
copia=$(mktemp -d)
# 6. Cada reporte se reconstruye igual desde sus registros
for corrida in capacidad-haiku-final capacidad-sonnet-final adversarial-haiku-final \
    adversarial-sonnet-final capacidad-haiku-arreglos capacidad-sonnet-arreglos capacidad-opus \
    capacidad-haiku-medium capacidad-haiku-simopus; do
  cp -R "evaluation/results/suite/$corrida" "$copia/"
  uv run eval-suite report "$copia/$corrida" > /dev/null
  cmp -s "$copia/$corrida/report.md" "evaluation/results/suite/$corrida/report.md" \
    && echo "$corrida: igual" || echo "$corrida: DISTINTO"
done
# 7. La calibración del juez: κ = 0,85 frente a las etiquetas humanas y 0,89 entre pasadas
c="$copia/capacidad-haiku-final/calibration"
uv run eval-suite calibrate score "$copia/capacidad-haiku-final" --labels "$c/etiquetas.json" \
  --repeat-labels "$c/etiquetas-repeticion.json" | grep "κ ="
```

## Volver a correr con costo

Las corridas de capacidad y de manipulación y el juez llaman a Claude y cobran. Solo el dueño las
lanza, con `ANTHROPIC_API_KEY` en el entorno y un tope de gasto. La suite viva levanta el comercio
y el proveedor de pagos reales: necesita `MERCHANT_REPO`, `PAYMENT_REPO` y Docker, como
[`tests/integration`](../tests/integration/README.md).

```sh
uv run eval-suite run --suite capability --dry-run          # plan y costo estimado; no corre nada
uv run eval-suite run --suite capability --max-usd <tope> --run-id mi-corrida   # corrida pagada
uv run eval-suite judge mi-corrida --dry-run                # preguntas pendientes y su costo
uv run eval-suite judge mi-corrida --max-usd <tope>          # el juez, con su propio tope
uv run eval-suite run --suite regression                    # guionada: sin LLM ni tope
uv run verify-evidence --agent-db agent.db --psp-db payments.db   # cadena de evidencia de una base
```

`--suite adversarial` corre la manipulación. `--agent`, `--effort` y `--simulator` cambian el
modelo del agente, su esfuerzo y el del comprador simulado; `--k` cambia los intentos por caso. Un
`--run-id` que ya existe se retoma. `verify-evidence` acepta `--grant` para la cadena de una compra
delegada y no llama a ningún modelo.

## Reglas de la evidencia

- **Los resultados no se editan.** Una corrida que falla o se desvía es evidencia; repetirla es una
  corrida nueva con otro `--run-id`. Si un reporte se regenera, se dice en el README de la corrida.
- **Las carpetas de resultados llevan su `SHA256SUMS`** con la huella de cada archivo, y varias
  pruebas lo comprueban. Se verifica con `shasum -a 256 -c SHA256SUMS` dentro de la carpeta.
- **Nada con secretos va a Git.** El artefacto completo de cada corrida de los pilotos (bases del
  agente, feed y log) quedó en `.local/runs/`, fuera de Git, porque lleva JWT y mandatos del
  sandbox. De la suite tampoco se suben las bases de cada intento (`trials/`) ni lo que deja el
  stack salvo la evidencia: lo excluye `results/suite/.gitignore`.

## Histórico

Lo exploratorio, que no respalda la tesis. Casi todo queda en su lugar porque pruebas o hashes lo
fijan; solo el piloto de la suite pasó a `results/suite/archivo/`.

- **Primer piloto de gobernanza:** `results/b0prime-b2-20261020T201055Z.*`, `b2-pilot-v1-report.md` y `data/b2-pilot-v1.json`; lo reemplazó el segundo.
- **Campaña de escenarios P0:** `results/exp-01-first-batch/` a `exp-05/`, `exp-08-inf31-hp/`, `exp-09-ct19-hnp/`, `ct19-hp/`, `p0-model-failures/`, `p0-ux08-ux10/`, `p0-cierre-yair/`, `p2-merchant-provenance/`, `p3-point5/` y `p3-v01-offer-checkout/`, con `scenarios/`.
- **Escenarios conversacionales con gpt-5-mini:** `results/exp06-*`, con su revisión en `exp06-review/`.
- **Piloto de la suite:** `results/suite/archivo/`, las cuatro corridas `*-k5` y sus logs.
- **Validador de la compra delegada (HNP-02):** `results/hnp02/`, una corrida offline con un validador LLM.
- **Borradores y diseño previo:** `data/injection-pilot-v1.json`, `data/b2-design.json` y el corpus `data/hnp/`.
