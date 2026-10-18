# Clasificación de los 144 tests retenidos

Son 144 **nodeids únicos no ejecutados por restricciones**; no 144 fallos ni 144 skips. El [CSV individual](merchant-retained-144.csv) incluye identificador exacto sanitizado, hash del nodeid original, grupo, razón y enlace al código congelado con línea. La clasificación es documental; no se ejecutó ni se recolectó pytest de nuevo.

| Grupo | Cantidad | Restricción y relevancia |
|---|---:|---|
| R1: creación/recovery/lookup ACP legacy | 70 | 32 en test_acp_creation_lookup y 38 en test_acp_creation_recovery. Fixtures/líneas de creación pública no condicionada, replay de POST y recovery legacy; dos variantes incluyen cancel. Aportarían regresión legacy, aislamiento e inmutabilidad, pero no son condición suficiente ni necesaria para demostrar la cadena D17 acotada ya medida |
| R2: ciclo de checkout ordinario | 29 | acp_http 15, checkout 10, AP2 integración 1, catálogo administrado 1, perfil P0 2. Preparación ordinaria, update/cancel/readiness y firma; fuera de la operación D17 y del presupuesto de transporte autorizado. Relevantes para ciclo comercial completo futuro |
| R3: compatibilidad guarded/público | 7 | guarded_creation 5, guarded_lookup 2. Cruzan rutas públicas, replay y serialización con guard/historia ordinaria. Serían evidencia adicional contra bypass y regresión legacy; siguen sin ejecutarse dentro de esta campaña |
| R4: completion, órdenes y webhooks | 27 | completion 10, webhook_delivery 17. Los helpers preparan/completan checkout y generan órdenes/outbox; muchas respuestas de proveedor y webhook se simulan. Excluidos por efectos y alcance, aunque no impliquen dinero real. Fuera de la demostración backend previa a continuación comercial |
| R5: AP2 completion / cliente PSP simulados | 4 | AP2 unitario 1 y payment_client 3. Stub/MockTransport; la confirmación de pago es sintética. Retenidos por semántica y alcance, no porque se haya demostrado contacto con un PSP real. No prueban interoperabilidad financiera |
| R6: fronteras API con stubs o 401 | 7 | Dos tests de autenticación/observabilidad y cinco parámetros de versionado con StubCheckout (create/get/update/cancel/complete). Conservadoramente fuera de la selección de rutas; no necesitan por ello siete efectos comerciales reales |
| **Total** | **144** | **70+29+7+27+4+7** |

Los grupos son excluyentes como inventario; las restricciones no lo son. R1/R2 pueden contener cancelación y replay; R4 también usa creación/update como fixture. El CSV asigna una razón primaria de selección y no pretende que todos los tests de un módulo escriban las mismas tablas. Los negativos de R1/R2 pueden fallar antes de producir efectos; quedaron retenidos por selección conservadora de módulos/fixtures mixtos, no porque se haya demostrado un pago en cada uno.

Hay una distinción necesaria: retención histórica no significa imposibilidad técnica permanente. La lectura del código confirma que R6 usa stubs o rechazos 401, y R5 dobles de pago. Es posible evaluar después una selección más estrecha, pero este encargo prohíbe nuevas pruebas y no autoriza levantar restricciones por iniciativa propia. No se recomienda activar completion/cancel/orden/pago para inflar el número ejecutado.

Los siete casos R3 son los pendientes más cercanos a compatibilidad/bypass D17; no deben ocultarse al hablar de garantías generales. Los 27 R4 y los componentes de R2/R5 sobre el ciclo posterior son relevantes para una compra gobernada completa, que no se reclama aquí. Ningún porcentaje sobre 393 se usa como tasa de seguridad.

El test histórico que canceló dos veces **no está entre estos 144**: ya se había ejecutado y figura separadamente como `HISTORICAL_PYTEST_PASS_AUTHORIZATION_BOUNDARY_INCIDENT` en el inventario completo. Su exclusión posterior evita repetición, pero no borra las dos cancelaciones ni convierte la campaña histórica en conforme.
