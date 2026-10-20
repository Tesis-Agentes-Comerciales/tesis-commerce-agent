# Auditoría técnica de cierre HP/B2 backend

**Resultado: PARTIAL.** Se audita evidencia existente, sin repetir cancelaciones ni ejecutar nuevas pruebas. La demostración funcional acotada está sustentada; no se declara validación formal global ni conformidad de toda la campaña con su autorización.

## Incidencia AUDIT-MERCHANT-CANCEL-01

Test exacto: `tests/integration/test_guarded_lookup.py::test_catalog_and_current_checkout_changes_preserve_history`. Es **un único test, ejecutado dos veces**, no dos pruebas distintas. El [test congelado](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/0dca1083f4b6a7d82c98ed3d81d44ec224501730/tests/integration/test_guarded_lookup.py#L180) crea un checkout condicionado, modifica el catálogo, invoca `CheckoutService(context).cancel` con la clave de fixture `synthetic-cancel` y comprueba que el lookup siga devolviendo la prueba original.

| Ejecución | Sesión observador / fase | Líneas JSONL antes → después | checkout_mutations | checkout_sessions | run_events |
|---|---|---|---|---|---|
| Primera | 2 / guard_lookup_new | 22 → 23 | 0 → 1 | 1 → 2 | 4 → 7 |
| Segunda | 4 / full filtrada | 158 → 159 | 1 → 2 | 43 → 44 | 172 → 175 |

Fuente directa: [extracto de los cuatro snapshots](evidence/cancellation-audit.json); el hash del JSONL original de 230 snapshots está en [catálogo de fuentes](evidence/source-catalog.json). Las diferencias entre teardown incluyen fixture, creación, edición y cancelación: no atribuir los tres eventos completos únicamente a cancel. Los 208 snapshots posteriores con un contador acumulado no nulo no son 208 cancelaciones. Los únicos dos incrementos son +1 y +1 en este test.

Cluster: `mer07a2-283ec644204b`, PostgreSQL 17.2 en loopback puerto 53190; base `tesis_lab`, rol `mer07_app`. Directorio relativo de datos: `sources/merchant/.local/mer07a2-283ec644204b/pgdata`, dentro del área aislada de validación. La identidad coincide antes y después en las dos ejecuciones. Los registros de cierre indican servicio detenido y puerto cerrado. En esta auditoría no se reinició ni se consultó ese cluster.

La operación identificada es cancelación de checkout **mediante servicio Merchant en el mismo proceso**, no un POST de cancelación enviado por Agent o D2. Creación y lookup del test pasan por TestClient/ASGI; no se trata de la pareja de procesos Agent–Merchant por TCP. La [fixture](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/0dca1083f4b6a7d82c98ed3d81d44ec224501730/tests/integration/test_guarded_creation.py#L55) hace `seed(..., variant="B2")` con actor, run y clave de creación nuevos. **La etiqueta B2 existe en esa fixture Merchant**: no sería preciso afirmar que la incidencia es ajena a B2 en todos los sentidos.

Los UUID exactos de run, actor y checkout de las dos operaciones no fueron conservados en los snapshots sanitizados. Se identifica cada operación por cluster, test, sesión/fase, líneas de evidencia y delta. La clave literal anterior es una constante pública del test, no una credencial operativa. No se inventan UUID ni se reabre la base para recuperarlos. Este límite impide una atribución forense fila por fila a partir del paquete publicable.

## Tablas afectadas y fuerza de la evidencia

| Alcance | Tablas | Evidencia / límite |
|---|---|---|
| Cancelación directa | `checkout_sessions`: UPDATE de revisión, estado canceled, snapshot y updated_at | [Código de cancelación](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/0dca1083f4b6a7d82c98ed3d81d44ec224501730/src/commerce_lab/checkout.py#L911) y assert passed; conteo de filas no mide el UPDATE |
| Cancelación directa | `checkout_mutations`: INSERT de operation=cancel y respuesta persistida | Código + incremento medido 0→1→2 |
| Cancelación directa | `run_events`: INSERT `checkout.canceled` | [Registro del evento](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/0dca1083f4b6a7d82c98ed3d81d44ec224501730/src/commerce_lab/checkout.py#L1087); delta global por test +3 incluye otros eventos |
| Preparación del test | `catalog_offers`: seed de ofertas y UPDATE de revisión/snapshot (nombre, revisión, estado inactive) | [Helper mutate](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/0dca1083f4b6a7d82c98ed3d81d44ec224501730/tests/integration/test_guarded_creation.py#L107); aumento medido +310 ofertas por fixture |
| Preparación/creación | `experiment_runs`, `lab_sessions`, `checkout_sessions`, `guarded_checkout_creations`, `run_events` | Aumentos observados por cada test: +1 run, +1 sesión, +1 checkout, +1 prueba guarded, +3 eventos totales |
| Sin nuevos registros observados | `orders`, `checkout_completion_attempts`, `webhook_deliveries`, `webhook_delivery_attempts` | Cero en los 230 snapshots. No equivale a captura exhaustiva de tráfico de todo el host |

La prueba original se preservó según el assert final del test. Los snapshots no son un volcado de filas ni un log de cada sentencia; no permiten declarar ausencia de toda escritura transitoria imaginable. El código del camino cancel no invoca completion, confirmación PSP ni creación de orden.

## Frontera y adjudicación

**Hecho comprobado:** hubo dos mutaciones de cancelación en el PostgreSQL descartable de la suite Merchant; el pytest pasó. **Incumplimiento:** la autorización experimental excluía cancelaciones. Que la base fuese aislada no vuelve autorizadas estas operaciones ni permite aprobar el gate histórico.

**Conclusión acotada:** la atribución de ambas cancelaciones queda localizada en la suite Merchant y su fixture sintética B2. No hay evidencia de que hayan sido continuación de una decisión Agent/D2, de que hayan tocado sus SQLite, otra base operacional, PSP, pagos u órdenes. Las ejecuciones Agent TCP y D2 enlazadas usaron clusters distintos. Sus contadores y registros de integridad están separados en [effects-extract.json](evidence/effects-extract.json). Esto sustenta confinamiento observado, no una garantía global obtenida de captura de paquetes o auditoría de cada sentencia.

La explicación compatible con la evidencia es un filtro de selección insuficiente: el nombre `preserve_history` no revela la llamada interna a cancel. No se presenta como fallo funcional del producto ni se borra del historial de cobertura; se presenta como incidencia de conformidad de ejecución. Se excluyó de la fase posterior. Su aceptación o cierre corresponde a revisión independiente humana, no a esta auditoría.

## Resultados e integridad

- Merchant: 393 únicos recolectados; 115 previos +134 nuevos =249 ejecutados únicos. Dentro de los 115 hay un test con esta incidencia. No se suman repeticiones entre fases. Los dos bootstrap de migraciones repetidos no agregan cobertura. Retenidos: 144; no se reclasifican como passed ni como skips de pytest.
- Fase Merchant posterior: fresh 2 passed; full filtrada 135 passed /1 skipped /2 warnings; cero fallos de producto. El skip de fresh-volume ya había pasado sobre la base recién creada. Dos intentos de helper fallaron antes de ejecutar tests por representación de ruta Windows; se corrigió solo el ejecutor temporal.
- Agent: seis casos previos de TCP/PG reales, dos casos enlazados D17→D2 y tres casos finales nuevos. Ninguno se repitió en esta auditoría. D2: 54 tests de componente sintéticos; la cadena enlazada usa D17 real y D2 ASGI con identidad de prueba.
- En los tres casos finales: cero POST D17 nuevos en ambos registros de transporte y Merchant; cuatro lookups y seis contadores de efectos en cero. No extrapolar ese cero a toda la campaña: la campaña previa tuvo las dos cancelaciones aquí documentadas.

Se verificaron nuevamente los manifiestos de 64 y 124 artefactos, el inventario de 393 nodeids y los 24 escenarios. El nuevo sellado de preparación compara los 29.555 archivos del baseline y los metadatos Git de 12 árboles; el resultado final está en [preparation-integrity.json](evidence/preparation-integrity.json). El alcance excluye .env, caches de dependencias y archivos PG históricos brutos; no afirma sellado binario completo de .git ni del host. Las únicas nuevas escrituras son documentos, extractos y helpers de empaquetado dentro del área aislada.

Se conservan los incidentes de preparación ya registrados: intento inicial D2 sin tests por ruta fuera del límite; dos intentos Merchant sin tests; normalización LF→CRLF de una copia durante empaquetado, corregida a bytes idénticos sin repetir pruebas. No se confunden con fallos del producto ni se eliminan sus antecedentes.

## Dictamen para la revisión independiente

Mantener **PARTIAL**. Se puede presentar la demostración funcional HP/B2 backend con las variantes medidas y sus límites. No declarar validación formal de todo HP/B2, ejecución histórica íntegramente conforme, compra completa, UI real o cierre de I10/D08/D09 generales. No ejecutar más efectos comerciales para intentar corregir retrospectivamente esta incidencia.
