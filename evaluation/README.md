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
uv run governance-pilot --dry-run                    # plan B0; --condition B2 para el tratado
uv run evaluate-conversation                         # con agente y Merchant locales activos
uv run hnp-review-demo                               # HNP-01 offline, escribe en .local/hnp01/demo
uv run hnp-matrix --dry-run --env-file .env.example  # HNP-02 con FakeTransport, escribe en .local/hnp02
uv run --with nbformat python evaluation/scripts/build_b0_analysis_notebook.py
```

Los comandos se ejecutan desde la raíz del repositorio. Las salidas HNP deben quedar bajo
`.local/`; el piloto escribe por defecto en `results/`.

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
