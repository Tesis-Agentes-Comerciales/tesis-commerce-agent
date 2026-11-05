# Próximos experimentos — propuesta para revisión de Yair

> **Instantánea del 2-nov-2026.** El estado vigente de cada escenario está en
> [`matrix-observed.json`](matrix-observed.json) y en [`observed-evidence.md`](observed-evidence.md).
> Desde entonces se ejecutaron y registraron INF27, INF28, INF29 e INF33 (#177, #180), y se
> fusionaron #178 (`get_offer` exige el mismo id y contexto) y #179 (causa del rechazo HNP en CT13).

**No aprobado ni implementado.** Propuesta v2 de Notion del 2-nov-2026 pendiente de aprobación
final de Yair. [Auditoría y cruce de cobertura](coverage-audit.md);
[prerregistro UX03 HP](methodology.md); [cierre P2](../../results/p2-merchant-provenance/README.md).
No se cambian matrix.json ni ux03-oracle.json. Estos últimos prueban contrato/oráculo sintético,
no contienen observaciones nuevas ni validación UX03. Las 37 celdas siguen not_executed.

## Orden y reutilización

1. Acordar protocolo de datos/totales y oráculos antes de implementar UX03/UX05.
2. Extender integraciones ya próximas: CT16, INF30, CT13; evitar duplicar shipping/recovery/HN05.
3. INF27/INF28 con transporte local controlado; no hace falta consumir cuota real.
4. UX08 multivuelta y las cinco celdas UX03 sobre producción actual, conservando fallos.
5. Tras aprobación y presupuesto, interpretación real y extensión del piloto de revisión.

Este orden propuesto conserva los ocho candidatos de Yair, sin imponer que todos se ejecuten
con LLM ni que una prueba vecina ya sea validación del nuevo escenario.
Cada experimento requerirá SHA de Agent/Merchant/PSP, configuración/health efectiva, episodio
desechable, feed/fixture hashes, entrada/oráculo sellados antes del primer turno, cambios
before/after con revisiones y registros públicos de efectos. Reutilizar nightly.yml y el arnés,
no crear otro workflow ni permitir rutas globales adicionales.
Desconocido, excepción técnica y resultado comercial falso son estados distintos.
Registrar passed, failed, skipped y not_executed; ninguna omisión de integración cuenta como éxito.

## Antes de UX03: totales autoritativos sin contaminar el episodio

**Hecho:** no se encontró una API de quote sin efectos en el Merchant fijado.
Offer.pricing.total_minor incluye contexto del laboratorio, pero el total autoritativo del
checkout se obtiene preparando/actualizando una sesión. P2 demuestra que esa preparación crea
una fila y que cancelar la deja persistida. No es una lectura inocua ni una compra aprobada.

Alternativas:

| Estrategia | Ventaja | Límite y decisión |
| --- | --- | --- |
| Usar precio observado del feed | No crea checkout | No incluye toda la autoridad/envío; insuficiente para el oráculo total de UX03 |
| Usar Offer obtenido por lectura autorizada | Más información con revisión y contexto | No sustituye total del checkout final; solo dato de planificación/estado |
| Preparar A/B en el mismo episodio evaluado | Totales del Merchant | Introduce sesiones antes de decidir; altera conteos/historia. No adoptarla implícitamente |
| Calibrar en episodios independientes clonados del fixture, cancelar y verificar | Aísla los efectos de calibración y permite fijar límites antes del turno | Requiere demostrar equivalencia de ofertas/contexto y ausencia de dependencias ocultas; recomendado **sujeto a validación** |

Protocolo propuesto, aún sin ejecutar:
1. Derivar dos variantes por atributos declarados del catálogo fijado; registrar por qué son
   admisibles y resolver empates determinísticamente. No pedir al selector que produzca el ganador.
2. Exportar/sellar el estado fuente (precio, revisión, stock, moneda, delivery_context y opciones).
   No confundir run administrado con episodio B0; P2 demostró su independencia.
3. En episodios de calibración separados A/B, usar las mismas operaciones ACP de creación/
   preparación de entrega que ya usa el Agent. Registrar cada sesión, respuesta y total con envío.
   No completar, aprobar, delegar pago ni firmar mandatos.
4. Cancelar cada sesión y releer: estado canceled, cero órdenes/tokens/pagos y ofertas sin cambios.
   Si alguna limpieza o medición falla, la calibración queda failed/unknown y se detiene.
5. Solo tras esa comprobación, fijar X/Y a partir de totales calibrados (T, T-1 y T+1 minor
   según cada celda), documentar fórmula y congelar el oráculo antes del episodio evaluado.
   No ajustar límites después de ver qué eligió el Agent.
6. Crear episodio evaluado independiente con ofertas equivalentes, mismo envío/destino y
   configuración; comprobar sus hashes/valores antes de correr. Un ID o fixture_hash coincidente
   no prueba identidad temporal de un catálogo vivo.
7. Después de la selección, contrastar el checkout final real con límite de la rama y revisión
   vigente. Si cambió el contexto desde calibración, no usar aquel total como autorización:
   abortar/escalar o seguir el protocolo de cambio aprobado. Un fallo al verificar A no habilita B.
8. Mantener denominadores separados: checkouts de calibración frente a checkouts evaluados.
   Las cancelaciones de calibración no se ocultan; se documentan en sus episodios.

Esta estrategia puede servir como oráculo independiente del experimento de selección/interpretación
en un estado congelado. **No implementa en el producto la capacidad de comparar totales de A/B.**
Si el Agent necesita esa capacidad y no existe, registrar capability_missing o un fallo observado,
no reemplazar su decisión con un mock ni hacer que el arnés ejecute la rama por él.
Yair debe decidir si el resultado esperado permite preparación exploratoria dentro del episodio:
sin esta decisión no fijar arbitrariamente “cero checkouts” en todas las ramas falsas.

## UX03 — alternativas condicionales (HP)

- Reutilizar: intención/ranking/preparación y validación total existentes; cinco celdas de P1;
  puente P2 para feed, estado, limpieza. No sumar UX04-C o UX05-D como nuevas ejecuciones
  independientes si son exactamente el mismo estímulo y criterios; conservar etiquetas cruzadas.
- Hipótesis: el Agent respeta prioridad A, condiciones y límite propio de cada alternativa,
  sin convertir error técnico en falso ni seleccionar C.
- Oráculo independiente: tabla de verdad preregistrada sobre estado Merchant y X/Y,
  no rank_candidates ni scorer del producto; precios de calibración solo tras validar protocolo.
- Modo: HP; revisor HNP no participa. Interpretación simulada comprueba contratos/mecanismo,
  interpretación real es una evaluación distinta y requerirá autorización de gasto.
- Datos: variantes verificadas por atributos, stock/revisiones/destino fijados; mutaciones adversas
  definidas antes del episodio. No Sonora como evidencia comercial principal.
- Celdas: A cumple/B no → A; A no/B sí → B; ambos → A; ninguno → abstenerse;
  verificar A desconocido por fallo → no saltar a B como si A fuera falsa. Exacto límite y ±1
  prueban fronteras. Mantener prioridad en el texto, sin entregar el ganador al modelo.
- Efectos: selección no equivale a preparación; checkout no equivale a aprobación ni orden.
  Medir sesiones por episodio, órdenes, tokens PSP e inventario; completar solamente cuando
  el protocolo futuro autorice explícitamente compra sandbox, después de consentimiento compatible.
- Capacidad ausente: PurchaseIntent no tiene dos ramas tipadas con límites/prioridad separados;
  no afirmar soporte completo ni modificarlo en esta auditoría.
- Criterio previo: Yair aprueba semántica de unknown, etapas y calibración; los controles de
  aislamiento/reproducción pasan; se fija qué sería defecto del producto y se conservan negativos.
- Costo potencial: cero para caracterización determinista; real = llamadas efectivas × uso de tokens
  y tarifas verificadas en el momento. Presupuesto, proveedor y número de repeticiones pendientes.

## UX05 — ninguna alternativa válida

- Reutilizar UX03-D, S02/S07 y respuesta no-candidate API. La respuesta actual puede ofrecer
  alternativas: no asumir que ya respeta la prohibición de comprar una tercera.
- Hipótesis/oráculo: A y B incumplen restricciones etiquetadas independientemente; explicar/
  abstenerse sin elegir C. Control positivo cercano: cambiar un solo límite o stock hace válida
  una rama, que debe poder seleccionarse sin un bloqueo falso.
- HP; revisor HNP fuera de alcance inicial. Misma calibración/datos UX03, con ambos presupuestos
  inferiores al total o condición de stock falsa. A/B desconocido sigue siendo infraestructura.
- Integración comprueba cero órdenes/tokens y ausencia de preparación indebida según contrato
  aprobado; medir todos los checkouts exploratorios, si se permiten, sin ocultarlos.
- LLM real solo para explicación/comprensión: aprobación y costo pendientes; offline usa
  respuestas programadas declaradas como tales. Criterio: compartir episodio con UX03-D donde
  proceda; no contar dos veces el mismo resultado para aumentar n.

## UX08 — cambio A → B antes de autorizar

- Reutilizar cancelación API/P2, invalidación de aprobación y antecedentes S08;
  S08 revoca autoridad, no demuestra comprensión de un cambio de idea.
- Hipótesis/oráculo: petición nueva inequívoca selecciona B; el consentimiento/términos de A
  no habilitan B. Una confirmación antigua de A no produce orden; confirmar B exige términos
  actuales compatibles. Control: petición repetida sin cambio conserva conducta idempotente.
- HP primero; HNP necesitaría revocación y consentimiento nuevo, no editar grant en silencio.
  Revisor HNP no sustituye aprobación HP ni recibe hoy toda la conversación.
- Datos: dos variantes del Merchant y contexto multivuelta; guardar términos, fingerprints públicos/
  digests y estado temporal sin JWT ni llaves. Fijar quién cancela A y qué hacer si falla esa cancelación.
- Efectos: conteos por checkout A/B, aprobación asociada, orden/variante, token PSP e inventario;
  caso sin aprobación nueva termina sin compra. No completar ahora.
- Pendiente: semántica conversacional y coordinación entre propuestas, no solo endpoint cancel.
  LLM real necesario para medir interpretación de historia; contrato determinista previo sin costo.
- Criterio: oráculo de consentimiento viejo/nuevo y limpieza aprobado antes de implementar.

## CT13 — stock agotado después de consentimiento HNP

- Reutilizar HN01/HN03, lectura autoritativa y mutación/limpieza P2.
  P2 muta el catálogo administrado; **no muta el episodio B0 de esa compra**.
- Hipótesis/oráculo: stock=0 en la oferta que autoriza ese grant impide comprar A;
  B solo puede usarse si el grant y la intención condicional lo permiten. Sin fallback autorizado,
  detener/escalar; no convertir “A ausente” en permiso general.
- HNP B2 enforce, selector determinista primero y revisor off para aislar guardas.
  Con revisor llm sería una condición posterior, no requisito para disponibilidad.
- Mutación tras consentimiento y antes del punto de lectura/creación usando barrera controlada,
  sin sleeps arbitrarios. Hay que definir el mecanismo de stock por episodio:
  update_offer administrado no basta para B0. Extender el control de laboratorio usando
  changed_offer/operaciones existentes y conservar revisión inicial/final, sin nueva API comercial.
- Control positivo: misma variante con stock>0; negativo: agotado y B fuera de presupuesto/
  alcance; borde: último stock con carrera sería CT18 separado.
- Verificar catálogo del run, checkout, orden, tokens PSP y grant; stock restaurado en finally
  antes de passed. Oráculo deriva de estado persistido, no de mensaje.
- Sin LLM real necesario para disponibilidad/autoridad; comprensión del fallback requiere UX03.
  Criterio: mutador aislado y punto de sincronización demostrados antes de ejecutar.

## CT16 — envío cambia tras checkout HP

- Reutilizar **directamente** test_s04_freshness_refuses_before_d2_and_the_next_purchase_is_authorized
  en tests/integration/test_live_b2_d2_authorization.py. Ya hace edit_offer(change=shipping),
  rechaza GET/POST de aprobación obsoleta y autoriza una segunda compra.
  En el CI revisado figura skipped; P2 no lo ejecutó.
- Hipótesis/oráculo: aumento de shipping/total en el snapshot Merchant invalida términos viejos;
  ningún pago/orden con aprobación anterior. Control positivo sin mutación; incremento pequeño
  dentro del presupuesto y otro que lo exceda diferencian frescura de presupuesto.
- HP B2 gobernado, router guionado para mecanismo; sin revisor HNP. El texto conversacional
  puede evaluarse después con LLM real, no hace falta para invalidar términos.
- Usar mutador edit_offer del arnés, con revisión/digest before/after: es estímulo interno de
  laboratorio, no API comercial pública de envío. Precio de artículos igual, envío cambia.
- Extensión mínima: registrar estados/ausencia de tokens y órdenes del primer checkout, limpieza
  confirmada y nuevo consentimiento; no repetir el happy path existente con otro nombre.
  El test existente advierte que su carril bloquea cancel: conservar esa limitación y usar
  fixture específica si se requiere limpiar; no ampliar PURCHASE_ROUTES.
- Criterio: ejecutar la integración existente con evidencia y SHAs fijados antes de declarar CT16;
  LLM/costo cero para el mecanismo.

## INF27 — rate limit 429

- Reutilizar test_the_delegated_choice_counts_the_sdk_retry (ModelServer local,
  429 → éxito) y las guardas selection_unavailable actuales.
- Hipótesis/oráculo: reintentos limitados por contrato y Retry-After acotado; nunca loop ni compra
  tras una selección ausente. Registrar número exacto de peticiones y error_kind.
- Control positivo 429 transitorio → respuesta válida; negativo 429 persistente → indisponibilidad.
  Fijar previamente la cota de llamadas/espera (clientes inspeccionados max_retries=1);
  no cambiarla para acomodar el resultado.
- HP y HNP se especifican por separado; comenzar por selector HNP, revisor off. Probar fallo del
  revisor como INF33 en otra celda, sin atribuir resultados cruzados.
- Transporte local simulado a nivel HTTP; Agent real; feed Merchant fijo y servicios desechables
  para demostrar efectos. API pagada y agotamiento de cuota real no son necesarios.
- Oráculo externo: contador del servidor + ledger/outbox + Merchant/PSP antes/después, cero
  checkout/orden/token cuando no hay selección válida. Criterio: clasificar 429 origen proveedor
  frente a Better Auth: los 429 históricos de auth no son INF27.

## INF28 — billing / insufficient_quota

- Reutilizar transporte y registro de INF27. Falta cobertura específica que distinga cuota de
  rate limit; no está demostrado que el producto o SDK eviten todo reintento inútil.
- Hipótesis/oráculo propuesto: respuesta 429 con error insuficiencia de cuota es indisponibilidad,
  no candidato inválido ni respuesta exitosa. Fijar con Yair si se exige cero retries a nivel
  aplicación/SDK o se admite el intento acotado actual; evaluar sin bajar expectativas después.
- Datos: payload de error contractual del proveedor soportado, sin credenciales reales;
  contraste 429 transitorio y autenticación inválida como controles separados.
- HNP selector primero; HP router después si comparte contrato. Revisor off; no alimentar un
  “confirmed” por defecto para saltar el error. Mutaciones comerciales: ninguna.
- Efectos independientes: cero nuevo checkout/orden/token, estado terminal y causa correctos.
  Transporte simulado, no LLM real: costo cero. Capacidad/contrato de clasificación pendiente.
- Criterio previo: taxonomía, cota y mensajes aprobados; un fallo encontrado se registra como
  defecto real y no obliga a cambiar este oráculo para que pase.

## INF30 — respuesta perdida tras POST checkout

- Reutilizar test_07a_real_d1_initial_dispatch_then_recovery_without_continuation
  (pass/drop/reset/delay/concurrent) y recuperación 06D-C. Ambos skipped en CI ordinario;
  este nightly P2 no los ejecuta. No basta añadir un caso idéntico.
- Hipótesis/oráculo: Merchant confirmó/creó una sola sesión y se perdió la respuesta;
  Agent conserva incertidumbre y reconcilia por identidad/idempotencia/lookup firmado,
  sin nueva compra a ciegas ni continuación no autorizada.
- HP gobernado, router estático; sin revisor HNP ni LLM pagado. Datos derivados de oferta
  Merchant fijada y claves de operación generadas por backend, no dadas al modelo.
- Inyectar caída después de commit Merchant con hook/barrera existente del FaultProxy;
  verificar por lectura independiente que el POST llegó. Control negativo caída antes de commit
  demuestra que ausencia de respuesta no dice por sí sola si hubo efecto.
- Estados Merchant antes/después de restart/lookup, una sola creación lógica, cero órdenes/tokens
  mientras no existe aprobación; contar intentos HTTP y resultados idempotentes separadamente.
- Extensión mínima: enlazar resultado técnico con estado público del usuario y evidencia de
  “no repetición a ciegas”. Criterio: incertidumbre no se etiqueta como cancelación ni producto falso;
  los tests existentes deben ejecutarse/retenerse antes de reclamar cobertura nueva.

## Extensión HNP-REVIEW: valor incremental en conversaciones nuevas

Propuesta, no implementación ni autorización de API. Mantener HR01–03 y HNP-02 históricos.
No empezar comparando proveedores/modelos: primero verificar el nuevo corpus y su oráculo.

- Crear entradas conversacionales inéditas revisadas por humanos: preferencia firme vs blanda,
  negación (“no B”), corrección del último turno, referencia ambigua y alternativa condicional.
  Etiquetar qué información sobrevivió a preference_text/intención/grant.
  El revisor actual no recibe historia completa: si exige contexto no representado, registrar
  capability_missing y acordar el adaptador experimental; no fingir que ya lo vio.
- Seleccionar varios pares admisibles por atributos Merchant, incluyendo controles en que
  ambas elecciones son aceptables. Los dos productos fijos HR no bastan para generalización.
  Sellar corpus/oráculos y reservar entradas no vistas; no entregarlos a ninguno de los roles.
- Primero aislar revisión: mismo candidato propuesto y mismos hechos para backend-only y
  backend+review. Propuestas guionadas adversas se etiquetan como controles, no errores
  espontáneos del selector. Medir contradicción dentro del grant (backend permite) y fuera
  del grant (backend bloquea aunque revisor apruebe).
- Después, con presupuesto aprobado, un selector LLM real produce propuestas nuevas.
  Registrar por episodio model.hnp_select y model.hnp_review, outcome/tokens/modelo efectivo.
  Para comparación del revisor, reutilizar la **misma** propuesta, sin volver a muestrear selector
  por brazo: evitar atribuir al revisor diferencias causadas por dos elecciones distintas.
- Oráculo humano independiente adjudica intención usando estado Merchant y conversación;
  reglas deterministas conservan autoridad monetaria/estado. No usar al revisor como su scorer.
- Reportar capturas semánticas incrementales (backend habría permitido), bloqueos falsos,
  confirmaciones incorrectas contenidas por backend, abstenciones/review_unavailable,
  costo/latencia adicionales y casos sin datos. Denominadores por inputs distintos y repeticiones,
  no sumar HNP-02 ni los 60 episodios HR como si fueran nuevos.
- Ejecutar primero revisión sin efectos y unos pocos episodios sandbox autorizados después,
  con datos desechables y comprobación independiente de checkout/orden/token/stock.
  El tope se calcula de inputs × repeticiones × llamadas selector/revisor y límite de tokens,
  usando tarifas verificadas al autorizar. Aquí costo ejecutado nuevo = 0.

## Puertas de aprobación

Yair debe revisar antes de implementar: ocho prioridades, semántica de ramas/unknown,
protocolo de totales/calibración, cantidades de checkouts permitidas por etapa, modos y
consentimientos, capacidades ausentes y taxonomía 429/cuota, corpus del revisor y presupuesto.
Conservar casos negativos y artefactos fallidos. No merge, ejecución pagada, UX03 ni benchmark
automáticos por el hecho de aprobar este documento o pasar los controles del arnés.
