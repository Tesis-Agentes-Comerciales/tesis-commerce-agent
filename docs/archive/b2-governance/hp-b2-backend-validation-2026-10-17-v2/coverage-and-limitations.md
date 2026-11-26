# Cobertura, variantes y gates

Estado global: **PARTIAL**. [Resultados](evidence/results.json), [gates conservados](evidence/gates.json), [matriz completa](scenario-matrix.csv).

## I05, I06, I09 e I10

| ID | Variante realmente probada | Evidencia y resultado | Garantía no medida |
|---|---|---|---|
| I05 | Política deny por merchant no permitido, inyectada por constructor público del worker temporal antes de initialize | [deny](evidence/http-deny.json): D1 real `enforce/deny/blocked`; sin guard, claim ni envío | No exhaustividad de todas las reglas, obligaciones, fallos de PDP, rutas de bypass o configuraciones productivas |
| I06 | Q shadow allow frente a decisión aplicada deny independiente | Mismo caso I05: Q no confiere autoridad, decide D1; no es un segundo test | No prueba todas las divergencias shadow/enforce ni un runtime público de políticas |
| I09 | Claim durable; caída controlada en create_once antes del envío; reinicio y dos lookup TCP/PG not_found; dispatch posterior bloqueado | [not-found](evidence/http-not-found.json): cero POST; not_found no permite retry | No equivale a respuesta perdida después de creación procesada por Merchant. Ese caso distinto está en I07/I11; tampoco cubre particiones o transacciones remotas sin commit |
| I10 | Un byte hexadecimal de HMAC de evento alterado en applied.sqlite nueva, después de cerrar Agent; schema/triggers intactos | [corrupt-history](evidence/http-corrupt-history.json): integrity_check ok, lookup real y rechazo local D17_HISTORY_CONFLICT; sin reemplazo/POST | No prueba toda historia remota corrupta, wire malformado, Merchant unrecoverable, rollback, borrado o manipulación general; **ID amplio sigue PARTIAL** |

Los tres casos compartieron PostgreSQL descartable y usaron procesos Agent reiniciados y TCP Merchant real para las lecturas correspondientes. El deny demuestra enforcement local genuino; no se lo considera una simulación basada únicamente en omitir el transporte. Un hook adicional bloquearía un POST prohibido, pero no se invocó: no fue ese hook el que produjo el deny. Hubo cuatro lookups reales entre los casos not_found y corrupt-history; cero POST D17 en los contadores independientes de HTTPX y Merchant y cero efectos comerciales medidos. [Resumen](evidence/http-results.json).

La corrupción local falló cerrado al registrar el resultado del segundo lookup. Por eso no se afirma que la corrupción impidiera toda lectura HTTP: sí hubo consulta histórica de solo lectura antes del conflicto local. Solo se tocó una SQLite descartable nueva; no historias previas, fuente P, Q o registry.

## Clases de evidencia

| Clase | Qué incluye | Qué no acredita |
|---|---|---|
| Unitaria / contrato congelado | Validación de modelos, esquemas, errores y políticas; Merchant desglosado por nodeid/kind | HTTP TCP, PG real o conformidad completa con un estándar |
| Sintética ASGI / MockTransport | Componentes D2 54 passed; parte de Merchant; consentimiento backend con principal sintético | UI vista por humano, IdP real, red TCP entre D2 y cliente |
| PG real con llamada directa o ASGI | Tests Merchant opt-in, fixtures/SQL reales; cuatro funciones nuevas de catálogo más bootstrap; inventario detalla kind | Dos servicios separados por TCP solo por llamarse integration o real_http |
| Agent–Merchant HTTP TCP + PG + SQLite | Seis casos previos, dos D17 enlazados, tres casos finales; seis +dos POST históricos, cero en últimos tres | Exactly-once distribuido general, carrera simultánea de Agents, reintento comercial autorizado |
| D17 real → D2 sintético enlazado | D17 aceptado/rejected reales; aceptación, rechazo, expiración y recuperación de recibos por ASGI | Identidad humana real, compra ejecutada, pago o continuación |
| No ejecutada | 144 Merchant; I10 general, D08/D09 generales; compra completa/UI/HNP | No se convierte en pass ni en fallo de producto |

No sumar estas clases: varios tests usan más de una tecnología. Los estados D2 `PASS_LINKED_SYNTHETIC_ASGI` de esta matriz aclaran una etiqueta anterior `PASS_REAL_INTEGRATION`, conservando intacta la matriz original en su paquete sellado. El enlace D17 es real; el humano y el transporte D2 siguen siendo sintéticos.

D08: principal diferente/inválido en cadena enlazada; owner/run ajenos solo en componente. D09: D17 rejected real bloquea presentación; D17 ausente/corrupto solo en componente. I08 fue segundo proceso secuencial, no carrera simultánea. I12 mide una fila claimed y un POST, no número de instrucciones SQL claim. La expiración D2 usa reloj inyectado.

## Gates que pueden presentarse como aprobados dentro del perímetro

- A: precondiciones del run acotado.
- B compatible: los 134 tests nuevos Merchant y bootstrap requeridos; no suite irrestricta de 393.
- C: variantes concretas medidas, incluidos I05/I06/I09; variante local I10 aprobada, categoría general pendiente.
- D: consentimiento backend sintético enlazado con D17 real; D08/D09 generales parciales.
- E actual: integridad y ausencia de efectos prohibidos en la fase final autorizada.

**No aprobado:** conformidad histórica E por las cancelaciones. **No declarado:** validación formal general HP/B2 backend hasta revisión independiente de esa incidencia y aceptación explícita de límites. **Sustentado:** demostración funcional experimental backend acotada. **No validado:** compra gobernada completa, completion/órdenes/pagos B2, UI humana real, HNP y comparación experimental B0/B2 completa.

No hay motivo para repetir los casos ya aprobados en esta auditoría. La revisión propuesta es estática: integridad, coherencia, atribución y sanitización de evidencia existente.

## Inventario Merchant por naturaleza técnica

Los retenidos se clasifican por el tipo que tendría su ejecución según el código, sin afirmar que se hayan ejecutado. Cada nodeid figura una vez.

| kind | Ejecutados únicos | Retenidos | Total |
|---|---:|---:|---:|
| UNIT_FROZEN_CONTRACT | 24 | 0 | 24 |
| POSTGRESQL_REAL_API_ASGI_OR_DIRECT_SERVICE | 86 | 133 | 219 |
| REAL_HTTP_TCP_POSTGRESQL_MERCHANT_ONLY | 2 | 0 | 2 |
| UNIT_CHECKSUM | 1 | 0 | 1 |
| UNIT_PURE_OR_MODEL_CONTRACT | 122 | 0 | 122 |
| SYNTHETIC_ASGI_IN_MEMORY | 12 | 8 | 20 |
| UNIT_MOCK_TRANSPORT | 2 | 3 | 5 |
| **Total** | **249** | **144** | **393** |

La columna ejecutados conserva el test histórico con incidencia. PG real no equivale a dos procesos HTTP TCP. Los dos casos Merchant TCP de esta tabla pertenecen a la campaña histórica y no son nuevos casos Agent.

## Interpretación de commercial_posts en D2

En las filas D01–D12 de `scenario-matrix.csv`, `commercial_posts=0` mide **cero POST comerciales adicionales iniciados por D2**, durante la fase de consentimiento posterior al D17 enlazado. No mide ausencia de POST D17 anteriores, ni el total de operaciones de toda la campaña.

Los dos casos enlazados ya habían efectuado sus POST D17 en operaciones distintas antes de evaluar D2: uno aceptado y otro rechazado. En el aceptado, D2 operó sobre la prueba D17 existente; en el rechazado, no se habilitó presentación ni decisión D2. Esos POST previos siguen documentados en `evidence/prior/linked-positive.json`, `evidence/prior/linked-rejected.json` y `evidence/prior-results-extract.json`. No deben sumarse repetidamente por cada fila D2 ni interpretarse como ceros retrospectivos. Los valores de la matriz no se modificaron.

La ausencia de continuación se refiere al backend sintético por ASGI y a la ventana medida, con identidad de prueba. No acredita UI humana real ni compra gobernada completa, ni cambia el dictamen global o las coberturas parciales.
