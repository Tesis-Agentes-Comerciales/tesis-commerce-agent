# EXP-04 — MA22/MA23 por revocación y consentimiento nuevo

Lote posterior a CT15/CT14 y CT13/CT16 verificados en Actions. La matriz P0 ya define
explícitamente revocar + nueva autorización para grants inmutables. No se introduce edición
in-place ni se atribuye comprensión conversacional. B2-HNP real, sin LLM ni compras completas.

MA22: variante Merchant disponible, grant inicial con total Merchant; revocar antes de trigger,
consentir otro grant idéntico salvo presupuesto reducido a un centavo por debajo del precio
observado del ítem. Es presupuesto sintético controlado, no un nuevo precio comercial. El
trigger antiguo debe devolver 409; el nuevo debe escalar budget_not_satisfiable sin POST.
Verificar límites firmados mediante verify_database/open_mandates, no solo la vista API.

MA23: elegir X disponible y Y cuyo precio de ítem observado excede el total actual de X.
Grant original permite X/Y con presupuesto igual a total de X; grant nuevo conserva ese
presupuesto y excluye X, dejando solo Y. X seguiría siendo comercialmente viable si se
ignorara exclusión, por lo que es distractor adversarial. Revocar el antiguo antes del único
trigger; trigger antiguo 409 y nuevo escalado por presupuesto, sin comprar X ni Y. No mutar
Merchant. Esto verifica exclusión/alcance renovado bajo el límite constante, no un cambio
de preferencias LLM ni una edición de mandato existente.

Congelar ambos requests y ofertas, catálogo completo, oráculos/límites/reloj y hashes antes
de iniciar Agent. Registrar consentimiento anterior, revocación duradera, consentimiento
nuevo y ambos triggers. Leer scopes/estados/epochs/digests del ledger y verificar ambas
firmas con el verificador existente. Huellas de todas las tablas Merchant y PSP idénticas,
cero POST creación, reserva/outbox/mandatos cerrados/token/orden/pago. La defensa del proxy
impide cualquier creación/pago/complete inesperado y lo convierte en fallo, nunca en passed.
Revocar ambos grants, cerrar proceso y comprobar limpieza del stack incluso con fallos.

Reutiliza HnpAgent/PurchaseLane/LiveStack/FaultProxy, recorded_case/reportador parametrizado,
MerchantControl, verify_database. No nuevo ejecutor. No cambia producto/proveedores/locks,
matriz/oráculos, evidencia previa o trabajo de Jair. Cada escenario se valida antes del otro.
