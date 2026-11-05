# CT19 — Viabilidad en Merchant fijado (inspección, no ejecución)

Fuentes: Merchant abf30df3b6a611284dedcc26c00accac6a853329, Agent main 4ce2196.
ACPCheckoutAdapter.create_in_transaction (acp.py) busca el ID bajo run/actor y devuelve
OFFER_NOT_FOUND si no existe. _offer no reemplaza por nombre/precio similar. api.py mapea
OFFER_NOT_FOUND a HTTP 404 / invalid_item. Agent runtime_dispatch clasifica exactamente
(404, invalid_item) como rechazo definitivo; un 404 genérico sigue siendo incierto.
PersistentCatalogReader.get registra offer.read incluso en ausencia; inspeccionarlo.

Por tanto el contrato PUEDE representar una variante desaparecida. No equivale a cantidad
cero: stock agotado devuelve 409 out_of_stock con identidad todavía presente. Desactivar
producto tampoco demuestra desaparición de ID. No se modifica Merchant para provocar verde.

MerchantControl existente permite reprice, stock, revisión y restore sobre una fila presente,
pero no desaparición/restauración de identidad. managed_catalog.update_offer solo precio/stock.
El esquema 004_checkout_sessions.sql referencia catalog_offers(run_id,offer_id): borrar una
oferta con sesiones previas puede fallar o invalidar el estímulo, no se debe forzar ni quitar FK.

Implementación mínima futura: un control SOLO del arnés dentro del cluster desechable, que
exija cero checkout para esa identidad, retire la fila después de selección/D1 y antes de
forward de creación, y pueda reinsertar snapshot exacto. Reutilizar worker MerchantControl y
FaultProxy.before_forward, no endpoint comercial nuevo ni acceso DB desde Agent. Verificar
que el ID falta en lectura scoped y en reexport, otros IDs/snapshots idénticos, feed Agent
viejo conservado. Esperar 404 invalid_item real, ningún reemplazo/segundo POST, ninguna orden/
pago; contrastar HP/HNP por separado. Incluir control stock-cero como condición DISTINTA.
Reinsertar la oferta y verificar tabla/snapshot/FK, cleanup y hashes aun con fallo.

No implementado ni ejecutado en EXP-05. Requiere ese control de fixture y un prerregistro
propio antes de la prueba. No existe bloqueo arquitectónico demostrado ni autorización para
cambiar producto/contrato; la viabilidad runtime y restauración todavía no están acreditadas.
