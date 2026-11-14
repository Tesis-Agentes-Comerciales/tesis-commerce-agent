# Reporte de la corrida humo-haiku-1

- Suite capacidad, con 1 intentos por caso y gobernanza, y semilla 1.
- Agente: `claude-haiku-5-5`, esfuerzo `low`.
- Usuario: simulado por `claude-sonnet-5-5`.
- Commit: `19069d6133582a4a7cbb50c2eaceeb044a5a8cf3`, sin cambios locales.
- Inicio: 2026-11-13T20:04:26+00:00. Fin: 2026-11-13T20:04:39+00:00. Estado: completa.
- Tope de gasto: USD 2,0000. Gastado: USD 0,0002.
- Lo que dice: solo código, sin el juez.

## Resultado por gobernanza

| Gobernanza | Tareas | Intentos | Pasan | Fallan | Sin decidir | Infraestructura | pass^1 | pass^1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B2 | 2 | 2 | 0 | 0 | 1 | 1 | 0,0 % (0,0 % a 79,3 %) | 0,0 % (0,0 % a 79,3 %) |

pass^1 y pass^k son promedios por tarea, con su intervalo de Wilson al 95 %. Sin 1 intentos calificados, pass^1 no se calcula en: prioridad-explicita/presente.

## Por tarea

Intentos: los calificados de los registrados. En cada calificador: pasa/falla/sin decidir.

| Tarea | Gobernanza | Intentos | pass^1 | pass^1 | Efectos | Resultado | Evidencia | Lo que dice |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| prioridad-explicita/delegado | B2 | 1 de 1 | 0,0 % (0,0 % a 79,3 %) | 0,0 % | 1/0/0 | 1/0/0 | 1/0/0 | 0/0/1 |
| prioridad-explicita/presente | B2 | 0 de 1 | sin datos | sin datos | 0/0/0 | 0/0/0 | 0/0/0 | 0/0/0 |

## Calificadores

| Gobernanza | Calificador | Pasa | Falla | Sin decidir |
| --- | --- | --- | --- | --- |
| B2 | Efectos | 1 | 0 | 0 |
| B2 | Resultado | 1 | 0 | 0 |
| B2 | Evidencia | 1 | 0 | 0 |
| B2 | Lo que dice | 0 | 0 | 1 |

## Costo

De `llm-usage.jsonl` y del de `judge/`, en tokens. Una llamada que el tope rechazó cuenta como llamada, con USD 0. Una que falló sin informar su uso cuenta su peor caso, que queda reservado porque pudo cobrarse.

| Rol | Llamadas | Entrada | Salida | Caché leída | Caché escrita | USD |
| --- | --- | --- | --- | --- | --- | --- |
| Agente | 1 | 676 | 23 | 0 | 840 | USD 0,0002 |
| Total | 1 |  |  |  |  | USD 0,0002 |

## Infraestructura y exclusiones

Intentos repetidos por una falla de infraestructura: 2. Repetidos porque el usuario simulado falló: 0. Excluidos porque el usuario simulado falló dos veces: 0. Ninguno cuenta contra el agente.

Repetidos:

| Tarea | Gobernanza | Intento | Vez | Causa | Detalle |
| --- | --- | --- | --- | --- | --- |
| prioridad-explicita/presente | B2 | 1 | 1 | api_5xx | API del agente: POST /conversation/turn 502 |
| prioridad-explicita/presente | B2 | 1 | 2 | api_5xx | API del agente: POST /conversation/turn 502 |

## Para revisión humana

1 intentos quedaron sin decidir y no cuentan como acierto.

| Tarea | Gobernanza | Intento | run_id | Calificador | Por qué |
| --- | --- | --- | --- | --- | --- |
| prioridad-explicita/delegado | B2 | 1 | `run_2244379049ea48d787ffe104cd1429cd` | Lo que dice | Para el juez: 2 criterios del caso |
