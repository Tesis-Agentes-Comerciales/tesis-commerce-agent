# EXP-05 — CT15 HNP desde main integrado

Base verificada 4ce219623ed0304a3e7378519a37f0b8844694c5 y CI 41908376626, cuatro jobs
success antes de cambiar archivos. Rama nueva desde main; PR directo a main. Pila P3 intacta.

HN03 histórico reexporta el feed y reinicia Agent sobre la misma base; B2-HNP ve el precio
nuevo y escala en D1. Se conserva esa evidencia, sin atribuirla como prueba P0 nueva.
CT15 aquí conserva el feed antiguo y aumenta precio en before_forward del POST: después
de selección/D1, antes de que Merchant cree el checkout. Se verifica cuerpo e identidad,
cronología, precio/revisión, feed y total HTTP autoritativo. No se reinicia ni refresca feed.

Datos: catálogo completo del episodio, X disponible de menor precio/ID estable y precio
mayor de otra variante observado Merchant. Sustituir ítem/total/revisión de X exclusivamente;
envío/stock/identidad/contexto/fechas idénticos. Presupuesto antiguo para above y máximo
exacto del nuevo total con envío para within. Inputs y oráculos congelados antes de Agent.

above: Merchant crea una vez, guarda común detecta total superior, cancela; tarea failed /
budget_guard, cero reserva/cierre/outbox/token/pago. within: preparación a total exacto,
seguida de revocación de seguridad declarada al solicitar checkout-jwt. Reutiliza on_checkout
y el recorrido de revocación existente: UoW-1 puede reservar y debe liberar no_outbox; cero
mandatos cerrados/outbox/token/pago. El final canceled es causado por esa revocación, no por
el precio: solo se acredita preparación en presupuesto, no compra ni aprobación autónoma.

Defensa proxy bloquea complete y POST al PSP; cualquier intento inesperado queda como fallo,
nunca como comportamiento correcto. La consulta checkout-jwt usa Merchant real y la revocación
es API real. Limpieza cancela todas las sesiones, restaura snapshot, revoca grant, cierra Agent
y observa tablas Merchant/PSP/ledger, run_events, reservas, idempotencia y shutdown.

Reutilizar LiveStack, PurchaseLane, HnpAgent, MerchantControl.reprice_offer, FaultProxy,
on_checkout, recorded_case y reportador EXP-01 parametrizado. No producto/proveedor/lock/
oráculos previos, modelos o pagos completos nuevos. Cada perfil produce evidencia separada.

CT19 es revisión de viabilidad, no se ejecuta aquí: desaparición exige ausencia de identidad
antes de lookup/create, no stock cero. Documentar contrato y falta de control fixture exacto.
El adaptador autenticado se valida aparte como transporte, sin declarar UX semántica ejecutada.
