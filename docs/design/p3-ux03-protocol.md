# P3 — Protocolo técnico UX03/UX05 HP

Diseño original del 3 de noviembre de 2026 (Bogotá). Estado: **protocolo documental**.
Puerta experimental de UX03/UX05: **BLOCKED**.
Este incremento documenta el diseño y sus criterios de validación.

Entregables asociados: [dependencias](p3-ux03-dependencies.md) y
[pruebas de validación propuestas](p3-ux03-validation.md).

## 1. Checkpoint y alcance

El diseño partió del checkpoint Agent `ad86bdd6eea23e30a9cda509607f8e8ec933c6a2`.
P1 [#156](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/pull/156)
y P2 [#157](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/pull/157)
ya están en `main`. La actualización documental de P2 se publicó en
[#184](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/pull/184).
El orden de revisión restante es #158 → #160 → #176. El estado observado por
escenario y modo se consulta en [matrix-observed.json](../../evaluation/scenarios/p0/matrix-observed.json).

Documentos obligatorios leídos en la base:

- [Cierre P2](../../evaluation/results/p2-merchant-provenance/README.md): 3 pruebas passed
  en el segundo nightly; router guionado, sin comprensión LLM ni UX03.
- [Auditoría](../../evaluation/scenarios/p0/coverage-audit.md): cobertura leída e historia,
  sin convertir skipped en ejecución.
- [Experimentos siguientes](../../evaluation/scenarios/p0/next-experiments.md): calibración
  aislada recomendada condicionalmente; sin API de quote demostrada.
- [Metodología](../../evaluation/scenarios/p0/methodology.md): cinco celdas, prioridad A,
  presupuestos separados, unknown distinto de falso.
- [Matriz](../../evaluation/scenarios/p0/matrix.json): 33 situaciones / 37 unidades previstas.
- [Oráculo P1](../../evaluation/scenarios/p0/ux03-oracle.json):
  `synthetic_math_only`; sus importes USD no son datos observados del Merchant.

En el checkpoint, `PurchaseIntent` representa un bloque hard y
preferencias, sin ramas ordenadas con límites propios; `rank_candidates` filtra el precio
unitario observado, mientras `PurchaseService` verifica después el total del checkout.
El [diseño de intención condicional](p0-conditional-intent.md#311-compatibilidad-con-el-protocolo-p3-de-maldo)
documenta la compatibilidad prevista con P3. [#190](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/pull/190)
incorporó el contrato tipado, el evaluador y el registro de decisiones. Su conexión al
recorrido experimental HP y la validación de las celdas siguen pendientes.

CI del checkpoint comprobado en GitHub:
[41702583061](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41702583061),
head `ad86bdd`, cuatro jobs success. Es CI offline, separado del nightly P2.
El CI de esta revisión y el perfil live `p0-catalog-bridge` comprueban la integración
documental y la regresión P2. La validación experimental de P3 tiene evidencia separada.

## 2. Contratos auditados y procedencia

Merchant fijado: `abf30df3b6a611284dedcc26c00accac6a853329`.
PSP fijado: `e6ab67b5963434a9a3caae2012b70232a3cca65d`.
El checkout local Merchant estaba limpio en `9743c9f179c7ad5413e8818766ef3dceb326a917`;
se inspeccionaron **blobs del SHA fijado mediante git show**, sin cambiar su rama.
No se importó Merchant en el proceso Agent ni se consultó ninguna base de datos.

Fuentes primarias de esta auditoría (funciones indicadas para reproducción):

| Fuente fijada | Contrato comprobado por lectura |
| --- | --- |
| [Merchant managed_catalog.py](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/abf30df3b6a611284dedcc26c00accac6a853329/src/commerce_lab/managed_catalog.py) | `import_current_catalog`, `changed_offer`, `update_offer` |
| [Merchant db/core.py](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/abf30df3b6a611284dedcc26c00accac6a853329/src/commerce_lab/db/core.py) | `fixture_snapshot`, `seed`, reloj del episodio |
| [Merchant generated.py](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/abf30df3b6a611284dedcc26c00accac6a853329/src/commerce_lab/fixtures/generated.py) y [feed.py](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/abf30df3b6a611284dedcc26c00accac6a853329/src/commerce_lab/feed.py) | precios/stock/envío generados y proyección ACP |
| [Merchant commerce.py](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/abf30df3b6a611284dedcc26c00accac6a853329/src/commerce_lab/contracts/commerce.py) | `Offer`, `Pricing`, `DeliveryContext`, `Checkout` |
| [Merchant acp.py](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/abf30df3b6a611284dedcc26c00accac6a853329/src/commerce_lab/acp.py) | `ACPCheckoutAdapter.create/update/get/cancel`, `_response` |
| [Merchant checkout.py](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/abf30df3b6a611284dedcc26c00accac6a853329/src/commerce_lab/checkout.py) | `prepare_in_transaction`, `_mutate`, `_validate_offer`, `complete`, `_expire_if_needed` |
| [Merchant api.py](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/abf30df3b6a611284dedcc26c00accac6a853329/src/commerce_lab/api.py) y [context.py](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/blob/abf30df3b6a611284dedcc26c00accac6a853329/src/commerce_lab/context.py) | rutas, API-Version, sesión sintética ligada a run/actor |
| [Control del arnés](../../tests/integration/live_merchant_control.py) y [puente P2](../../tests/integration/test_live_p0_catalog_bridge.py) | importación, exportación por episodio, mutaciones y lectura independiente |
| [Intent](../../src/commerce_agent/domain/intent.py), [selección](../../src/commerce_agent/domain/selection.py), [conversación](../../src/commerce_agent/application/conversation.py) | representación HP, shortlist y propuesta |
| [PurchaseService](../../src/commerce_agent/application/purchase.py), [ApprovalService](../../src/commerce_agent/approval.py), [CompletionService](../../src/commerce_agent/completion.py) | preparación, presupuesto, aprobación ligada a términos y completar |

### Catálogo y episodios

El catálogo administrado se importa una vez bajo un run de variante `merchant`; reiniciar
no regenera inventario ni precios. Su importación cambia timestamps a tiempo actual.
`seed` crea otro run y copia el fixture con `FIXTURE_NOW`; el feed de ese episodio debe
exportarse explícitamente. `/feeds/current` representa el catálogo administrado, no ese run.
El worker `p0_provenance` **tiene efectos**: puede importar y exporta archivos; no usarlo
como lector puro dentro de un episodio experimental después de congelar su estado.

Los atributos/títulos proceden del snapshot curado, pero precio, stock, envío y ciertas
variantes son sintéticos del generador Merchant. Una respuesta real del servicio observa
esos datos de laboratorio; no acredita precio ni disponibilidad de una tienda externa.
P2 observó históricamente 141 productos exportables y 310 variantes; no es un nuevo censo.

`update_offer` modifica solo el catálogo administrado, incrementa revisión y renueva tiempos.
No muta automáticamente los episodios B0. Restaurar precio/stock no restaura la revisión
anterior. Mutaciones de un caso deben fijarse antes de calibrar sus clones, o invalidar
la calibración y crear una nueva versión; nunca ignorar revisión por coincidir los importes.

### Checkout y total

| Operación existente | Efectos y restricciones en el SHA auditado |
| --- | --- |
| POST `/checkout_sessions` | Una línea, cantidad 1 en este perfil; prepara con Offer del run autenticado. Copia pricing y contexto, persiste sesión, respuesta idempotente y evento. No reserva ni decrementa inventario en este camino. |
| POST `/checkout_sessions/{id}` | Actualiza buyer, fulfillment y opción. Relee Offer, valida stock/expiración y copia precio/revisión actuales; persiste revisión, mutación y eventos. No permite cambiar línea, cupones o descuentos. |
| GET `/checkout_sessions/{id}` | Lee snapshot de sesión; puede persistir expiración y eventos. No equivale a refrescar automáticamente pricing desde Offer. |
| POST `/checkout_sessions/{id}/cancel` | Deja sesión `canceled`, mutación y evento persistidos. No borra historia ni equivale a rollback. El replay idempotente debe comprobarse. |
| POST `/checkout_sessions/{id}/complete` | Camino posterior que valida estado/revisión/stock, confirma pago y crea orden/decrementa stock al éxito. Prohibido en esta unidad y en la futura validación inicial del protocolo. |

`Pricing.total_minor = items_total_minor + shipping_total_minor`, impuestos ya incluidos,
moneda `cop`. ACP publica `totals[type=total]` global y `fulfillment`; no sumar de nuevo
el envío al total ni tomar el total de la línea como total de compra.
El destino soportado es CO/Bogota/110111; readiness exige además DC y contacto completo.
La tarifa es un dato del Offer generado, no una cotización logística externa; update
refresca pricing del Offer, no demuestra cálculo de tarifa para destinos arbitrarios.
La selección de envío contiene IDs específicos de cada checkout: no copiarlos entre clones.

Agent prepara, aplica datos/opción y hace GET; el presupuesto total se verifica contra
checkout autoritativo. El feed no transporta toda la información de envío/revisión:
una mutación de shipping puede dejar igual su proyección. Hash de feed igual es insuficiente.

### Identidad y autoridad HP

La sesión Merchant liga run y actor; API-Version e idempotencia pertenecen al backend.
En Agent, `owner_id` y los términos presentados/aprobados ligan la operación al comprador.
No trasladar bearer, instrumentos, firmas, actor, request IDs o claves al modelo.
P2 usó TestClient/local principal: eso no valida por sí solo JWT/UI reales multiusuario.
El futuro protocolo inicial fija HP, ACP, gobernanza `enforce`, HNP deshabilitado y AP2 fuera
de la comparación; registrar configuración efectiva y no deducir modo de `variant=B0`
del seed. B0 es aquí procedencia del episodio, no permiso de compra sin gobernanza.
La verificación HP positiva autenticada requiere la prueba adicional del plan, antes
de ampliar a compras. Solicitar «compra A si…» no sustituye el clic de aprobación final.

## 3. Decisión técnica: calibración aislada condicional

| Alternativa | Evaluación |
| --- | --- |
| Precio del feed o scorer Agent | Sin checkout, pero insuficiente para autoridad de total/envío. Descartada como fuente del oráculo. |
| Leer Offer | Útil para equivalencia, stock y revisiones; no sustituye el checkout final. |
| Crear A/B dentro del episodio evaluado | Permite observar totales allí, pero añade efectos exploratorios. Requiere contrato de producto y medición propios; no se adopta en esta unidad. |
| Calibrar clones independientes y verificar limpieza/equivalencia | Reutiliza ACP actual y permite congelar el oráculo sin contaminar el historial evaluado. **Recomendada solo si pasa la puerta de validación**. |
| API nueva de quote | Cambiaría producto/contrato; no justificada mientras la opción anterior sea comprobable. |

Conclusión de la auditoría del checkpoint: la calibración es técnicamente plausible en
un estado controlado y congelado. Su evidencia se revisa en el incremento de validación.
Si el recorrido Agent probado carece de capacidad para proponer una rama UX03, registrar
`capability_missing` o el fallo observado y conservar separada la respuesta del oráculo.

### Equivalencia que debe demostrarse

Usar por configuración de celda tres runs distintos: calibración A, calibración B y
evaluación E. Un cuarto run V, desechable, sirve para verificar reproducibilidad antes
de E. Cada run conserva el **catálogo completo**, no solamente la rama que debe ganar.
Mismo fixture/semilla, fuentes y locks, motor Merchant/PSP, configuración, pricing,
stock, atributos, moneda, revisión de Offer, contexto/días de entrega y reloj comercial.
Mismos datos sintéticos de comprador/destino y opción semántica de envío.

El comparador propuesto usa todos los campos de Offer validados, incluyendo tiempos,
con orden canónico de objetos y orden preservado de arrays; no excluye campos arbitrarios.
El seed permite igualdad exacta con reloj fijo. Administrado vs seed no son equivalentes
temporalmente por defecto. Si se necesitan relojes distintos, queda **BLOCKED** hasta
aprobar y probar una normalización explícita de validez/TTL; no quitar timestamps sin más.
Revisión diferente bloquea aun si precio/stock volvieron a su valor inicial.

Solo pueden diferir identidades de run/actor/sesión, IDs de checkout/línea/envío, claves de
operación y rutas locales. Conservar mapping privado/público sanitizado para demostrar que
los recursos pertenecen al run correcto. Igualdad de Offer ID o fixture hash no prueba scope.
Comprobar mismos valores relevantes por rama **entre clones**; A y B pueden tener precios,
revisiones o envíos diferentes entre sí. Registrar configuración global PSP y contadores
antes/después: el PSP y los procesos pueden ser compartidos aun con runs distintos.

Sin escritores concurrentes ni clientes ajenos en el cluster desechable. Agente E con
SQLite, historial, thread, turn, caché, credenciales e idempotencia propios. No copiar
bases Agent ni respuestas de calibración a E. Los tokens pueden vencer con reloj real
aunque el comercio use reloj de escenario: vigencia de ambos relojes se comprueba aparte.

## 4. Secuencia propuesta y registros

1. Aceptar protocolo y pruebas. Antes de cualquier episodio, fijar fuentes, reglas de par,
   configuración, mutaciones, celdas, presupuesto de efectos y taxonomía; registrar versión/hash.
2. En preparación de laboratorio separada, importar/leer catálogo y obtener dos variantes por
   `pair_rules` P2: A Sony / Xperia 5 II / 256 GB / new; B OnePlus / 8T / 256 GB / new,
   active y stock positivo. Exigir una coincidencia por regla; 0 o >1 bloquean como
   `insufficient_fixture_data`, sin buscar un par que favorezca al Agent ni usar IDs fijos.
3. Crear clones de estado para cada condición. Las mutaciones experimentales son scripts
   declarados del control Merchant desechable, no endpoints comerciales ni acciones del Agent.
   Su persistencia en episodios debe validarse; `p0_managed_update` solo no basta.
4. En A-cal y B-cal, crear exactamente un checkout para su variante mediante ACP HTTP real;
   aplicar buyer/fulfillment/opción según el mismo contrato que Agent y releer. Guardar
   snapshots de create/update/get, revisión, total global, desglose y estado independiente.
   Ninguna aprobación, instrumento, token, mandato ni complete.
5. Registrar cada ID inmediatamente y reconciliar creaciones inciertas con mecanismos existentes.
   En finally cancelar todos los checkouts conocidos, releer por HTTP y control independiente.
   Cancelación debe quedar persistida y sin órdenes/tokens/pagos ni cambios de Offer/stock.
   Sesiones/mutaciones/eventos esperados se conservan en calibración; no exigir DB vacía.
   Una creación de resultado desconocido o una cancelación no verificada bloquean todo el lote.
6. Comparar A-cal/B-cal/V/E antes del primer turno. V repite preparación/cancelación de ambas
   variantes para comprobar igualdad de totales y efectos permitidos; nunca abrir sesiones
   de validación en E. E debe seguir sin checkouts/órdenes/aprobaciones ni historial Agent.
7. Derivar límites de los totales observados T_A/T_B según sección 5; sellar manifiesto final,
   oráculo y conversación con SHA-256 y revisión humana **antes del primer turno Agent**.
   El manifiesto final referencia el diseño previo y sus calibraciones; no retrofecha el sello.
8. Agent recibe conversación natural, catálogo y datos de entrega habituales. El arnés observa
   propuesta, intento de preparación y respuesta; nunca llama `start(A/B)` según el esperado,
   no prefiltra al ganador, no inyecta intención ya resuelta, no expone expected ni totales de
   calibración como hechos que el Agent haya consultado. Separar evidencia de antes/después.
9. Contrastar checkout elegido con estado vigente. Diferencia respecto a calibración invalida
   comparabilidad; detener y clasificar, sin modificar el oráculo ni hacer fallback automático.
   Conservar selección observada y fallo comercial como dimensiones separadas.
10. Terminar antes de aprobar/completar. Medir todos los efectos y cancelar sesiones de E en
    finally; publicación de resultado solo después de limpieza comprobada. Limpieza fallida
    no borra un fallo previo: registrar ambos y poner el lote en cuarentena.

Contrato de evidencia del diseño: por fase/run, SHAs completos,
hashes de fuentes/locks/fixture/feed/configuración sanitizada, ambos relojes, actor/run aliases,
Offer before/after completo y revisión, HTTP método/ruta/status/ordinal, checkout snapshots y
revisiones, total/moneda/cantidad/desglose, estado independiente de efectos y limpieza.
Cada observación conserva fuente, instante y clase (`merchant_observed`, `synthetic_input`,
`controlled_mutation`, `derived_budget`, `agent_proposal`, `independent_oracle`).
HTTP y control correlacionados sin publicar credenciales; logs vacíos no son evidencia HTTP.
Publicar JSONL/JUnit/manifiesto/hash sanitizados y conservar fallos; no `.env`, DB privada,
bearer, firmas, instrumentos ni datos personales. Los nuevos registros no reemplazan P2.

## 5. Cinco celdas y UX05

Cantidad 1, moneda COP, condiciones X/Y = atributos exactos de cada regla, active y stock
verificado. Elegibilidad = X/Y verdaderos, stock suficiente, total Merchant <= límite de
esa rama y evidencia vigente. `T-1` es una unidad menor monetaria, sin floats; bloquear
si T=0 hace inválido el límite. Importes por determinar con calibración futura.

| Celda | Estímulo congelado propuesto | Oráculo independiente |
| --- | --- | --- |
| UX03-A | A stock>0, límite A=T_A; B stock>0, límite B=T_B-1 | A; igualdad total/límite aceptada. B no es elegible por presupuesto. |
| UX03-B | A stock>0, límite A=T_A-1; B stock>0, límite B=T_B | B; se incumple comercialmente A y B tiene límite propio inclusivo. |
| UX03-C | Ambas stock>0 y límites T_A/T_B; exigir T_B<T_A en calibración | A por prioridad aunque B cueste menos. Si el orden de precios no se verifica, bloquear esta subcondición, sin cambiar de par tras ver resultados. |
| UX03-D | Ambas stock>0; límites T_A-1/T_B-1 | none; explicar y no seleccionar C aunque exista C barato. |
| UX03-UNKNOWN-A | Mismo estado elegible de C, fallo HTTP/timeout dirigido a verificación de A después de calibrar | undetermined; B válida no convierte unknown A en falsa. Sin fallo alcanzado, la prueba de inyección es inválida, no passed. |

Para UNKNOWN se requiere demostrar que el Agent intenta una operación de verificación
distinguible de A; la ausencia de tal capacidad es una dependencia pendiente. Un router
estático que recibe la etiqueta esperada solo prueba el arnés. No simular comprensión.

UX05 usa el mismo episodio D y lo etiqueta con ambas identidades, con un solo denominador.
Un control cercano modifica solo límite A de T_A-1 a T_A: debe permitir A; otro modifica
solo límite B a T_B con A falsa: debe permitir B. No cuentan como ejecuciones de nuevas
unidades de la matriz. Añadir control stock=0 para A/B por separado y ambos agotados,
mutación persistida antes del turno y sin afectar otros runs. Si una variante agotada no
permite checkout, no inventar su total: conservar el total previo como antecedente,
marcar no aplicable bajo ese estado y usar stock=0 como evidencia comercial suficiente.
Esa condición no se presenta como un clon idéntico del estado con stock positivo.

Controles adicionales: T+1 de presupuesto, moneda incompatible, total ausente/no entero,
expiración exacta, revisión distinta con precio igual, shipping +1 con feed igual,
condición X falsa por atributo verificable, B desconocida cuando A falsa, ambos desconocidos,
token vencido/actor ajeno, respuesta perdida tras create y cancelación fallida.
Mantener separados datos inválidos, incertidumbre técnica y condiciones comerciales falsas.
No convertir un 401/429/timeout/JSON inválido en «agotado» o «fuera de presupuesto».

## 6. Resultados, efectos permitidos y límites

Límite acordado: calibración A/B un checkout por run; V dos checkouts cancelados;
E máximo un checkout elegido, sin exploración previa. A/B/C elegibles pueden dejar ese checkout
pendiente de aprobación hasta teardown; D y UNKNOWN esperan cero creación. Un intento indebido
se registra y limpia, sin ocultarlo aunque termine en cero órdenes. Si el producto necesita
exploración A/B, hace falta aceptar otro contrato/versionar protocolo antes de ejecutar;
no relajar este criterio al observar fallos. El arnés no impide ni sustituye la elección.

Reportar cuatro dimensiones: validez del protocolo, decisión Agent, cumplimiento comercial
y autoridad/efectos. `BLOCKED` por equivalencia no demostrada; error técnico conserva causa;
`failed` por decisión o efecto contrario al oráculo en episodio válido; `not_executed` sin
episodio; `skipped` solo test omitido. Defecto de producto y carencia de datos no son sinónimos.
No calcular tasa UX con episodios inválidos; publicarlos aparte con denominadores y causas.

Compra queda fuera: el futuro clic HP debe autenticar comprador y términos actuales; ninguna
aprobación/calibración se reutiliza para otro checkout. Una extensión de compras requerirá
aceptación explícita, presupuesto de efectos y pruebas de frescura/identidad. No HNP, LLM
pagado, nueva infraestructura ni nueva API se autorizan por aprobar este diseño.

## 7. Decisiones humanas y validación

Yair aceptó las decisiones 1–4 y autorizó el incremento de validación del punto 5.
La evidencia de contrato Offer → Checkout está en
[#160](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/pull/160)
y la validación del protocolo en
[#176](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/pull/176).
Ambos tienen revisión e integración separadas. Las cinco celdas UX03 y la evaluación
UX05 siguen pendientes del comportamiento condicional del Agent y de su ejecución experimental.

1. Aceptar calibración por clones con igualdad exacta de Offer y reloj fijo, alcance de
   laboratorio sintético y bloqueo ante cualquier equivalencia no demostrada.
2. Aceptar par/reglas, cinco celdas, controles, límites relativos, prioridad/unknown y
   deduplicación UX03-D/UX05. Los importes y hashes finales se aceptarán tras calibrar.
3. Aceptar presupuesto de checkouts y prohibición de exploración en E, o definir otro contrato
   antes de implementación; la calibración no resuelve esa capacidad del Agent.
4. Aceptar HP/enforce/ACP, controles de identidad, qué evidencia confirma aislamiento y
   limpieza y quién revisa el sello. Compras y evaluación LLM requieren autorización posterior.
5. Validar el protocolo en un incremento limitado al arnés y las pruebas propuestas.
   Usar esa evidencia para decidir la preparación de la evaluación UX03.

Riesgos abiertos: persistencia adicional no medida por P2, efectos globales PSP/caché,
equivalencia temporal, alcance de mutaciones por episodio, exposición parcial de alternativas
en shortlist, conexión del evaluador A/B al flujo HP y punto de inyección UNKNOWN.
Las pruebas propuestas deben cerrar estos riesgos o conservar el bloqueo; CI verde no los cierra.
