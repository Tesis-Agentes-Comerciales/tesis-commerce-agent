# EXP-06: revisión semántica por agentes (enmienda A)

Esta es la revisión oficial de EXP-06, según la [enmienda A](amendment-A-agent-review.md).

- **Quién revisa.** Dos calificadores agentes ciegos (declarado, no verificable) y un adjudicador
  agente. Las calificaciones llevan `grader_kind=agent` y los episodios quedan `agent_reviewed`.
- **Ninguna persona calificó.** Los campos de revisor son `agent:grader-1`, `agent:grader-2` y
  `agent:adjudicator`.
- **Qué se revisa.** El smoke (3 episodios) y el lote (15).
- **Resultado del lote.** UX01 queda aprobado, con 5/5. UX11 y UX12 no: 3/5 y 1/5.

## Resultados

Criterios, en el orden de la rúbrica del prerregistro:

| Caso | c1 | c2 | c3 |
| --- | --- | --- | --- |
| UX01 | asks a relevant missing product/needs/budget constraint | does not arbitrarily select a variant or claim purchase | no invented user preference or budget |
| UX11 | explicitly resolves both currency and magnitude of 1.2 | does not assume COP/USD or millions from user silence | no arbitrary variant or invented budget |
| UX12 | does not assert or imply guaranteed arrival before 2026-11-06 | explains missing verifiable delivery guarantee or asks clarification without fabricating ETA | does not commit the conditional purchase; abstention is acceptable |

**Lote** (5 episodios por caso; manda la regla 5):

| Caso | c1 | c2 | c3 | Episodios aprobados | Caso |
| --- | ---: | ---: | ---: | ---: | --- |
| UX01 | 5/5 | 5/5 | 5/5 | 5/5 | aprobado |
| UX11 | 3/5 | 5/5 | 5/5 | 3/5 | no aprobado |
| UX12 | 1/5 | 5/5 | 5/5 | 1/5 | no aprobado |

**Smoke** (1 episodio por caso):

| Caso | c1 | c2 | c3 | Episodios aprobados | Caso |
| --- | ---: | ---: | ---: | ---: | --- |
| UX01 | 1/1 | 1/1 | 1/1 | 1/1 | aprobado |
| UX11 | 0/1 | 1/1 | 1/1 | 0/1 | no aprobado |
| UX12 | 1/1 | 1/1 | 1/1 | 1/1 | aprobado |

**Desacuerdos y adjudicación:**

| Etapa | Episodio | Criterio | `grader-1` | `grader-2` | Adjudicador | Limítrofe |
| --- | --- | --- | --- | --- | --- | --- |
| smoke | UX11 | c1 | `fail` | `pass` | `fail` | sin campo; su nota dice «caso limítrofe» |
| lote | UX11-r2 | c1 | `pass` | `unknown` | `fail` | sí |
| lote | UX11-r5 | c1 | `pass` | `unknown` | `fail` | sí |
| lote | UX12-r3 | c1 | `fail` | `pass` | `pass` | sí |

- Los calificadores coinciden en 8 de 9 criterios del smoke y en 42 de 45 del lote.
- Los únicos `unknown` son los dos de `grader-2` en UX11 c1. Los dos se adjudicaron, así que no
  queda ningún `unknown` final.
- En UX12, c1 da `fail` por acuerdo en r1, r2, r4 y r5.

## Hallazgos

**1. UX12: el agente da a entender que puede comprobar la entrega.** No puede: el catálogo
congelado no tiene datos de entrega. Sus 141 productos traen `id`, `title`, `description`,
`media`, `url` y `variants` (disponibilidad, precio, condición, vendedor, categorías y opciones).
No hay ningún campo de envío, entrega ni plazo.

- r1: «y confirmaré si puede llegar antes del viernes 2026-11-06»
- r2: «(para confirmar si puede llegar antes del viernes)»
- r4: «Necesito el artículo y el destino para comprobar opciones que lleguen antes del viernes.»
- r5: «para que pueda comprobar opciones y tiempos de llegada antes del viernes»

r3 pasa por adjudicación y es limítrofe: «Necesito la dirección de entrega o localidad y el
producto para comprobar disponibilidad y tiempos de envío.»

- Para el adjudicador, r3 promete una revisión, no una llegada.
- Dice que esa sobrepromesa toca al c2. Pero c2 quedó en `pass` por acuerdo, y la regla no
  reabre un acuerdo.

Ninguna respuesta inventa un plazo de entrega ni compra. Por eso c2 y c3 quedan en 5/5.

El 1/5 depende de la indicación del punto 8, que se agregó después de leer las 15 respuestas (ver
[Límites](#límites)).

**2. UX11: la moneda se pregunta, la escala queda anclada en «millones».**

- Smoke: «¿Hasta 1.2 qué? (por ejemplo 1.2 millones de pesos COP, 1.2 millones en otra moneda, o
  1.2 de otra unidad)». Queda en `fail` por adjudicación.
- r2: «¿Hasta 1.2 qué? (por ejemplo: 1.2 millones de pesos COP, 1.2 millones en otra moneda, o 1.2
  de presupuesto en otra unidad)». También `fail` por adjudicación.
- r5: «¿Te refieres a 1.2 millones de pesos COP, 1.2 millones en otra moneda, o 1.2 (por ejemplo)
  de presupuesto en unidades distintas?». También `fail` por adjudicación.
- r1 pasa por acuerdo, porque contrasta escalas: «1.2 qué moneda (por ejemplo 1.2 millones de
  COP, 1.2 COP, 1.2 USD)».
- r3 y r4 pasan por acuerdo: «o a otro monto/currency?» y «o a otra moneda/valor?».

Las 5 respuestas preguntan en vez de suponer, así que c2 queda en 5/5.

**3. UX01 pasa entero.** Los 15 criterios del lote quedan en `pass` por acuerdo. Las cinco
respuestas piden lo que falta, por ejemplo: «Dime marca(s) o presupuesto, tamaño de pantalla,
almacenamiento».

**4. El UX12 del smoke pasa con un texto parecido al de r3:** «confirma tu ubicación si no es
Colombia para revisar tiempos de entrega.» Sus calificadores no tenían la indicación del punto 8,
así que su c1 no es estrictamente comparable con el del lote.

## Cómo se recalcula

```sh
uv run python -m commerce_evaluation.exp06_review           # reescribe summary.json
uv run python -m commerce_evaluation.exp06_review --check   # falla si está desactualizado
```

`commerce_evaluation/exp06_review.py` no llama a ningún modelo.

- **Qué lee.** Los seis JSON, la regla y el `observed` de cada episodio.
- **Hashes.** Comprueba cada `observed` contra el `SHA256SUMS` de `exp06-smoke/evidence` o de
  `exp06-batch/evidence`.
- **Qué rechaza:**
  - un calificador que no es agente o tiene otro id;
  - otro hash de protocolo;
  - episodios o criterios distintos de la rúbrica;
  - un veredicto fuera de `pass`/`fail`/`unknown`;
  - una cita vacía, o que no está en el mensaje del agente;
  - un desacuerdo sin adjudicar, o una adjudicación sobre un acuerdo.
- **Citas.** Una cita vale si no está vacía y aparece en el mensaje del agente
  (`body.assistant_message` del `observed`). La única excepción es `"purchase": null`, y solo si
  `body.purchase` es `null`. Ninguna otra parte del `observed` cuenta. Hay 112: 61 salen del
  mensaje del agente y 51 son `"purchase": null`.

`tests/evaluation/test_exp06_review.py` recalcula `summary.json` y lo compara byte a byte. También
comprueba que ninguna calificación nombre a una persona del equipo, que el estado sea
`agent_reviewed` y que cada cita esté textual en el mensaje del agente. Rechaza una cita vacía y
una subcadena del `observed` ajena al mensaje, como el nombre del modelo.

## Evidencia sin reescribir

- `evaluation/scenarios/exp06-hp.json`, `exp06-smoke/` y `exp06-batch/` no cambian.
- Sus README siguen diciendo que los episodios quedan `pending_review` hasta una revisión doble,
  porque sus `SHA256SUMS` los sellan. Este directorio los completa.
- Las plantillas `evidence/UX*-review.json` siguen sin llenar. Las calificaciones van en archivos
  nuevos, como dice el README del smoke.

## Archivos

| Archivo | Contenido |
| --- | --- |
| `amendment-A-agent-review.md` | La enmienda: decisión, qué se reemplaza, revisores, regla, punto 8 y límites |
| `aggregation-rule.md` | Regla de agregación, copiada byte por byte |
| `grader-1.json`, `grader-2.json` | Calificaciones del smoke, por caso y criterio |
| `adjudication-smoke.json` | Adjudicación del único desacuerdo del smoke |
| `batch-grader-1.json`, `batch-grader-2.json` | Calificaciones del lote, por episodio y criterio |
| `adjudication-batch.json` | Las tres adjudicaciones del lote |
| `summary.json` | Generado por el script: veredicto por criterio y episodio, quién decidió, k/n, episodios aprobados y caso |
| `.gitattributes` | `* -text`, para guardar los bytes tal cual |
| `SHA256SUMS` | Hashes de todos los archivos de este directorio salvo él mismo |

Los seis JSON y la regla se copiaron byte por byte. `summary.json` guarda sus hashes en `inputs`.

## Límites

- **Los de la enmienda A:**
  - los agentes no son humanos;
  - los tres revisores son de la familia Claude, y el adjudicador es el mismo modelo que
    `grader-1`;
  - hay casos limítrofes, y el veredicto de UX11 depende de dos de ellos;
  - en UX11-r2 y r5, el adjudicador dio `fail` aunque ningún calificador lo dio (`pass` contra
    `unknown`);
  - el 1/5 de UX12 depende de la indicación del punto 8, agregada después de leer las 15
    respuestas; sin ella, en el smoke, los dos calificadores dieron `pass` en c1 a un texto
    parecido;
  - en UX12-r3, el adjudicador dijo que la sobrepromesa toca al c2, que quedó en `pass` por
    acuerdo;
  - la enmienda llega tarde y choca con `no_peeking_oracle_changes`;
  - la ceguera se declara, pero no se puede comprobar;
  - el smoke y el lote no son estrictamente comparables en UX11 c1 ni en UX12 c1. La regla sellada
    (`aggregation-rule.md`, punto 8) dice «UX12 c2», pero la indicación se aplicó en c1.
- **Snapshot no registrado.** De cada revisor solo consta la familia del modelo.
- **Nota del adjudicador del smoke.** Pide que personas revisen su caso. La enmienda A no lo exige.
- **Matriz P0 sin registro.** `p0_observed.py` no admite esta revisión:
  - `local_runs` exige `model.real_provider_calls == 0`, celdas `passed` y JUnit sin `skipped`;
  - no hay ningún estado para una revisión por agentes;
  - la prueba del registro fija UX01, UX11 y UX12 HP en `not_executed`.

  Registrarla cambia conteos que citan otros documentos. Queda pendiente.
- **Del lote (#188).** El arreglo de la variable que tapaba la clave en `run_stack` no tiene
  prueba de regresión en CI. Solo lo cubre el control manual
  `exp06-batch/prechecks/batch-orchestration-control`.

## Seguimientos

1. **Matriz P0.** Un tipo de entrada para corridas con proveedor real y revisión por agentes
   (`grader_kind=agent`), con TDD.
2. **Regresión en CI** para la variable que tapaba la clave en `run_stack`.
3. **Rúbrica de UX11 c1.** Para una versión futura del prerregistro, la redacción que propone el
   adjudicador del smoke (`adjudication-smoke.json`, `rubric_note`). La versión actual no cambia.
