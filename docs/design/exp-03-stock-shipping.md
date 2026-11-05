# EXP-03 — Stock HNP y envío HP

Continuación autorizada del encargo: CT15 y CT14 verificados en Actions 41895460657 y
41895755779 antes de ampliar. PR/rama independientes apilados explícitamente sobre EXP-02.
No se reescribe ningún protocolo anterior, resultado, producto ni proveedor.

CT13 HNP B2-HNP: catálogo completo Merchant congelado, variante elegida por precio/ID
observados y disponibilidad real. Consentir un grant exclusivo de esa variante; antes del
único trigger agotar solo stock/revisión. Conservar feed antiguo. Debe obtener rechazo
Merchant 409 out_of_stock sin checkout aceptado, orden, token, pago ni mandato cerrado.
No se autoriza fallback fuera del único SKU del grant. No se evalúa interpretación LLM.
El outcome actual de la aplicación puede ser checkout_failed: lo importante es observar
la causa comercial independiente; no convertir un timeout/error técnico en stock agotado.
No ejecutar control positivo HNP que pudiera completar una compra.

CT16 HP: preparar y presentar checkout con condiciones estables; incrementar solo envío
y total, usando como incremento el envío observado del propio Merchant (no una observación
de mercado ni un precio inventado). No cambiar subtotal, stock, revisión, nombre ni fechas.
Verificar feed comercial idéntico al reexportar y comparar snapshots completos. La relectura
debe bloquear checkout_terms_changed y registrar shipping_minor/total_minor cambiados.
Control estable independiente. No aprobar ni completar. No se demuestra aquí reaprobación
ni disponibilidad de nuevos términos: ese tramo queda explícitamente pendiente.

Mismos pines y stack LiveStack/PurchaseLane/FaultProxy/MerchantControl/HnpAgent. Reutilizar
helper HP de EXP-02 y reportador EXP-01 parametrizable, sin otro runner o infraestructura.
Congelar protocolo y entrada antes de Agent; consentimiento y barreras HNP se registran.
Huella de todas las tablas Merchant, run_events/sesiones/stock, ledger Agent y PSP; restaurar
oferta exacta, cancelar cualquier checkout, revocar grant, cerrar proceso y verificar shutdown.
No pagos nuevos, no llamadas LLM. Cada escenario se ejecuta y revisa antes del siguiente.
