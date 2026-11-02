# P1: contrato de evaluación y UX03 HP

Estado: preregistro y arnés determinista, **sin ejecución de escenarios**.
Fuente: [página 06 de Notion](https://www.notion.so/f6dec8b0ab2e8213b71c016fb6c1e64d),
consultada el 2 de noviembre de 2026; base auditada Agent
`4555bff91be5893a622a925d607f95d01f26a102`.

## Matriz y límites de la auditoría

[matrix.json](matrix.json) conserva literalmente los 33 enunciados y sus expectativas
(`source_text`), más la propuesta de modo, estímulo, cobertura leída, brecha y datos.
Los modos no explícitos en Notion son decisiones propuestas, no hechos del runtime.
`coverage.behavior` describe lo que las aserciones existentes comprueban, **no una ejecución
nueva ni una validación completa del escenario**. `existing_test` significa prueba escrita.
Las referencias históricas siguen en el índice de evaluación y sus informes originales;
no se importan como observaciones de estas nuevas celdas.

Los oráculos de los otros 32 escenarios aún requieren preregistro específico: la expectativa
de Notion sola no basta para medirlos. Los oráculos de cada fila son propuestas,
no especificaciones de ejecución completas. UX04 y UX05 mantienen su
identidad aunque solapen conceptualmente UX03-C/D; no se deduplicarán resultados sin decidirlo.

Capacidades pendientes: Merchant auditado rechaza cupones/descuentos (CT17); no se dispone de
promesa fiable de entrega para UX12. Los grants no se editan in-place (MA22/23): definir
revocación y nuevo consentimiento. PSP modela approved/declined, sin saldo bancario ni razón
específica de fondos insuficientes (INF32). No reemplazar estas ausencias por simulaciones
presentadas como integración comercial.

## Especificación UX03

Instrucción: «Comprar A si se cumplen X y su presupuesto; en caso contrario, comprar B
únicamente si se cumplen Y y su presupuesto». Modo inicial: **HP**, con aprobación humana
explícita compatible con los términos finales. B tiene un límite propio, no hereda el de A.
No se implementan ramas comerciales en P1.

Elegibilidad propuesta de cada rama: condición verificable verdadera, stock disponible y
total autoritativo en moneda explícita menor o igual a su presupuesto. Se exige evidencia
suficiente y vigente de esos hechos. Falso y desconocido son estados distintos.

| Celda | A | B | Decisión esperada |
| --- | --- | --- | --- |
| UX03-A | cumple | no cumple | seleccionar A |
| UX03-B | no cumple | cumple | seleccionar B |
| UX03-C | cumple | cumple | seleccionar A por prioridad, aunque B cueste menos |
| UX03-D | no cumple | no cumple | ninguna; explicar, no seleccionar una tercera |
| UX03-UNKNOWN-A | desconocido por fallo técnico | cumple | indeterminado; informar o recuperar, no fallback como si A fuera falsa |

Elegir A o B **no aprueba ni ejecuta una compra**. En ninguna celda se autoriza gasto
por el mero resultado del oráculo. Si A no cumple y B es desconocido, también se suspende
la decisión. Un fallo de formato o monedas incompatibles invalida la entrada.

## Cinco etapas que no deben confundirse

1. **Precio observado del feed:** observación comercial fechada/versionada; no demuestra
   total final ni disponibilidad futura. Guardar snapshot y hash, sin inventar IDs o importes.
2. **Total autoritativo:** importe confirmado por Merchant con envío, moneda, cantidad y
   términos concretos. El límite se aplica al total, no solo al precio del feed.
3. **Selección:** propuesta de alternativa compatible. Preservar por separado decisión
   preliminar y decisión tras conocer los términos; no reconstruir evidencia anterior
   usando una observación posterior.
4. **Preparación del checkout:** puede crear estado y otros efectos. No asumir una API de
   cotización pura ni abrir checkouts de ambas ramas de manera implícita.
5. **Aprobación y compra:** consentimiento HP ligado a términos vigentes; completar y
   conciliar son pasos posteriores. Un cambio exige revalidación y, si corresponde,
   nueva aprobación. No atribuir controles HP/HNP a todos los modos automáticamente.

## Estrategia Merchant: pendiente de decisión, integración bloqueada

Antes de ejecutar integración, revisar endpoints actuales y efectos de create/update/cancel,
reserva/decremento de stock, expiración y capacidad de obtener envío. Resolver la tensión:
necesitamos un total autoritativo para decidir, pero obtenerlo puede crear un checkout.
Opciones a revisar: datos autoritativos en episodio aislado con preparación explícita y
limpieza verificable, o una capacidad de cotización existente demostrada. No añadir una API
nueva ni llamarla sin revisión de este protocolo. Documentar qué efectos están permitidos,
qué preparación es especulativa, barreras temporales y qué decisión se evalúa en cada etapa.

Derivar A/B de un snapshot versionado obtenido del Merchant de pruebas o de un episodio
aislado con semilla y SHA conocidos; seleccionar por atributos/condiciones del caso,
no por IDs/precios arbitrarios. Fijar presupuestos relativos a los totales observados
(por ejemplo igualdad y una unidad menor de diferencia) **después de aprobar la estrategia
de obtención**; congelar entradas antes de ejecutar el agente. El oráculo independiente
usa el estado comercial controlado y registros autoritativos, nunca la respuesta del
selector ni el scorer. Registrar estado, revisión y momento de cada observación.
Si no hay dos alternativas apropiadas, registrar bloqueo, no sustituir silenciosamente
el catálogo.

## Niveles, evidencia y organización

- Unitario offline: consistencia de manifiestos, aritmética sintética, prioridad,
  límite inclusivo, desconocido, entradas inválidas y contabilidad de resultados.
- Evaluación con LLM real: comprensión de ramas/presupuestos/prioridad y errores técnicos;
  requiere autorización futura, presupuesto y trazas sanitizadas. No se activa por defecto.
- Integración Merchant/PSP/PostgreSQL desechables: total/envío, stock y cambios de estado,
  preparación, aprobación, fallos parciales, idempotencia y carreras. Sin LLM pagado por defecto.

Plan actual: 33 escenarios; UX03 se expande a cinco celdas, por lo que hay **37 unidades
planificadas**, todas `not_executed`. El arnés no es una de esas unidades.
`scenarios_p0.count_plan` compara IDs planeados con observaciones; no infiere ejecución por
nombres de tests ni por JUnit. En P1 no hay adaptador de ejecución de escenarios:
el informe lo dice explícitamente y conserva las 37 pendientes incluso con pytest verde.

`score_ux03` compara solamente una etiqueta de decisión observada con el oráculo
preregistrado: una discrepancia es failed y ausencia de observación es not_executed.
No evalúa aprobación ni efectos de compra y no alimenta el informe de escenarios en P1.

JUnit es evidencia de tests Python. Un complemento de pytest preserva passed, failed,
skipped, xfail, xpass (incluido strict) y tests recolectados no ejecutados.
Errores de colección/sesión se informan aparte. Un informe ausente indica desconocimiento,
no cero fallos. Los contadores JSON se validan junto con el XML JUnit; artefactos
ausentes, truncados o inconsistentes no admiten éxito y hacen fallar el comando de reporte.
Un resultado failed de pytest sigue siendo failed aunque generar su informe sea correcto.
La publicación de artefactos se intenta ante fallos; cancelación o terminación forzada
pueden impedir escribirlos. Nunca declarar integración completa porque el CI offline pase o porque
live-stack se omita. La evidencia nightly es independiente y debe cubrir los casos requeridos.

Mantener tests en `tests/evaluation/`, contrato/datos aquí y código en
`evaluation/src/commerce_evaluation/`. Resultados futuros van a
`evaluation/results/<run-id>/`, sin sobrescribir historia: manifiesto congelado, SHAs
Agent/Merchant/PSP, lock y hashes, modo/configuración, versión de datos, JUnit, trazas
sanitizadas, comparación esperada/observada y efectos antes/después. Distinguir
`not_executed`, bloqueo, fallo del arnés y fallo comercial. No almacenar credenciales ni
datos personales. Reportar bugs/correcciones vinculados a celda, SHA y evidencia original.

## P1-FIX: cabecera JUnit y commits evaluados

El run [41516151762](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41516151762)
ejecutó el head de PR f9279549cfa5901a35926eb58435fa12b45c1f0e mediante el merge temporal
1ea3f7c0d6a6660293e4243d7184831c771986c8. Sus 3516 elementos testcase concuerdan con
3424 passed y 92 skipped; el atributo tests=3594 incluye además 78 reportes de subtests
exitosos de unittest en los 32 tests P1. No son fallos ni escenarios adicionales.

Pytest 9.0.2 (lock) emite reportes de subtests con el nodeid del padre. LogXML.append_pass
incrementa sus estadísticas por cada reporte, pero conserva un solo testcase del padre.
El complemento registra ahora junit_subtests por estado; el lector reconcilia la cabecera
con los elementos y esos reportes, sin tolerancias numéricas arbitrarias. Un exceso sin
registro, casos ausentes, estados contradictorios o XML truncado siguen siendo inválidos.
Los fallos de subtests siguen dominando al reporte exitoso posterior del padre.
Las excepciones previas de call/teardown con error conservan su tratamiento explícito.

El manifiesto report.json distingue pr_head_sha (código propuesto), actions_sha
(GITHUB_SHA) y checked_out_sha (git HEAD realmente ejecutado). agent_sha conserva el
significado de HEAD por compatibilidad. El SHA de Actions debe coincidir con el checkout;
fuera de Actions los metadatos de PR/Actions quedan null, sin inventar procedencia.
La configuración transmite únicamente SHAs del evento, sin alterar permisos ni secretos.

Las regresiones incluyen un subprocess de pytest real aislado, sin conftest del producto,
que comprueba subtests passed/failed/skipped y sus XML. Se ejecuta con las dependencias
del lock en CI; con Python estándar sin pytest se registra skipped, nunca passed.
El run original y sus artefactos permanecen históricos: el campo nuevo no se retroañade
a sus resultados como si hubiera sido medido allí.

## Reproducción

Entorno de referencia: Ubuntu del `python-tests` existente en
`.github/workflows/ci.yml`, Python de `.python-version`, uv 0.12.15 y `uv.lock`.

```sh
uv python install
uv sync --frozen --all-groups
uv run pytest tests/evaluation/test_scenarios_p0.py -q
uv run pytest --cov --cov-report=term --junitxml=.local/p0/python-junit.xml -p commerce_evaluation.p0_pytest_report --p0-report=.local/p0/python-outcomes.json
uv run python -m commerce_evaluation.scenarios_p0 --pytest-report .local/p0/python-outcomes.json --junit .local/p0/python-junit.xml --output .local/p0/report.json --summary .local/p0/summary.md
```

Sin dependencias preparadas, las pruebas nuevas usan solo biblioteca estándar y también
pueden ejecutarse desde la raíz con Python 3.12:

```sh
PYTHONPATH=evaluation/src python -B -m unittest discover -s tests/evaluation -p test_scenarios_p0.py -v
```

En PowerShell: `$env:PYTHONPATH = "evaluation/src"`, seguido del mismo comando Python
sin la asignación inicial. No reutilizar el entorno de otro checkout.
El resultado local que se comunique debe identificar comando y SHA; GitHub Actions queda
pendiente hasta publicar una revisión autorizada. No se han ejecutado aquí escenarios,
LLM ni integración.

## Decisiones pendientes antes de medir UX03

Revisar con Yair el protocolo de total autoritativo, la definición temporal de X/Y,
la semilla/snapshot, los efectos permitidos al preparar y el criterio de recuperación.
Aprobar luego adaptadores de observación, contratos de evidencia y pruebas del flujo.
Pruebas futuras deben aceptar que el producto mejore: ninguna aserción exige que siga
sin soportar ramas condicionales.
