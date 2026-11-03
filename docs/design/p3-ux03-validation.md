# P3 — Puerta y pruebas mínimas propuestas

Vinculada al [protocolo](p3-ux03-protocol.md) y [dependencias](p3-ux03-dependencies.md).
**Propuesta, sin nuevas pruebas implementadas ni ejecutadas con servicios.**
Una prueba del validador confirma su contrato; la equivalencia requiere además evidencia real
de Merchant/PostgreSQL desechable y sus efectos. No ejecutar UX03 para validar su propio oráculo.

## Puerta de aceptación del protocolo

| Criterio | Evidencia concreta exigida | Prueba adicional mínima / rechazo |
| --- | --- | --- |
| Totales A/B provienen de Merchant | Respuestas HTTP create/update/get con total global/moneda/envío, ID/revisión, correlacionadas con snapshots independientes del worker Merchant | PV-01/PV-05; rechazar feed/scorer como fuente, desglose inconsistente o respuesta sin vínculo al run |
| Calibración y evaluación no comparten efectos | Baselines completos E y Agent antes/después de A-cal/B-cal/V; mapping de run/actor/session alias/SQLite/thread/cache y claves distintas; inventario igual | PV-02/PV-06; un checkout/evento ajeno, escritura inesperada o reutilización bloquea |
| Cada checkout registrado y cancelado | Ledger de intentos HTTP y sesiones creadas, cancel y readback `canceled`, cotejo independiente; cero órdenes/tokens/pagos y ofertas inalteradas | PV-03/PV-07; timeout no reconciliado, cancel sin readback o sesión extra bloquean aunque el test de selección pase |
| Precio/stock/envío/revisiones comparables | Offer completo before/after por rama/run, reloj/validez/config, total y opción semántica; igualdad exacta con excepciones de identidad enumeradas | PV-02/PV-05/PV-08; shipping +1 con feed igual, revisión distinta o expiración bloquean |
| Equivalencia no demostrable bloquea | Registro terminal `BLOCKED`, causa estructurada, evidencia incompleta preservada, ninguna llamada Agent E | PV-04/PV-08; probar datos ausentes y mismatch de un solo campo; no omitir ni degradar a advertencia |
| Arnés no decide por Agent | Entrada natural y feed completo, propuesta registrada antes de scorer; separación de expected/calibraciones y runtime; contador de quién inicia cada checkout | PV-04/PV-09; propuesta adversa del doble permanece incorrecta y falla, sin ser sustituida por A/B esperado |
| Oráculo congelado antes del resultado | Diseño inicial y manifiesto final con hashes/versiones, firma de revisión documental y secuencia de eventos sello → primer turno → propuesta → scoring | PV-04/PV-09; modificar un byte o sello tardío invalida lote, sin reetiquetar resultados |

Para abrir la puerta, todas las pruebas requeridas deben estar ejecutadas con la evidencia
que corresponde a su capa, cero skips requeridos y limpieza confirmada. El revisor humano
acepta el informe de validación antes de implementar escenarios completos. Un fallo de producto
posterior no se arregla debilitando esta puerta. `not_executed` y `BLOCKED` no son passed.

## Pruebas offline propuestas (sin servicios ni modelos)

Ubicación futura: `tests/evaluation/`, código experimental en `evaluation/src/commerce_evaluation/`.
TDD en el siguiente incremento aprobado; aquí no se añade código de producto ni tests artificiales.

| ID | Caso y controles | Resultado que debe distinguir |
| --- | --- | --- |
| PV-01 | Dos respuestas ACP congeladas con procedencia; total global distinto del de línea; shipping no cero. Negativos: falta total, suma inválida, float/bool, moneda/cantidad errónea, fuente scorer | Aceptar solo monto autoritativo trazable en enteros; nunca recalcularlo como sustituto de respuesta. Fixtures siguen siendo sintéticas. |
| PV-02 | Equivalencia exacta por Offer/reloj/config. Cambiar de a uno stock, revisión, shipping, precio, condición, TTL, destino, modo; mantener feed/IDs iguales en controles adversos | Cada diferencia relevante bloquea; solo diferencias de identidad enumeradas son aceptables y mapeadas. |
| PV-03 | Ledger de creación/cleanup: dos sesiones, replay de una, ID adicional, resultado perdido, cancel error/readback fallido, fallo del ejercicio seguido de cleanup exitoso o fallido | Contar intentos vs sesiones lógicas; éxito solo tras cleanup; conservar fallo original y fallo de limpieza. |
| PV-04 | Máquina de fases: diseño → calibración → limpieza → equivalencia → sello → turno → scoring. Fallan transición prematura, unknown convertido en false y sello tardío | Bloquear ejecución dependiente; resultado del scorer no se usa para selección. |
| PV-09 | Sello/independencia: alterar conversación/oráculo/config; doble de selector elige rama equivocada; catálogo contiene C; límites T-1/T/T+1 con presupuestos independientes | Cambio de byte invalida; decisión equivocada no se corrige; cinco celdas y controles dan etiquetas previstas. No demuestra comprensión LLM. |

## Validación propuesta con servicios reales (autorización posterior)

Ubicación futura: `tests/integration/`, LiveStack/PurchaseLane y worker Merchant existente,
cluster PostgreSQL desechable, refs fijados. Sin imports Merchant en Agent, sin DSN externo,
sin LLM ni órdenes. No se despacha en esta unidad. Reutilizar nightly existente.

| ID | Preparación y estímulo | Evidencia y aceptación |
| --- | --- | --- |
| PV-05 | A-cal y B-cal con catálogo completo equivalente; create/update/get por HTTP. V crea A y B secuencialmente, los cancela; E permanece sin turno | Dos totales por variante iguales entre calibración y V, con envío, revisiones/precios/stock/destino/clock iguales; E sin efectos. Cada sesión cancelada, no borrar filas. |
| PV-06 | Credenciales de otro run/actor intentan GET/update/cancel de sesión ajena; reinicio Agent con base nueva; cambio de catálogo administrado con clones preservados | Acceso ajeno denegado, sesión propia inalterada, historial/caché/idempotencia aislados, E y otras ofertas inalterados. Snapshot/control en worker, no DB desde producto. |
| PV-07 | Repetir create/update/cancel con misma key; contenido conflictivo; perder respuesta después del commit; fallo de cancel y reintento acotado | Una creación lógica, conflicto conserva identidad, incertidumbre reconciliada; limpieza fallida bloquea. No POST extra a ciegas, cero tokens/órdenes/pagos. |
| PV-08 | Cambiar shipping +1/revisión/stock en V antes de preparar; otro control sin cambio; expiración exacta con reloj controlado | Detectar cada no equivalencia aunque el feed coincida. Mutación no toca A-cal/B-cal/E; restauración deja revisión nueva y exige nuevos clones/calibración. |
| PV-10 | Agent real con router guionado declarado, caso simple de preparación HP; petición de completar sin aprobación y de otro comprador autenticado; no clic positivo ni instrumento | Rechazo antes de crear token/pago/orden, scope correcto, checkout cancelado y verificado. Comprueba mecanismo HP y conexión; no valida selección condicional ni UI humana. |

Extensiones posteriores necesarias: control de shortlist con A/B visibles y fallo dirigido a
verificación A (UNKNOWN); observación de propuesta Agent sin resolverla desde el arnés;
revisión humana de explicación UX05; corpus natural sellado y repeticiones/modelo con costo
autorizado para afirmar interpretación LLM. Solo después, si se autoriza, recorrido HP con
clic real y compra sandbox para validar aprobación/frescura/completar. No sumar estas pruebas
del protocolo como episodios UX03 ni como nuevas observaciones de la matriz.

## Verificación de esta unidad documental

Se revisan enlaces locales, referencias a símbolos y hashes de fuentes P2 en blobs fijados,
integridad SHA256SUMS del cierre P2, JSON de matriz/oráculo, invariancia de archivos previos,
diff y tests offline P1 existentes. No se tocan Python, auth, UI, lock ni workflows:
sus suites completas quedan cubiertas por el CI del PR documental; no se presenta eso como
validación PV-05–10. Los comandos y resultados efectivos se informan en el PR y la entrega,
sin anticipar PASS de checks aún pendientes.

Comprobaciones locales ejecutadas sobre la base `ad86bdd` más estos tres documentos:

- `python -B -m unittest discover -s tests/evaluation -p test_scenarios_p0.py -v`, con
  `PYTHONPATH=evaluation/src`: 37 tests recogidos, **36 passed y 1 skipped**. El omitido
  requiere pytest del entorno bloqueado de este checkout; no se instaló ni reutilizó otro
  entorno. Esta prueba debe ejecutarse en CI con `uv sync --frozen --all-groups`.
- Comprobación offline con biblioteca estándar: 19 enlaces locales resueltos en los tres
  documentos; siete hashes de fuentes Merchant contra sus blobs del SHA fijado; 28 miembros
  de SHA256SUMS P2 retenidos coinciden byte a byte; matriz y oráculo JSON válidos.
- `git diff --check` sin errores. Solo se agregan estos tres documentos; los archivos
  obligatorios y resultados P1/P2 permanecen intactos. La comprobación se repite tras staging.

Estos resultados verifican documentación, integridad y contratos P1; **no ejecutan PV-01–10**
ni abren la puerta experimental. El CI del PR aporta las suites completas existentes.
