# Evaluación de gobernanza

Este directorio fija la condición experimental B0 y define los escenarios que se ejecutarán sin
gobernanza de decisión y, después, con la variante gobernada B2.

## Alcance actualizado — 14 de octubre de 2026

El diseño final incluye HP y HNP. Este directorio conserva el piloto **HP** y el
manifiesto B2 original como antecedentes; no acredita implementación HNP ni B2.
El [roadmap vigente](../docs/b2-governance/system-design-v1/roadmap.md) requiere
una base HNP equivalente y corpus separado antes de comparar por modo. No mezclar
HP y HNP ni atribuir cambios de autonomía/protocolo a gobernanza.

## Condiciones

- **B0** conserva el prototipo transaccional actual: ACP, AP2 Human Present, filtros duros,
  aprobación explícita, idempotencia y validaciones deterministas. No incluye un evaluador externo
  de políticas sobre la selección ni un registro normativo de por qué una alternativa fue elegida.
- **B2** debe conservar el mismo modelo, prompt, catálogo, servicios y controles transaccionales.
  La única diferencia experimental será la capa de gobernanza de la decisión y su evidencia.

No se elimina AP2 ni la aprobación humana para construir B0. La comparación estudia la decisión
anterior al checkout, no una versión artificialmente insegura del pago.

## Código de los experimentos

Desde el 20 de octubre de 2026 este directorio es el miembro `tesis-commerce-evaluation` del
workspace uv. El código de los experimentos vive aquí, depende del agente y no entra en el wheel
ni en la imagen del agente (`src/commerce_agent`), que nunca lo importa. Es una dependencia del
grupo `dev`, así que `uv sync --frozen --all-groups` lo instala junto con el agente; ruff, pyright
y pytest lo cubren desde la raíz. Las pruebas están en `../tests/evaluation/`.

| Ruta | Contenido |
| --- | --- |
| `src/commerce_evaluation/governance_pilot.py` | runner del piloto pareado B0/B2 |
| `src/commerce_evaluation/conversation.py` | evaluación conversacional contra un agente local |
| `src/commerce_evaluation/hnp/` | arnés HNP-01 y validador LLM de HNP-02 ([`llm/`](src/commerce_evaluation/hnp/llm/README.md)) |
| `src/commerce_evaluation/hnp_review_demo.py`, `hnp_matrix.py` | runners de HNP-01 y HNP-02 |
| `scripts/evaluation_mutate_offer.py` | estímulo de S04; el piloto lo ejecuta con el entorno del Merchant |
| `scripts/build_b0_analysis_notebook.py` | genera el notebook B0; `nbformat` no está en el lock |

```sh
uv run governance-pilot --dry-run                    # plan de B0'; --condition B2 para el tratado
uv run governance-pilot --dry-run --condition B0prime --condition B2  # plan intercalado del piloto
uv run evaluate-conversation                         # con agente y Merchant locales activos
uv run hnp-review-demo                               # HNP-01 offline, escribe en .local/hnp01/demo
uv run hnp-matrix --dry-run --env-file .env.example  # HNP-02 con FakeTransport, escribe en .local/hnp02
uv run --with nbformat python evaluation/scripts/build_b0_analysis_notebook.py
```

Los comandos se ejecutan desde la raíz del repositorio. Las salidas HNP deben quedar bajo
`.local/`; el piloto escribe por defecto en `results/`.

## Piloto B2 gobernado: B0' y B2

La spec es [`b2-governed-pilot.md`](../docs/design/b2-governed-pilot.md) y el manifiesto
pre-registrado, [`data/b2-pilot-v1.json`](data/b2-pilot-v1.json). Las dos condiciones corren sobre el
mismo commit y en la misma invocación. Una corrida en vivo usa el modelo real
(`OPENAI_MODEL=gpt-5-mini`) y escribe en `results/<condiciones>-<fecha>.jsonl`:

```sh
uv run governance-pilot --condition B0prime --condition B2
```

- **Orden** (`execution.order` del manifiesto): por escenario y por repetición; dentro de cada
  repetición corren las dos condiciones y la primera alterna: en las repeticiones impares va el
  orden dado y en las pares, el inverso. `--dry-run` imprime el plan en ese orden. Con una sola
  `--condition` el plan es el de antes (escenario y repetición).
- Cada corrida arranca un agente nuevo con su propio directorio (`<condición>-<escenario>-<n>`), su
  `agent.db` y su corrida del comercio.
- **Procedencia.** Cada fila y el resumen registran `manifest_id` (`B2-PILOT-v1` solo cuando corren
  exactamente B0' y B2 sobre la matriz del manifiesto; con una sola condición u otra matriz,
  `null`), `agent_commit` y `agent_worktree_dirty`. Si hay cambios sin commit en archivos versionados del agente, el runner
  aborta antes de levantar servicios, salvo con `--allow-dirty`. Los archivos sin versionar (como
  los resultados) no cuentan.
- **Resumen** (`.summary.json`): los escenarios y repeticiones planeados (`scenarios`,
  `repetitions`), `by_condition` y `by_condition_scenario`. Las filas con `harness_error` se cuentan
  en `harness_errors`; `oracle_pass_rate` y `mean_latency_ms` se calculan sobre las filas evaluadas
  y `oracle_pass_rate_all_runs`, sobre todas. Con una condición, el resumen conserva además los
  totales en la raíz; con varias no se agrega una tasa común.
- **Traza (enmienda M1).** La métrica heredada `trace_completeness` de cada fila tiene techos de
  0,25 a 1,0 según el escenario y no es comparable, así que el resumen no publica su media:
  `trace_completeness` es `"not_evaluated"`. La cobertura de evidencia gobernada por mecanismo
  (`mechanism_evidence`, abajo) se reporta aparte.

El runner le pasa al agente la condición como variables de proceso, que ganan sobre su `.env`:

| Condición | Variables |
| --- | --- |
| `B0prime` | `AGENT_GOVERNANCE_MODE=off`, `AGENT_HP_FLOW=blocked` |
| `B2` | `AGENT_GOVERNANCE_MODE=enforce`, `AGENT_HP_FLOW=governed`, `AGENT_D1_ALLOWED_MERCHANT_ORIGINS` = origen del comercio y los `AGENT_D1_*` de abajo |

- Los identificadores de D1 son estables: política `b2-pilot-v1`, revisión `1` y clave
  `b2-pilot-v1-run`. Las claves HMAC y AES se generan en memoria en cada invocación y solo las
  recibe el agente. No se escriben en los resultados y se ocultan en la cola del log del agente.
- Antes de cada repetición, el runner lee `GET /health` del agente. Si `governance_mode` o `hp_flow`
  no son los de la condición, aborta la invocación completa, con todas sus condiciones. Cada fila
  del JSONL registra ambos valores; en las filas con `harness_error` quedan en `null`.
- Las claves D1 se generan una vez por invocación y solo si corre B2.
- `--condition B0` sigue funcionando y en este commit equivale a `B0prime`: las filas dicen
  `B0prime`. El B0 histórico (`B0-2026-10-06`, commit `767058a`) no se puede volver a correr con
  este runner. El comercio sigue sembrando la corrida como `B0`, porque solo conoce B0, B1 y B2.
- S04 (enmienda M2) solo cuenta como éxito si la aprobación responde
  `checkout_terms_changed`. Cualquier otro error no cuenta.
- B2 no arranca si el `.env` local define `AGENT_D1_POLICY_PATH` o `AGENT_D1_POLICY_DIGEST`,
  porque enforce no acepta archivo de política y lista de comercios a la vez.

### Evidencia gobernada, checkouts del comercio y latencia de política

El oráculo (`score_result`) no cambia. Cada fila evaluada suma tres campos:

- `merchant_checkouts` (enmienda M3): después del turno (y, en S04, de la aprobación) y **antes**
  de que el runner cancele la compra que el agente haya dejado abierta, corre
  `python -m commerce_lab.db run-checkouts <merchant_run_id>` en el comercio y registra `count`,
  `by_status`, `order_count` y la lista (`checkout_id`, `status`, `offer_id`). Así un `canceled`
  es un checkout que el agente creó y canceló, y uno abierto aparece como `prepared` o
  `ready_for_payment`. La fila registra aparte `runner_canceled` (si el runner llamó a cancelar).
  Al arrancar, el runner verifica que el comercio tenga ese subcomando; si no, se detiene antes de
  levantar servicios.
- `governed_evidence`: cuando el agente ya terminó, el runner abre su `agent.db` en solo lectura
  (`mode=ro`). `d1_decisions` trae, de `governance_evidence`, `record_kind`, `outcome`, `origin`,
  `reason_codes` y `latency_ms`. `hp_evidence` trae el `record_kind` y solo los campos de resultado
  del cuerpo (frescura: `stage`, `outcome`, `decision`, `changed_fields`, `reason`,
  `governance_mode`, `observed_revision`; recuperación: `control`, `kind`, `cause`), sin
  identificadores ni montos. `hp_evidence_chain_verified` dice si la cadena de digests verifica;
  sin filas es `null`. Una tabla que no existe es `null` (no disponible), nunca un error.
- `mechanism_evidence`: para B2, cada mecanismo que `attribution.mechanism_by_scenario` del
  manifiesto asigna al escenario queda `recorded`, `missing` (la tabla existe pero no hay registro),
  `unavailable` (la tabla no existe) o `not_recorded` (no tiene registro propio: la continuación
  gobernada reutiliza el allow de D1). `d1_dispatch` busca un `decision_record` de D1 con
  `outcome: allow` (un deny no despacha), `offer_freshness` un `freshness_check` y
  `governed_recovery` un `governed_recovery`. Para B0' es `not_applicable`.

El resumen agrega, por condición y por condición × escenario: `http_5xx`; `merchant_checkouts`
(corridas medidas, corridas en que canceló el runner, corridas con checkouts, total, `by_status` y
órdenes); `approval_response_codes`
(conteo por código entre las aprobaciones enviadas; `none` si la respuesta no trae código);
`policy_latency_ms` (`n`, `median`, `max` de los `decision_record` con `origin: policy`, con
resolución de 1 ms, truncada), aparte de `mean_latency_ms`, que es la latencia del turno con el
modelo; y `mechanism_evidence`, el conteo de estados por mecanismo.

## Archivos

- `data/b0-baseline.json`: commits, etiquetas, toolchain, modelo, fixtures y evidencia disponible.
- `data/pilot-scenarios.json`: matriz canónica y legible por máquina de los cinco escenarios piloto.
- `data/pilot-matrix.md`: vista humana, protocolo de ejecución y criterios de puntuación.
- `data/b2-design.json`: manifiesto de tratamiento, puntos de decisión, evidencia y criterios de éxito.
- `data/hnp/cases.json`, `data/hnp/oracle.json`: corpus HNP de 27 casos y su oráculo separado.
- `results/b0-pilot-v1.jsonl`: las 25 ejecuciones del piloto B0.
- `results/b0-pilot-v1.summary.json`: agregado generado por el runner.
- `results/b0-pilot-v1-report.md`: análisis humano y decisiones derivadas del piloto.
- `results/b0-pilot-v1-analysis.ipynb`: análisis ejecutable, tablas y gráficos del piloto.
- `results/b0-pilot-v1-analysis.html`: vista renderizada del notebook para lectura sin kernel.
- `../docs/b2-governance/`: investigación, arquitectura, contrato, amenazas y plan de B2.

## Protocolo del piloto

1. Restaurar los tres repositorios a la etiqueta `b0-baseline-2026-10-06`.
2. Crear bases temporales y sembrar el catálogo desde el fixture fijado en el manifiesto.
3. Ejecutar cada escenario cinco veces con B0.
4. Guardar una fila de resultado por ejecución, incluso cuando el agente falle.
5. Implementar B2 detrás de una configuración explícita, sin modificar los demás controles.
6. Repetir los mismos escenarios con el mismo modelo, prompt y estado inicial.

Cinco repeticiones sirven únicamente para detectar errores en el diseño del experimento. El tamaño
de la evaluación final se define después de observar la variabilidad del piloto.

## Unidad de resultado

Cada ejecución debe registrar como mínimo:

- condición, escenario, repetición y versiones de los tres servicios;
- solicitud e intención estructurada;
- candidatos considerados, descartes y alternativa seleccionada;
- resultado de política cuando la condición sea B2;
- checkout, huella mostrada y decisión humana;
- pedido, mandates, recibos o error terminal;
- latencia, uso del modelo y métricas derivadas.

Los secretos, bearer tokens y tokens de pago no forman parte de los artefactos experimentales.
