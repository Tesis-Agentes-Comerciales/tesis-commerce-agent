# CT19 HP: desaparición real de la variante

**Resultado validado** para una selección explícita de SKU en B2 HP. El Merchant real rechazó la
creación con `404 invalid_item` tras desaparecer la identidad. Un episodio independiente conservó
la misma identidad con stock cero y obtuvo `409 out_of_stock`. El stock es un control y se contabiliza
solo CT19 HP como escenario P0. HNP queda `not_executed`.

| Ejecución | JUnit | Puerta | Observación |
| --- | --- | --- | --- |
| [41967362290](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41967362290) | 1 failed | `false` | Error del arnés al consultar el proxy PSP de HNP desde la lane HP. |
| [41967635493](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41967635493) | 1 passed | `true` | Retirada y restauración exacta en la prueba previa del estímulo. |
| [41968306631](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41968306631) | 2 error | `false` | Fixture de pytest ausente. Ambos controles quedaron `not_executed`. |
| [41968564766](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41968564766) | 2 failed | `false` | Ambos rechazos nativos constan en el artefacto. El cuerpo y el `finally` fallaron al exigir igualdad de `request_observations`. |
| [42033289049](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42033289049) | 2 passed | `true` | CT19 HP y control independiente de stock. Cero failed, error y skipped. |

La medición terminal ejecutó Agent `4fbf19104175786874666042dd4ba311ba7ac4b7`, Merchant
`abf30df3b6a611284dedcc26c00accac6a853329` y PSP `e6ab67b5963434a9a3caae2012b70232a3cca65d`.
En ambos episodios el catálogo dio `BBiPhone12mini-64GB`, revisión 1, 11 unidades y un total de
40.500.000 unidades menores COP, incluidas 2.500.000 de envío. D1 permitió la variante antes de la
mutación, con el feed original intacto. Cada episodio produjo un solo POST, sin sustitución.
El Agent conservó la variante seleccionada, registró `failed` con el código nativo y dejó
`definitive_failure / VERIFIED_NOT_CREATED` en el registro de despacho.

La restauración recuperó la fila completa, el hash del catálogo, las restricciones y los triggers.
El estado comercial del Merchant volvió exactamente al estado inicial y el PSP quedó sin cambios.
Hubo cero checkouts aceptados, aprobaciones, órdenes, tokens y pagos. Cada caso conservó una sola
observación de creación correlacionada por `Request-Id`, run, actor y hash de idempotencia, y dos
eventos `offer.read` correlacionados con sus respuestas. La limpieza no alteró la auditoría y se
confirmó el retiro de PostgreSQL y el cierre del proxy.

[El índice de evidencia](runs.json) vincula cada run y artefacto a un único ZIP original en Git,
independiente de la caducidad en Actions. Incluye SHAs, hashes, JUnit, resultados de la puerta y
limpieza. Los ZIP contienen también los reportes completos, entradas congeladas, observaciones y
logs originales. Se comprobaron los cinco hashes externos y los 84 hashes internos declarados.
Las copias expandidas se retiraron del diff tras comprobar igualdad de bytes con el ZIP. Los
commits que las publicaron siguen en la historia.

[El estado observado de la matriz](../../scenarios/p0/matrix-observed.json) registra CT19 por modo.
`matrix.json` sigue siendo el insumo congelado que exige el preflight histórico. La evidencia cubre
desaparición de identidad en un episodio HP con SKU explícito. La selección conversacional, un
cambio de identificador y HNP quedan fuera de esta medición.
