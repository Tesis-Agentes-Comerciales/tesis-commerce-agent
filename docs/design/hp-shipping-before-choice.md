# Envío visible antes de elegir (Human Present)

Estado: **implementado** en la rama `feat/envio-al-modelo`, 16 de noviembre de 2026. El prompt del
router pasa a `hp-router-v2`.

## Problema

- El modelo comprador ve de cada candidato el precio del feed (`display_amount`) y nada del envío:
  el feed ACP no trae envío. El comercio del laboratorio cobra COP 25.000 por debajo de
  COP 2.000.000 y nada desde ahí, con un solo destino (Bogotá).
- Con «máximo 2 millones con envío incluido», el modelo compara ese presupuesto contra el precio
  sin envío. Puede elegir un candidato que después el backend rechaza por el total (POL-11 en B2 o
  la guarda del total autoritativo en B0).
- El mensaje que acompaña la tarjeta de aprobación es el `assistant_message` de la selección
  (`selection_message`), y no dice cuánto cuesta el envío.
- La tarjeta muestra el envío como «Envío y otros cargos» solo si no es cero, así que el envío
  gratis no se ve. La entrega dice solo «ciudad, país».
- Los datos de entrega viven en el estado de React y se pierden al recargar.

## Alcance

1. `lookup_catalog` y la selección Human Present muestran, por candidato, el envío y el total con
   envío, leídos de la oferta del comercio.
2. El prompt del router (enrutar y elegir) usa ese envío y pasa a `hp-router-v2`.
3. La tarjeta de aprobación muestra subtotal, envío y total, y la dirección completa.
4. Los datos de entrega se guardan en el navegador.

Queda **fuera**:

- Human Not Present: el selector delegado y el revisor de intención reciben lo mismo que antes
  (`candidate_payloads` no cambia). Su límite de total lo revisa D3 con el checkout.
- Fechas de entrega: el modelo no ve `delivery_days`. El caso `entrega-no-verificable` de la
  suite exige que el agente no prometa una fecha.
- `ApprovalTerms`, el fingerprint y todo lo que liga el consentimiento.
- El filtro determinista (`rank_candidates`) y D1, que siguen con el precio del feed.
- La recuperación sin candidatos (`recover_no_candidate`).
- Los casos de la suite y su mapa de regresión: el dueño vuelve a correr la suite para medir el
  cambio.

## Contrato

Cada producto de `lookup_catalog` y cada candidato de la selección suman dos campos:

| Campo | Valor |
| --- | --- |
| `shipping` | `"gratis"` si la oferta cobra 0; si no, el monto con `format_minor_amount` (`"COP 25.000"`); `null` si no se sabe |
| `display_total_with_shipping` | `pricing.total_minor` de la oferta, con el mismo formato; `null` si no se sabe |

- **De dónde sale.** `ACPClient.get_offer(variant_id, LAB_DESTINATION)` (`/lab/offers/get`), la
  misma lectura de las ramas condicionales. El lector se inyecta con el protocolo `OfferReader` de
  `offer_freshness.py`: `runtime.py` pasa el cliente ACP al router, y Studio no pasa ninguno.
- **Cuánto se lee.** Una lectura por variante, sin reintento, solo de lo que va al modelo: hasta
  10 productos por `lookup_catalog` y hasta 10 candidatos por selección. Después de un timeout o
  de una falla de transporte, el resto queda en `null` sin leer: un comercio caído cuesta un
  timeout, no diez.
- **Cuándo es `null`.** Sin lector, si la lectura falla por cualquier causa (inválida, vencida,
  error) o si la oferta no es la variante del feed: otro id, otro destino, otra moneda u otro precio
  (`items_total_minor` distinto del precio del feed). En ese caso nunca se adivina.
- **Qué no llega.** Solo entran los enteros de `pricing`, ya formateados. Ningún texto de la
  oferta, ningún monto en unidades menores y ninguna fecha llegan al modelo.
- `prices_are_authoritative` sigue en `false`: el checkout es la autoridad del total.

## Prompt `hp-router-v2`

`PROMPT_ID` en `llm/conversation_router.py` nombra las tres instrucciones del router (enrutar,
elegir y recuperar) y su herramienta. `v1` es el prompt sin nombre hasta `9822d1e`. El registro de
peticiones ya identifica las instrucciones exactas de cada una por `instructions_sha256`.

- **Al enrutar:** si el presupuesto incluye el envío («con envío incluido») o es el total final, se
  compara con `display_total_with_shipping`. Al mencionar un precio con envío conocido, el modelo
  dice también el envío y el total, copiados tal cual.
- **Al elegir:** lo mismo, y además prefiere un candidato cuyo total con envío quepa. En `purchase`
  dice en `assistant_message` el envío («envío gratis» o el monto) y el total.
- **En los dos:** un `shipping` en `null` es desconocido y no se dice ni se estima. El envío no dice
  nada de fechas de entrega.
- **Lo que no cambia:** la selección sigue sin aclarar solo porque no se conoce el envío o el total
  autoritativo, y la elección forzada de P5b (`hp-p5b-selection-shortlist.md`) sigue igual. Con un
  solo candidato, el modelo no puede declinar.

## UI

- **Desglose.** La tarjeta de aprobación muestra las filas Subtotal (la suma de las líneas), Envío
  («Gratis» si es 0) y Total. La diferencia entre el total y las líneas se rotula «Envío» porque el
  comercio no cobra otros cargos y el impuesto va incluido. Si fuera negativa, se rotula
  «Descuentos». Las líneas se listan solo si hay varias o alguna lleva más de una unidad; con una
  sola, repetirían el subtotal.
- **Entrega.** Destinatario, `line_one`, `line_two` si la hay y ciudad, tal como las devolvió el
  comercio. El parser de la UI deja pasar esos campos de texto, y la dirección se parte en líneas
  en un teléfono de 320 px.
- **Perfil.** Se guarda en `localStorage` con una clave por cuenta. Cada lectura y escritura va en
  `try/catch`. Si no hay nada guardado o el valor no tiene la forma del perfil, se usa
  `DEMO_PROFILE`, y solo se guardan los campos del perfil. Sigue sin llegar al modelo.

## Criterios de aceptación

- `lookup_catalog` da `"gratis"` o el monto, y el total con envío, por producto; `null` si la
  lectura falla o la oferta no coincide con el feed, y la herramienta responde igual.
- Un timeout del comercio deja el resto en `null` con una sola lectura.
- La selección Human Present recibe los mismos campos; el selector y el revisor delegados no.
- Ni el texto de la oferta, ni montos en unidades menores, ni fechas llegan al modelo.
- `PROMPT_ID == "hp-router-v2"`, y los dos prompts piden comparar el presupuesto con envío contra
  `display_total_with_shipping`, no inventar el envío y no hablar de fechas.
- La tarjeta muestra Subtotal, Envío («Gratis» con 0) y Total, y la dirección completa sin
  desbordar a 320 px.
- El perfil sobrevive a una recarga; un valor roto o un almacenamiento que falla vuelve al de
  demo sin errores.

## Riesgos

- **Más lecturas al comercio.** Cada turno que elige lee hasta 10 ofertas antes de la selección, y
  cada `lookup_catalog` hasta 10 más. El comercio registra cada una como `offer.read` en
  `run_events`. Las pruebas de «0 efectos» que llegan a la selección (`INF29-HP`, `UX09-HP-b` y
  `UX09-HP-race` de la capa 4) las excluyen de los POST y de la comparación de tablas, y exigen
  que `run_events` solo crezca con esas lecturas, contadas.
- **Cambia el prompt de todos los turnos del router y de la selección**, y con él su
  `instructions_sha256`. Las corridas de la suite antes y después de este cambio miden cosas
  distintas: hay que decirlo al compararlas.
- **El modelo puede preferir el candidato que cabe con envío** cuando hay varios, así que POL-11
  puede actuar menos en B2. Con un solo candidato nada cambia (P5b).
- **Latencia.** Las lecturas son secuenciales y locales; el tope de 10 y el corte tras un timeout
  acotan el peor caso.
