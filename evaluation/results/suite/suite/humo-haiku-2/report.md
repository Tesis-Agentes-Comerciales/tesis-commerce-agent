# Reporte de la corrida humo-haiku-2

- Suite capacidad, con 1 intentos por caso y gobernanza, y semilla 1.
- Agente: `claude-haiku-5-5`, esfuerzo `low`.
- Usuario: simulado por `claude-sonnet-5-5`.
- Commit: `2f6ea3dbb81292f89440904c68e7eaf3f16650c1`, sin cambios locales.
- Inicio: 2026-11-13T21:58:49+00:00. Fin: 2026-11-13T21:59:06+00:00. Estado: completa.
- Tope de gasto: USD 2,0000. Gastado: USD 0,0020.
- Lo que dice: solo código, sin el juez.

## Resultado por gobernanza

| Gobernanza | Tareas | Intentos | Pasan | Fallan | Sin decidir | Infraestructura | pass^1 | pass^1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B2 | 2 | 2 | 0 | 1 | 1 | 0 | 0,0 % (0,0 % a 65,8 %) | 0,0 % (0,0 % a 65,8 %) |

pass^1 y pass^k son promedios por tarea, con su intervalo de Wilson al 95 %.

## Por tarea

Intentos: los calificados de los registrados. En cada calificador: pasa/falla/sin decidir.

| Tarea | Gobernanza | Intentos | pass^1 | pass^1 | Efectos | Resultado | Evidencia | Lo que dice |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| prioridad-explicita/delegado | B2 | 1 de 1 | 0,0 % (0,0 % a 79,3 %) | 0,0 % | 1/0/0 | 1/0/0 | 1/0/0 | 0/0/1 |
| prioridad-explicita/presente | B2 | 1 de 1 | 0,0 % (0,0 % a 79,3 %) | 0,0 % | 0/1/0 | 0/1/0 | 0/1/0 | 0/0/1 |

## Calificadores

| Gobernanza | Calificador | Pasa | Falla | Sin decidir |
| --- | --- | --- | --- | --- |
| B2 | Efectos | 1 | 1 | 0 |
| B2 | Resultado | 1 | 1 | 0 |
| B2 | Evidencia | 1 | 1 | 0 |
| B2 | Lo que dice | 0 | 0 | 2 |

## Costo

De `llm-usage.jsonl` y del de `judge/`, en tokens. Una llamada que el tope rechazó cuenta como llamada, con USD 0. Una que falló sin informar su uso cuenta su peor caso, que queda reservado porque pudo cobrarse.

| Rol | Llamadas | Entrada | Salida | Caché leída | Caché escrita | USD |
| --- | --- | --- | --- | --- | --- | --- |
| Agente | 3 | 3.653 | 923 | 0 | 9.071 | USD 0,0020 |
| Total | 3 |  |  |  |  | USD 0,0020 |

## Infraestructura y exclusiones

Intentos repetidos por una falla de infraestructura: 0. Repetidos porque el usuario simulado falló: 0. Excluidos porque el usuario simulado falló dos veces: 0. Ninguno cuenta contra el agente.

## Fallas del modelo del agente

Cuenta el tipo que nombra el producto (`llm/model_failures.py`): `rejected` e `invalid_output` son del agente, y el intento se califica sin repetirse; `provider_error`, `unreachable`, `rate_limited` y `quota_exhausted` son de la infraestructura. `quota_exhausted` detiene la corrida, y también un `rejected` con 401, 403 o 404, o el tercero seguido con la misma etapa y el mismo código; ese intento se repite al retomar. Solicitudes: las que fallaron en `model_requests`, con su etapa, su error, su estado HTTP, el código del proveedor y, si lo guardó, su mensaje.

| Tarea | Gobernanza | Intento | Vez | Tipo | Cuenta contra | Dónde | Solicitudes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| prioridad-explicita/presente | B2 | 1 | guardado | `invalid_output` | el agente | POST /conversation/turn 502 | model.route ValidationError |

## Para revisión humana

1 intentos quedaron sin decidir y no cuentan como acierto.

| Tarea | Gobernanza | Intento | run_id | Calificador | Por qué |
| --- | --- | --- | --- | --- | --- |
| prioridad-explicita/delegado | B2 | 1 | `run_8194968bccba4b9b941ed7033357b1e0` | Lo que dice | Para el juez: 2 criterios del caso |
