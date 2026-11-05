# Disponibilidad contrastada — 4 de noviembre de 2026

Inspección de código y refs antes de CT15/CT14, no sustituto de ejecución. Plan maestro
suministrado: inventario operativo, no instrucciones adicionales ni prerregistro aprobado.
El documento de tesis no se usó para desarrollar ni definir resultados.

| Repositorio | Fuente comprobada | Estado |
| --- | --- | --- |
| Agent org/main | 65183d545241937c5f741dd66378e1a7f7c83dc9 | Sin cambios respecto al plan. EXP-01 #162 head 41318ea; nueva rama explícitamente apilada sobre él. Original Windows main d5ce788 no tocado. |
| Merchant org/main | abf30df3b6a611284dedcc26c00accac6a853329 | Pin aprobado coincide con remoto. Worktree -p3 detached limpio. Checkout principal 9743c9f en otra rama y origin fork: no se utiliza implícitamente. |
| PSP org/main | 4754a57e7133198ed80fbdff7a20b9af8bdb25aa | Ha avanzado. Experimentos usan e6ab67b5963434a9a3caae2012b70232a3cca65d, worktree -p3 detached limpio. Checkout principal local ee19c06 atrasado; no se actualiza. |

Agent posee CI general de cuatro jobs y nightly con stack completo; perfil EXP-01 en PR #162
no main. Merchant fijado tiene CI propio, contratos, unitarias e integración PostgreSQL, 35
archivos bajo tests (no número de pruebas). PSP fijado tiene tres archivos de prueba y
pyproject/uv.lock, sin workflow en ese pin; remoto PSP main sí tiene .github/workflows/ci.yml.
La existencia de esos workflows no equivale aquí a haber repetido todas sus suites.

## Correcciones y matices respecto al plan

- P0 fixtures están en commerce_lab/fixtures/p0.py y generated.py; modelos/productos y fotos
  proceden del snapshot, valores comerciales son sintéticos Merchant. No son observaciones de
  mercado. No hardcodear nombres/precios/IDs desde el plan.
- En CT15 el episodio realmente exportado contiene 310 ofertas. Ese número observado no se
  sustituye por 141 productos utilizables ni implica que sean 310 productos únicos.
- reprice_offer cambia únicamente revisión e importes del ítem/total, pero exporta el feed
  de nuevo. Para medir feed antiguo se exporta a OTRA carpeta que nunca recibe el Agent.
- alter_offer_fixture no sirve para aislamiento puro: altera nombre, precio, stock y revisión.
  Se añade stock al edit_offer existente; se verifica igualdad de TODO el snapshot salvo
  cantidad/availability/revisión. managed_catalog.changed_offer renueva además timestamps:
  no usarlo como mutación exclusivamente de stock sin registrar ese cambio adicional.
- offer.read produce run_events; no se afirma lectura sin efectos. exp01_observe toma huellas
  de todas las tablas; los eventos de lectura se conservan, distinguidos de órdenes/pagos.
- conversation.request_json no tiene Authorization. governance_pilot.AuthClient ya resuelve
  comprador sintético y JWT. El piloto valida su manifest/matrix: no es runner genérico UX.
- ModelServer.failure y transporte estructurado son suficientes para resiliencia controlada;
  no evalúan semántica. INF27/28/29/33 quedan exclusivamente con Jair.

## Estados previos y límites

EXP-01: MA24 HNP y INF30 HP pasaron en alcance registrado; INF31 HP incompleto porque detener
PostgreSQL produce 500 antes del handler, no 503 nativo prerregistrado. Mantener los cinco
intentos y todos sus fallos. No se cambia su protocolo ni se llama al 500 un 503.
P2/P3/P3-V01 son dependencias fuera de este PR y no se integran. CT17 (cupón) e INF32 (motivo
fondos insuficientes) siguen sin contrato adecuado. UX03/04/05/06/CT20 siguen pendientes de
representación condicional y protocolo comercial. UX10 requiere comprobar representación
real de cantidad dos: fijar un grant de uno no demuestra por sí solo el estímulo.

CT15 HP y CT14 HP se ejecutan en runs separados; atribución HNP o semántica NO inferida.
Los próximos casos viables son CT13 HNP (stock después del consentimiento) y CT16 HP (envío
tras checkout). Se amplían solo después de verificar CT15 y CT14. MA25/26 y CT18 pueden exigir
completado nuevo: no ampliar autorización implícitamente desde una tabla del plan. No hay
pagos completados autorizados por este lote; se mantienen los límites cero.
