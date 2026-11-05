# P3 punto 5: validación técnica del protocolo

El arnés validó las puertas offline PV-01–04/09 y los controles live PV-05–08/10 en
el laboratorio fijado. **P3 sigue BLOCKED para ejecutar las cinco celdas UX03.**
La medición no atribuye ninguna celda ni UX05 a la matriz.

| Ejecución | JUnit | Puerta | Alcance |
| --- | --- | --- | --- |
| [42042970689](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42042970689) | 2 passed, 1 failed | false | El proxy bloqueó el GET de descubrimiento ACP y PV-10 devolvió 502 antes de crear checkout. |
| [42043531442](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42043531442) | 3 passed | true | Habilitada solo la lectura ACP necesaria. PV-10 pasó con autenticación JWT/JWKS. |
| [42044099706](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42044099706) | 3 passed | true | Calibración cerrada antes de V y mutación del catálogo administrado aislada. |
| [42044485210](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42044485210) | 3 passed, 0 failed/error/skipped | true | Run terminal con replay y conflicto de update sobre el checkout recuperado. |

La ejecución terminal usó Agent `f68f333ef55ec360d0027fb19423039e8700903e`,
Merchant `abf30df3b6a611284dedcc26c00accac6a853329` y PSP
`e6ab67b5963434a9a3caae2012b70232a3cca65d`. El árbol de producto coincide con
la base P3-V01. El perfil manual ejecutó primero 30 pruebas offline. La verificación
offline dirigida, incluida la suite del prerregistro, dio 67 passed y 89 subtests.

| Control | Evidencia y límite |
| --- | --- |
| PV-01/02/03/04/09 | Validadores rechazan cantidades no enteras, totales no globales, pérdida de identidad, drift con feed igual, sesiones sin reconciliar y sello tardío o alterado. El scorer conserva una decisión adversa. Los cinco casos usados aquí son los del oráculo matemático congelado y solo ejercen el validador. |
| PV-05 | Dos calibraciones independientes, canceladas y selladas antes de que V creara. Catálogo completo de 310 variantes. ACP global en unidades menores COP: A 389.000.000 y B 177.500.000, cada uno idéntico entre calibración y V. Create/update/get coinciden con revisión, contexto y pricing persistido. |
| PV-06 | Las tres operaciones contra una sesión de otro run devolvieron 404 y se rechazó emitir sesión para actor ajeno. E mantuvo su snapshot y feed. El cambio de stock administrado 2→0→2 quedó en su propio run, con la revisión histórica conservada 1→2→3. La instancia HP con SQLite nueva no accedió al run anterior. |
| PV-07 | Un POST de creación con respuesta perdida. Lookup verificó run, actor, clave y huellas. Update con la misma clave devolvió el mismo checkout sin una segunda mutación. Cuerpo diferente con la misma clave devolvió 422. Cancelación con respuesta perdida y replay de la misma clave produjo lectura `canceled`. Para replay/conflicto de create se reutiliza la evidencia [P3-V01](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41750611778). |
| PV-08 | Clones adversos separados bloquearon envío, stock, revisión y reloj en la frontera de expiración. El cambio de envío no alteró el feed reexportado. E conservó su estado. |
| PV-10 | Agent real HP/enforce con JWT/JWKS, un router fijo para una solicitud simple y un checkout preparado. Completar sin aprobación, desde otro dueño o sin JWT devolvió 409/404/401. Cero solicitudes de pago o de completar al Merchant. |

Los seis checkouts del run terminal quedaron cancelados por lectura independiente. El PSP
conservó sus huellas iniciales y no se observaron nuevas órdenes, intentos de completar ni
tokens. Se comprobó el retiro de PostgreSQL y el cierre del proxy. El [índice](runs.json)
conserva los cuatro ZIP originales, los resultados desfavorables, los SHAs, JUnit, manifests,
snapshots, trazas sanitizadas y hashes. Coincidieron cuatro hashes externos y 77 internos.
El verificador mantiene la auditoría registrada. El bloque de autorización evita enviar
operaciones comerciales al PSP durante la prueba HP.

La [nota de integración](../../../docs/design/p3-point5-validation.md) describe el port
offline y la conciliación del workflow/worker de #157/#158/#160 con main. La integración del
protocolo y el producto condicional requiere revisión humana y una nueva comprobación en el
árbol combinado. Faltan la intención tipada de Yair, la lectura vinculada a identidad/revisión
en la ruta de producto, la propuesta previa al scorer y la explicación UX05. Los montos de
esta validación corresponden a controles de contrato. El futuro corpus, los límites por rama
y los oráculos de los episodios reales se sellan en su propio lote antes de observar al Agent.
