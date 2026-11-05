# P0: conciliación de EXP-01 a EXP-05

El [registro observado](matrix-observed.json) enlaza escenarios y modos con los ZIP originales
versionados. Cada modo tiene una ejecución primaria, alcance y limitación. El índice de runs
incluye los intentos anteriores, el resultado original de JUnit, la puerta, los SHAs efectivos
y la referencia al protocolo y al índice de hashes existente. En el preflight bloqueado de
EXP-01 solo se conoce el SHA del Agent que lanzó Actions. Merchant y PSP quedan sin verificar.

`matrix.json` conserva la cobertura declarada y los modos propuestos. Sus 33 escenarios contienen
56 pares escenario–modo. El contador histórico de 37 unidades sustituye UX03 por sus cinco celdas
y no desglosa HP/HNP. Los dos denominadores se conservan por separado.

| Estado observado | Escenarios distintos | Pares escenario–modo |
| --- | ---: | ---: |
| `passed` dentro del alcance medido | 5 | 6 |
| `partial` con evidencia positiva de una fase | 3 | 3 |
| `failed` del escenario | 0 | 0 |
| `blocked` con medición incompleta | 1 | 1 |
| `harness_error` como resultado primario | 0 | 0 |
| Sin medición en EXP-01–05 | 24 completamente pendientes | 46 |

Los 46 pares pendientes incluyen INF30 HNP e INF31 HNP, aunque ambos tienen medición HP.
En total hay 26 escenarios con al menos un modo pendiente. Los fallos de arnés, de publicación
y el preflight bloqueado conservan sus JUnit y atribución originales. EXP-01 terminó con
`validated=false` por INF31 y su informe final atribuye de forma independiente MA24 e INF30.
AUTH-TRANSPORT valida infraestructura y queda fuera del conteo P0.

## Resumen para Notion

| Escenario | Modo | Resultado | Alcance y limitación | GitHub Actions |
| --- | --- | --- | --- | --- |
| CT13 | HNP | Parcial | Variante agotada tras consentimiento, 409 sin checkout. Falta fallback a una variante autorizada. | [EXP-03](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41897313836) |
| CT14 | HP | Parcial | Stock revalidado tras preparar checkout. Falta probar el intento de completar. | [EXP-02](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41895755779) |
| CT15 | HP | Aprobado en alcance | Total actualizado por Merchant. Preparación dentro del máximo y cancelación por exceso. Sin aprobación ni compra completada. | [EXP-02](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41895460657) |
| CT15 | HNP | Aprobado en alcance | Guarda de presupuesto con feed antiguo. El caso dentro del máximo termina por revocación de seguridad tras preparar checkout. | [EXP-05](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41913671886) |
| CT16 | HP | Parcial | Envío modificado e invalidación de términos. Faltan términos nuevos, reaprobación y prueba de complete. | [EXP-03](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41897599243) |
| MA22 | HNP | Aprobado en alcance | Revocación y nuevo consentimiento por API. Escalado por presupuesto sin crear checkout. Sin edición in-place. | [EXP-04](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41898998656) |
| MA23 | HNP | Aprobado en alcance | Nuevo grant solo para Y, con X asequible fuera del alcance. Escalado sin compra. Sin edición in-place. | [EXP-04](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41899701762) |
| MA24 | HNP | Aprobado en alcance | Revocación efectiva antes del trigger, 409 y cero nuevos efectos. Medición por API. | [EXP-01](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41846571407) |
| INF30 | HP | Aprobado en alcance | Pérdida de respuesta tras creación aceptada, reconciliación del mismo checkout y sin segundo POST. Sin evaluar toda la UX de recuperación. | [EXP-01](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41846571407) |
| INF31 | HP | Bloqueado | Cinco controles aprobados. El fallo de PostgreSQL devolvió 500 nativo en lugar de 503, y Agent quedó uncertain. | [EXP-01](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41846571407) |

La cobertura declarada en P0 procede de auditoría de código y pruebas históricas. Por ejemplo,
MA24 figura como `existing_test`, mientras que MA22 y MA23 figuraban como `design` y hoy tienen
evidencia HNP dentro del contrato de revocar y volver a consentir. UX01, UX11 y UX12 conservan
semántica pendiente aunque el transporte autenticado aprobó. Ninguna de estas mediciones acredita
una compra completada.

## Validación y conciliación con CT19

Desde la raíz, `uv run python -m commerce_evaluation.p0_observed` verifica los archivos originales
y lista los 46 pares pendientes. `uv run pytest tests/evaluation/test_p0_observed.py` verifica el
registro y rechaza atribuciones a un modo incorrecto, infraestructura, cambios de hash o promoción
de un bloqueo.

CT19 HP tiene evidencia separada en [#171](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/pull/171).
Mientras ese PR esté pendiente, el registro de EXP-01 a EXP-05 no incorpora esa fila. Ambos PR
crean `matrix-observed.json`, por lo que al integrar el segundo habrá que resolver el alta doble
con una unión por escenario y modo. Conservar este índice de EXP-01 a EXP-05 y añadir una sola
fila CT19 HP desde #171, con referencia a su índice y ZIP originales. Mantener CT19 HNP
`not_executed` y conservar el control de stock fuera de P0. Adaptar en esa integración la
validación al índice de CT19 y comprobar el nuevo conteo por modo. Esta conciliación es
independiente de #171 y no modifica los insumos congelados.
