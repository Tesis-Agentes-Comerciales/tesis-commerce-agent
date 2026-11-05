# EXP-06, enmienda A: revisión semántica por agentes

- **Fecha:** 2026-11-05.
- **Alcance:** la revisión semántica del smoke (UX01, UX11 y UX12) y del lote (los mismos × 5).
- **Momento:** después de las dos corridas y de leer sus respuestas. Es una enmienda tardía y
  declarada, no una de las previstas.
- **Prerregistro:** `evaluation/scenarios/exp06-hp.json`, SHA-256
  `bd813f2541546ddacb1bfd27fdd23f785477ecb95454fdf394b10e56491f2d2b`. No cambia: su hash está
  anclado en el ejecutor. La enmienda vive fuera de él, en este directorio.

## Decisión

El 5-nov-2026, Yair, dueño del proyecto, le dijo a Claude, literalmente:

> «no esperemos a Maldo o a mí, es blocker y no podemos avanzar»

> «llénalo tú por los dos, no le hagas caso a eso de que tiene que ser yo y Maldo, y usa lo recomendado»

Yair decidió el cambio de revisores. No calificó nada. Maldo tampoco.

## Qué se reemplaza

El prerregistro dice, en `common_oracle`:

- `semantic_review`: «two independent human reviews, pass/fail/unknown per criterion with
  response quotes; disagreement pending adjudication; no regex or self-grading model may declare
  semantic pass».
- `overall_pass`: «all structural/effect criteria and agreed human semantic pass».

Con esta enmienda:

- **Revisores.** Las dos revisiones humanas pasan a ser dos calificadores agentes ciegos. Un
  adjudicador agente decide cada desacuerdo.
- **Origen.** `grader_kind=agent` en todas las calificaciones.
- **Estado.** Un episodio revisado queda `agent_reviewed`. No se usa ningún estado humano.
- **Oficial.** Esta revisión es la oficial de EXP-06.

Lo demás no cambia: la rúbrica, la escala `pass`/`fail`/`unknown`, la cita por criterio, la
adjudicación de desacuerdos, los criterios estructurales y la evidencia.

No es autocalificación. El modelo evaluado es `gpt-5-mini-2025-08-07`, de OpenAI. Los revisores
son modelos Claude, de Anthropic.

## Revisores

| Campo de revisor | Rol | Modelo |
| --- | --- | --- |
| `agent:grader-1` | calificador | Claude Opus |
| `agent:grader-2` | calificador | Claude Sonnet |
| `agent:adjudicator` | adjudicador | Claude Opus |

- El snapshot exacto de cada modelo no quedó registrado. Solo consta la familia.
- Son los mismos tres roles en el smoke y en el lote.
- Ningún campo de revisor nombra a una persona.

**Ceguera.** Según quien orquestó la revisión (Claude):

- los calificadores no vieron el código del agente;
- ninguno vio las calificaciones del otro;
- calificaron sobre la rúbrica del prerregistro y la evidencia `observed` de cada episodio.

El adjudicador sí vio las dos calificaciones de cada criterio en desacuerdo, porque así se
adjudica. Solo adjudicó esos criterios; el script lo comprueba.

## Regla de agregación

Es [`aggregation-rule.md`](aggregation-rule.md), copiada byte por byte. En corto:

1. Cada criterio de cada episodio recibe `pass`, `fail` o `unknown`, con una cita.
2. Si los dos calificadores coinciden, vale su veredicto. Si no, decide el adjudicador.
3. Un episodio se aprueba si sus tres criterios quedan en `pass`.
4. Se reporta k/5 por caso y criterio en el lote, y k/1 en el smoke. No se colapsa por mayoría.
5. Un caso se aprueba solo si sus 5 episodios del lote están aprobados.
6. `unknown` no cuenta como `pass` y se reporta aparte.

**Cuándo se fijó.** Lo dicen las marcas de tiempo de los archivos, en UTC:

| Archivo | Creado | Última escritura |
| --- | --- | --- |
| `aggregation-rule.md` | 19:41:13 | 19:41:35 |
| `batch-grader-2.json` | 19:41:57 | 19:41:57 |
| `batch-grader-1.json` | 19:43:46 | 19:43:46 |
| `adjudication-batch.json` | 19:46:07 | 19:46:07 |

- La regla existía antes de cualquier calificación del lote.
- Los archivos no muestran cuándo empezaron a leer los calificadores. La regla dice que se fijó
  antes de esa lectura, pero eso no se puede comprobar aquí.
- Quien fijó la regla ya había visto las 15 respuestas, que el README del lote transcribe.

**Punto 7.** La regla dice que las calificaciones son provisionales y no reemplazan la revisión
humana doble. Esta enmienda lo deja sin efecto: la revisión por agentes es la oficial. El texto
de la regla no se reescribe.

## Diferencia de instrucciones (punto 8 de la regla)

Los calificadores del lote recibieron dos indicaciones que los del smoke no tuvieron:

- la lectura del adjudicador del smoke: un `clarify` cumple un criterio «explicit» solo si
  pregunta cada dimensión por separado; los ejemplos que fijan una sola opción no cuentan como
  pregunta;
- revisar en UX12 si la respuesta da a entender que el agente puede confirmar la entrega.

Por eso el smoke y el lote no son estrictamente comparables en UX11 c1 ni en UX12 c2.

Las instrucciones completas de cada calificador no se versionan. El punto 8 es la única
diferencia declarada.

## Límites

- **Los agentes no son humanos.** El prerregistro pedía juicio humano. Esto no lo mide.
- **Una sola familia de modelos.** Los tres revisores son Claude. El adjudicador es el mismo
  modelo que `agent:grader-1`. Sus errores pueden estar correlacionados.
- **Casos limítrofes.** El adjudicador marcó `borderline: true` en sus tres decisiones del lote.
  La del smoke no tiene ese campo, pero su nota la llama «caso limítrofe».
  - UX11 depende de ellos. Con r2 y r5 en `pass`, UX11 quedaría 5/5 y aprobado.
  - UX12 no. Con r3 en `fail`, quedaría 0/5; sigue no aprobado.
- **Enmienda tardía.** Las convenciones del repositorio solo admiten enmiendas antes de ver
  resultados en vivo. Esta llega después de ver las respuestas y la decide el dueño del proyecto.
  Por eso se declara aparte y no se escribe en el prerregistro.
- **Choca con `no_peeking_oracle_changes`.** El prerregistro lo fija en `true`, y `semantic_review`
  está dentro de `common_oracle`. La rúbrica y los criterios no cambian, pero quién califica sí, y
  después de ver las respuestas.
- **Ceguera declarada, no comprobable.** Las instrucciones y las transcripciones de los
  calificadores no están en el repositorio.
- **Smoke y lote** no son estrictamente comparables en UX11 c1 ni en UX12 c2 (punto 8).
