# EC-HNP-02 — validador LLM sobre el arnés HNP

Experimento offline: **brazo A** (payload del comprador + controles del backend) frente a
**brazo B** (lo mismo + un validador LLM). El sumidero es ficticio y en memoria; ningún brazo
alcanza checkout, pago ni comercio, y nada de lo que produce este paquete es una operación real.

Reglas que el código impone, no solo documenta:

- El backend siempre decide. El validador puede bloquear, nunca admitir lo que el backend
  rechaza: B admite un subconjunto de lo que admite A.
- El modelo solo ve hechos públicos de revisión (`ProposalView`, `ReviewerContext`): sin
  `eligible_variant_ids`, sin id del adaptador comprador, sin digests de sobre, sin etiquetas
  del caso ni del oráculo. Responde diez checks de negocio, una decisión y razones breves; el
  adaptador deriva el resto (cobertura, refs, digests, códigos VAL y el check `binding`).
- Una decisión que contradice sus propios checks, un JSON fuera de esquema, una negativa o una
  respuesta truncada se tratan como salida inválida (fail closed), nunca se reparan.
- Un fallo de proveedor/red o de presupuesto es infraestructura: bloquea el episodio y se
  excluye de los denominadores y de los pares.
- Los secretos no entran en prompts, trazas, evidencia ni resúmenes (`HnpSettings.public()`).

## Estructura

| Módulo | Contenido |
| --- | --- |
| `config.py` | `HnpSettings` (prefijo `HNP_`), sin valores por defecto para parámetros experimentales; gates de ejecución live |
| `prompts.py`, `prompts/validator-v1.md` | prompt versionado; sha256 sobre bytes exactos (LF) |
| `schemas.py` | `ValidatorDecision`: lo único que el modelo devuelve |
| `transport.py` | `OpenAIResponsesTransport` (único que llega a un proveedor), `BudgetedTransport`, `FakeTransport` |
| `validator.py` | `LlmValidatorAgent`, `review_input`, respondedor pseudoaleatorio del dry-run |
| `cost.py` | precios por millón de tokens y estimación previa (cota inferior y superior) |
| `trace.py` | trazas por episodio (`hnp-llm-trace/1`) con actores, llamadas, tokens y costo |
| `matrix.py` | plan pareado sembrado y ejecución de la matriz |
| `scoring.py` | métricas por brazo, Wilson 95 %, McNemar exacto, desviaciones, resumen `hnp-llm-run/3-draft` |

Comando: `uv run hnp-matrix` (`commerce_evaluation/hnp_matrix.py`). Corpus:
`evaluation/data/hnp/`. Pruebas: `tests/evaluation/hnp/test_llm_*.py`.

## Configuración

Todas las variables están en el bloque HNP de `.env.example` y son obligatorias; si falta una,
el script se niega y las lista. `none` significa "no enviar el parámetro" (p. ej.
`HNP_TEMPERATURE=none`, porque los modelos de razonamiento no aceptan temperatura).

`HNP_LIVE`, `HNP_VALIDATOR_MODEL`, `HNP_TEMPERATURE`, `HNP_REASONING_EFFORT`, `HNP_SEED`,
`HNP_TIMEOUT_SECONDS`, `HNP_MAX_OUTPUT_TOKENS`, `HNP_REPETITIONS`,
`HNP_VALIDATOR_INPUT_USD_PER_MTOK`, `HNP_VALIDATOR_OUTPUT_USD_PER_MTOK`, `HNP_MAX_COST_USD`,
`HNP_VALIDATOR_PROMPT`, `HNP_CASES_PATH`, `HNP_ORACLE_PATH`, `HNP_OUTPUT_DIR`, y para la
Fase 2 (aún sin implementar) `HNP_BUYER_MODEL`, `HNP_BUYER_PROMPT`,
`HNP_BUYER_INPUT_USD_PER_MTOK`, `HNP_BUYER_OUTPUT_USD_PER_MTOK`. La clave va en
`OPENAI_API_KEY`, solo en `.env` o en el entorno.

Los precios de `.env.example` son marcadores; hay que verificarlos antes de cualquier corrida.
`HNP_MAX_COST_USD=0` hace que una corrida live se rechace.

## Cómo correr

```bash
# Plan y estimación de costo, sin llamadas
uv run hnp-matrix --estimate-only --env-file .env.example

# Dry-run: FakeTransport, no abre conexiones (~1 s)
uv run hnp-matrix --dry-run --env-file .env.example

# Live: solo con presupuesto aprobado. Requiere HNP_LIVE=1, OPENAI_API_KEY, precios
# verificados y HNP_MAX_COST_USD >= la cota superior que imprime el plan.
uv run hnp-matrix --env-file .env
```

Si falta cualquiera de esos gates el script termina con código 2 antes de crear el cliente. En
live, `BudgetedTransport` reserva el peor caso de cada llamada y deja de llamar al proveedor
cuando el gasto podría superar el tope. Las salidas (`summary.json`, `traces.jsonl`,
`evidence.jsonl`) van a `HNP_OUTPUT_DIR/<experiment_id>/`, que debe estar bajo `.local/`.
`--no-strict` registra las desviaciones de arnés en vez de abortar.

La corrida dry-run `hnp02-dry_run-20261020T044329Z-s20261022` se conserva como evidencia
histórica en `docs/governance-monitor/sources/hnp02/`, con los SHA-256 de `summary.json` y
`traces.jsonl` en `provenance.json`; ver `docs/governance-monitor/README.md`.

## Diseño de la matriz y métricas

- Casos de negocio: los 19 fixtures con `validator_behavior == "normal"`. Los 8 fixtures que
  simulan un validador defectuoso (falso accept, timeout, malformado, replay...) prueban el
  arnés, no a un revisor, y se excluyen.
- Un par es un caso en una repetición; ambos brazos reciben la misma fuente, payload y
  controles. Pares y orden de brazos se barajan con `HNP_SEED`.
- Por brazo: efectos simulados, escapes (efecto en casos inseguros) y bloqueos falsos (casos
  admisibles bloqueados), con intervalo de Wilson al 95 %.
- Validador (brazo B): capturas (rechazo de propuestas inválidas), capturas con los checks
  esperados, capturas únicas (las que el backend no habría bloqueado), fallos contenidos por el
  backend, rechazos falsos, abstenciones, salidas inválidas y timeouts.
- Comparación pareada A/B con McNemar exacto sobre todos los pares, los admisibles y los
  inseguros. Costo y latencia por llamada y por episodio.
- Desviaciones del oráculo: de arnés (A o backend; nunca esperadas, abortan en modo estricto),
  de infraestructura y del validador (resultado experimental).

Ejemplo de dry-run, abreviado (`.env.example`, semilla 20261022). Las cifras del validador son ruido del
respondedor falso y no dicen nada de ningún modelo:

```text
Plan: 19 cases x 5 repetitions x arms A/B = 190 episodes; seed 20261022
... 90 validator calls; ~180155 input tokens; estimated USD 0.0582-0.4137
Arm A: effects 10, escapes 0/85, false blocks 0/10, infrastructure errors 0
Arm B: effects 6, escapes 0/83, false blocks 4/10, infrastructure errors 2
McNemar A vs B: all p=0.125 (only B blocked 4, only A blocked 0); unsafe p=1
Calls: 90 (0 provider); tokens in/out 176152/7159; USD 0.0584 (fake_transport_estimate)
```

## Amenazas a la validez

- **B ⊆ A por construcción.** Cero escapes en B no es un hallazgo; la ganancia de seguridad
  solo puede venir de `unique_captures`.
- **El prompt replica las reglas del backend.** Con payloads guionados, todo lo que el
  validador captura ya lo bloquea el backend; se espera `unique_captures = 0`. La Fase 1 mide
  sobre todo el costo del validador (bloqueos falsos, salidas inválidas, latencia, dinero). Su
  valor potencial está en lo que el backend no puede comprobar (preferencias en lenguaje
  natural, texto manipulado), que es la Fase 2.
- **Pocos casos admisibles.** De 19 casos, 2 son admisibles y 1 cambia después de la revisión;
  los intervalos de bloqueo falso son anchos. Ampliar casos válidos es la mejora más barata.
- **Repeticiones correlacionadas.** Las repeticiones de un mismo caso no son independientes;
  Wilson y McNemar por episodio sobreestiman la precisión. Reportar también por caso.
- **Sin semilla del proveedor.** La API Responses no acepta seed; `HNP_SEED` fija el orden de
  episodios y las respuestas falsas, no el muestreo del modelo.
- **Estimaciones.** Los tokens previos se estiman con 3 caracteres por token; los tokens en
  caché se cobran a precio completo y los de razonamiento cuentan como salida (cota
  conservadora). El costo real sale del `usage` que reporta el proveedor.
- **Deadline.** El presupuesto del validador y el timeout HTTP valen `HNP_TIMEOUT_SECONDS`; una
  respuesta tardía se descarta, pero su llamada se sigue contando (tokens y costo).

## Cambios al arnés HNP-01 que acompañan este paquete

- El validador corre bajo un deadline real (`deadline.py`); `validator-timeout` en B pasa de
  `unavailable` a `timeout`, con el mismo código `VALIDATOR_UNAVAILABLE`.
- Nuevos códigos de admisión `BUYER_UNAVAILABLE` y `BACKEND_UNAVAILABLE`; las fallas de
  infraestructura ya no se confunden con veredictos.
- El validador recibe vistas públicas (`ProposalView`, `ReviewerContext`); la revisión guionada
  recalcula la elegibilidad desde los candidatos públicos. Los 54 episodios HNP-01 conservan
  códigos, efectos, fallos de backend y checks.
- `binding` se aplica solo en `commit_if_current`; se eliminó la rama muerta del backend.
- La evaluación registra desviaciones del oráculo en vez de abortar (salvo `--strict`) y cuenta
  las llamadas a modelos en lugar de reportar cero.

## Fase 2 (pendiente)

Comprador LLM (`HNP_BUYER_*`, prompt `buyer-v1` aún no escrito) con fixtures que el backend no
puede juzgar: preferencias en lenguaje natural e instrucciones inyectadas en datos de catálogo.
Requiere etiquetas por escenario en el oráculo y añadir títulos o descripciones a los
candidatos, que hoy no existen.
