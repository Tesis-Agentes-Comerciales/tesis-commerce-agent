# EXP-02 — Controles comerciales sobre EXP-01

Autorización: encargo del 4 de noviembre de 2026, CT15 y CT14 primero, cada escenario
validado antes de ampliar. Rama apilada sobre EXP-01 #162, sin fusionarlo ni modificar su
protocolo o evidencia. Agent producto base 65183d5; proveedores en los pines de EXP-01.

## Contrato y aceptación previa a ejecución

CT15 HP: exportar catálogo completo del episodio Merchant; elegir por orden estable una
variante activa con stock y otra con precio observado mayor. El nuevo precio es ese precio
Merchant existente, aplicado como mutación sintética controlada a la primera variante.
Conservar el feed original; cambiar exclusivamente items_total_minor/total_minor/revisión.
Congelar antes de ejecutar el Agent las dos ofertas, presupuesto y oráculo. Dos episodios
independientes: máximo igual al nuevo total (incluido envío), y máximo igual al total antiguo.
El Agent real debe usar el checkout autoritativo: dentro del máximo prepara sin aprobar;
fuera cancela por budget_exceeded, sin orden/token/pago. No se evalúa comprensión ni selección
conversacional. No se proporciona al Agent el resultado esperado.

CT14 HP: preparar un checkout con el runtime gobernado real de producción, mostrar términos
frescos; después agotar exclusivamente stock (cantidad cero/availability y revisión),
conservando precio, nombre, envío, contexto y expiración. Releer términos por PurchaseService;
debe rechazar checkout_terms_changed, registrar stock changed y no persistir aprobación.
Control positivo en episodio independiente sin mutación. No se pulsa aprobación ni complete:
el resultado se limita a revalidación antes de aprobar, no rechazo de complete.

## Componentes y evidencia

Reutilizar LiveStack, PurchaseLane, FaultProxy, MerchantControl, runtime build_runtime,
settings_for del test D2, recorded_case y reportador EXP-01 con protocolo parametrizable.
Añadir solo lectura de snapshot de oferta y mutación pura stock al controlador de pruebas.
Cada episodio conserva feed completo, oferta antes/después, reloj, presupuesto, entrada,
SHAs, barrera temporal y hashes antes de ejecutar el comportamiento afectado. Estado de
preparación previo a la mutación CT14 es preparación del estímulo; se registra por separado.
Observación independiente de todas las tablas Merchant (conteos/huellas), stock/run_events,
checkouts y PSP; nada de JWT, claves ni bases privadas. Sin órdenes, tokens ni pagos nuevos.
Cancelar todos los checkouts, restaurar snapshot exacto, verificar limpieza y shutdown.
Fallos funcionales se conservan; error de estímulo no se renombra como fallo comercial.

## Secuencia

Primero ejecutar únicamente CT15 en Actions. Revisar JUnit, evidencia, hashes y limpieza.
Después CT14; ampliar con CT13/CT16 solo tras revisar resultados válidos. No ejecutar full
ni INF27/28/29/33 (Jair). Los perfiles de ejecución reutilizan nightly.yml; CI general aparte.
No modificar producto, proveedores, matriz P0, oráculo UX03, resultados anteriores ni locks.
No ejecutar llamadas pagadas, firmas de aprobación o complete. No hay infraestructura nueva.
