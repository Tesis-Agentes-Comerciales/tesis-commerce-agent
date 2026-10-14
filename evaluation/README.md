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

## Archivos

- `b0-baseline.json`: commits, etiquetas, toolchain, modelo, fixtures y evidencia disponible.
- `pilot-scenarios.json`: matriz canónica y legible por máquina de los cinco escenarios piloto.
- `pilot-matrix.md`: vista humana, protocolo de ejecución y criterios de puntuación.
- `results/b0-pilot-v1.jsonl`: las 25 ejecuciones del piloto B0.
- `results/b0-pilot-v1.summary.json`: agregado generado por el runner.
- `results/b0-pilot-v1-report.md`: análisis humano y decisiones derivadas del piloto.
- `results/b0-pilot-v1-analysis.ipynb`: análisis ejecutable, tablas y gráficos del piloto.
- `results/b0-pilot-v1-analysis.html`: vista renderizada del notebook para lectura sin kernel.
- `b2-design.json`: manifiesto de tratamiento, puntos de decisión, evidencia y criterios de éxito.
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
