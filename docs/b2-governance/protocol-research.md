# Protocolos: investigación para el diseño final

Fecha de consulta inicial: 2026-10-11. Estado: investigación y selección provisional para HP + HNP;
no es una selección definitiva ni una verificación de conformidad integral.

Este trabajo responde a la observación de la reunión del 9 de octubre: justificar qué protocolos
y qué partes necesita el sistema. Los hechos sobre los estándares se apoyan en fuentes primarias;
las decisiones propuestas para la tesis son inferencias de diseño.

## Mapa por responsabilidad

| Pieza | Qué define | Qué estudiar primero | Propuesta para v1, pendiente de cierre |
|---|---|---|---|
| ACP | Intercambio comercial; Agentic Checkout y Delegate Payment son especificaciones complementarias [1] | Sesiones, estados, actualización, finalización, errores, idempotencia, feed, eventos y delegación de pago | Partir del perfil REST existente y justificar cada subconjunto utilizado |
| UCP | Capacidades y extensiones comerciales, discovery y varias vinculaciones de transporte [2] | Checkout, fulfillment, órdenes, negociación de capacidades y extensión AP2 | Compararlo directamente con ACP; justificar conservar ACP o migrar, sin asumir que hacen falta dos adaptadores |
| AP2 | Mandatos y recibos ligados al checkout y al pago; contempla Human Present y Human Not Present [3] | Actores, Trusted Surface, firma, hash, audiencia, vigencia, verificación y recibos | Incluir HP y HNP: mandatos cerrados, abiertos y cadenas; probar el puente ACP/SDK |
| MCP | Interfaz para herramientas, recursos y prompts; descubrimiento e invocación de herramientas [4] | Qué interfaz se necesita, quién es cliente/servidor y qué capacidades se exponen | Incorporarlo si existe una necesidad concreta de interoperabilidad de herramientas; una función interna no demuestra uso de MCP |
| A2A | Comunicación entre sistemas de agentes independientes; Agent Cards, mensajes, tareas y artefactos [5] | Si el merchant será un servicio comercial o un agente con tareas propias | Estudio y exclusión inicial si no hay interacción entre agentes independientes |
| TAP de Visa | Reconocimiento de agentes mediante firmas de mensajes, con mecanismos de reconocimiento de consumidor y contenedor de pago [6] | Modelo de confianza, claves, firmas y replay; requisitos de incorporación | Estudio y exclusión inicial para el merchant configurado; reconsiderar ante reconocimiento externo de agentes |

OPA/Rego es un motor de políticas candidato, no una alternativa al protocolo comercial ni al de
autorización. Su selección se cierra en la arquitectura de gobernanza.

La separación de capas permite estudiar una combinación concreta: comercio + autorización +
interfaz de comunicación cuando haga falta. UCP contempla REST, MCP y A2A como transportes [2];
esto no obliga al proyecto a implementar los tres.

## Lo que ya existe y debe distinguirse del estándar

Estas observaciones proceden de los README de los tres repositorios, del
[perfil AP2 local](../ap2-profile.md) y de la procedencia del bundle ACP. No sustituyen pruebas E2E.

| Pieza existente | Naturaleza | Trabajo pendiente del perfil final |
|---|---|---|
| Checkout REST y Product Feed ACP `2026-04-17` | Subconjunto validado contra fuentes congeladas; commit `7fdd78df677a94dce04c770644b0fbbb1401272b` [7] | Enumerar operaciones, estados, campos y errores efectivamente cubiertos |
| `GET /feeds/current` | Ruta de publicación propia con forma de Product Feed | Declararla como propia; distinguirla de Feed API incremental |
| `tesis_sandbox` y proveedor de pago separado | Handler sintético y subconjunto de Delegate Payment | Separar contrato estándar, configuración del handler y confirmación/consulta propias |
| `/checkout_sessions/{id}/ap2/checkout-jwt` y `/ap2/complete` | Puentes experimentales ACP–AP2 | Documentar mapeo de payload, revisión, hash y verificación; no presentarlos como rutas normativas ACP |
| Better Auth/JWT y fingerprint HMAC | Identidad de aplicación y vínculo local de aprobación | Diferenciarlos de mandatos AP2 y de una firma individual de cada comprador |
| `lookup_catalog` y nodos LangGraph | Herramienta y orquestación de aplicación | No etiquetarlos como MCP o A2A sin implementar la interfaz correspondiente |

AP2 deja los detalles del protocolo comercial fuera de su alcance y exige validaciones
deterministas [3]. Por eso la integración ACP–AP2 necesita un perfil explícito que describa los
responsables y las verificaciones del puente, aunque ambas piezas ya estén presentes en la demo.

## Preguntas que debe resolver la lectura profunda

**ACP y UCP.** Para cada requisito, identificar operación, estado y autoridad. ¿Cómo se obtiene la
cotización actual? ¿Qué ocurre si stock o precio cambian? ¿Qué campos y precondiciones son estándar,
cuáles son internos y cuáles requieren una extensión? ¿Cómo se conserva la identidad de operación
en reintentos? Comparar con el mismo caso B2C antes de decidir si UCP aporta una capacidad necesaria.

**AP2.** Seguir compras HP y HNP completas: quién emite Checkout JWT, quién firma cada
mandato, quién verifica cada restricción y quién devuelve cada recibo. Especificar rechazo, expiración,
replay y cambio de términos. Documentar que el perfil actual confía en la firma del proveedor del
agente después del clic autenticado; no equivale a una clave individual del comprador.

**MCP, A2A y TAP.** Para cada uno, formular primero la interacción que lo necesitaría, los dos
extremos que lo implementarían y el beneficio verificable. Si no existe esa interacción en v1,
cerrar su exclusión con ese motivo. Incorporarlo solo al cambiar el alcance correspondiente.

## Ficha de decisión por capacidad

Cada fila de la matriz final debe contener:

| Campo | Evidencia requerida |
|---|---|
| Requisito | Comportamiento concreto que necesita la tesis |
| Protocolo/módulo y versión | Fuente normativa fijada; no depender solo de `main` o `latest` |
| Operación y datos | Endpoint/mensaje, campos obligatorios, estados y errores |
| Responsable y confianza | Emisor, consumidor/verificador y fuente autoritativa |
| Cobertura | Existente, por implementar, excluida o propia del experimento |
| Integración | Transformaciones, precondiciones y diferencia respecto al modelo interno |
| Fallos y prueba de aceptación | Caso positivo, inválido, expirado, repetido o incierto según corresponda |
| Decisión | Incluir, conservar, sustituir o excluir; razón y alternativa descartada |

La recomendación provisional es **ACP REST + AP2 v0.2 HP y HNP**, condicionada a una prueba
criptográfica del puente HNP. HP ya existe; HNP no. UCP queda como alternativa real si la adaptación
ACP no permite verificar correctamente restricciones y cadenas o resulta desproporcionada. Estudiar un protocolo
no implica implementarlo, y usar un subconjunto no acredita conformidad completa.

## Selección por módulos y brechas verificadas

El alcance HP + HNP fue confirmado por Yair el 2026-10-11. HNP no implica B2B ni obliga a MCP/A2A.
El [roadmap](system-design-v1/plan.mdx) desarrolla la arquitectura y los cierres.

| Pieza | Incluir en diseño v1 | Límite o brecha |
|---|---|---|
| ACP REST | Discovery, esquema Product/snapshot, create/get/update/cancel checkout, fulfillment físico, handler/Delegate Payment sintéticos, Order/webhook | Una unidad/COP/merchant configurado propuestos; estados desconocidos se rechazan |
| ACP completo | No incorporar cart independiente, push incremental de feed, descuentos/loyalty, binding MCP o Delegate Authentication por defecto | Cada exclusión tiene condición de reapertura en D08; `GET /feeds/current` es pull propio, distinto de Feed API push |
| AP2 HP | Checkout/Payment Mandates cerrados, Trusted Surface, firmas, hash y recibos | Consentimiento sobre términos exactos; no una firma individual de comprador por implicación |
| AP2 HNP | OpenCheckout/OpenPayment + Agent Key/cnf + cadena a cerrados + recibos | Nuevos emisores, materializador y verificadores; base actual solo HP |
| UCP REST | Alternativa al adaptador ACP, con discovery/capacidades y extensión AP2 publicada | Cambia modelo/estados, PUT de checkout y JCS/JWS; migrar exige baseline común nuevo |
| MCP/A2A/TAP | Exclusión inicial con motivo y condición explícitos | Herramienta interna, compra autónoma y merchant determinista no bastan para necesitarlos |
| OPA | Evaluación local detrás de puerto Python; versión por fijar tras spike | Motor de gobernanza, no protocolo de pago; no autentica ni verifica firmas |

### AP2 v0.2 HNP: estándar frente a contratos propios

1. El estándar define `mandate.checkout.open.1` y `mandate.payment.open.1`, ligados por `cnf` a
   una clave de agente. Después de consentimiento previo, el agente cierra sobre una transacción
   y presenta la cadena abierta/cerrada. No usar `IntentMandate` como tipo normativo de esta versión.
2. Perfil mínimo propuesto: una compra futura, variantes y merchant explícitos, una unidad, monto
   total máximo COP, instrumento acotado y vigencia corta por definir. Los constraints útiles son
   `checkout.allowed_merchants`, `checkout.line_items`, `payment.allowed_payees`,
   `payment.amount_range`, `payment.allowed_payment_instruments` y `payment.reference`.
3. Destino, preferencias semánticas, registro de uso, triggers y revocación operativa son controles
   propios. Si deben demostrarse mediante firma ante terceros, diseñar una extensión con schema
   y algoritmo determinista compartidos. Rechazar constraints desconocidos.
4. Separar emisor confiado tras Trusted Surface de la Agent Key que prueba posesión al cerrar.
   El LLM no accede a claves ni llama libremente al signer. Merchant y proveedor verifican sus
   restricciones independientemente del PDP.
5. Dos checkouts distintos pueden violar una misma delegación aunque sus claves idempotentes
   sean válidas. AP2 exige evitar cierres solapados; diseñar reserva/CAS por mandato abierto y
   guardar abierto/cerrado/recibo. Un timeout no libera cupo ni autoriza un cierre nuevo.
6. Revocación instantánea distribuida no viene resuelta por un endpoint normativo del perfil
   consultado. Especificar epoch y orden revocación/compromiso; una operación ya comprometida puede
   estar en curso. Introspección remota inmediata requeriría un contrato online propio adicional.
7. Frescura se bifurca: HP exige presentación nueva ante cambio material; HNP puede cerrar términos
   nuevos dentro de toda la delegación vigente. Fuera de límites, detener y solicitar consentimiento.

Fuentes fijadas: [modelo de autorización](https://github.com/google-agentic-commerce/AP2/blob/e1ea56db72a6385bce3e5c1112b3a56ce60acb43/docs/ap2/agent_authorization.md),
[Checkout constraints](https://github.com/google-agentic-commerce/AP2/blob/e1ea56db72a6385bce3e5c1112b3a56ce60acb43/docs/ap2/checkout_mandate.md),
[Payment constraints](https://github.com/google-agentic-commerce/AP2/blob/e1ea56db72a6385bce3e5c1112b3a56ce60acb43/docs/ap2/payment_mandate.md),
[Double Spend](https://github.com/google-agentic-commerce/AP2/blob/e1ea56db72a6385bce3e5c1112b3a56ce60acb43/docs/ap2/security_and_privacy_considerations.md),
[flujo HNP](https://github.com/google-agentic-commerce/AP2/blob/e1ea56db72a6385bce3e5c1112b3a56ce60acb43/docs/ap2/flows.md),
[Agent Key y Mandate Management](https://github.com/google-agentic-commerce/AP2/blob/e1ea56db72a6385bce3e5c1112b3a56ce60acb43/docs/ap2/implementation_considerations.md).

### Spike que decide ACP frente a UCP para HNP

El helper fijado `CheckoutMandateChain.extract_parsed_checkout_object()` espera un checkout UCP
como payload JWT, con su modelo y estado `ready_for_complete`. El merchant local firma un wrapper
con `checkout: <snapshot ACP>`, revisión y claims JWT; ACP usa `ready_for_payment`. Activar el helper
sin adaptación no demuestra verificación correcta. Evidencia local: `MerchantCheckoutSigner.sign`
en `tesis-commerce-merchant/src/commerce_lab/ap2_checkout.py`.

Probar un adaptador determinista de restricciones sobre JWT verificado, conservando el hash de
los bytes originales, o elegir otra representación firmada explícita. Comparar el costo/claridad
contra UCP. La prueba debe aceptar cadena válida y rechazar cambio de total, merchant, variante,
cnf, vínculo abierto/cerrado y constraint desconocido en ambos verificadores. Si falla, no congelar
ACP solo por existir en B0. Fuente: [helper del SDK fijado](https://github.com/google-agentic-commerce/AP2/blob/e1ea56db72a6385bce3e5c1112b3a56ce60acb43/code/sdk/python/ap2/sdk/checkout_mandate_chain.py),
[extensión AP2 UCP](https://ucp.dev/2026-08-25/specification/payment/extensions/ap2-mandates/).

### Frescura: brecha actual independiente del motor de políticas

GET devuelve checkout almacenado. UPDATE recotiza, pero incrementa revisión y `updated_at` aun
sin cambio comercial. La huella HP incluye timestamp y el JWT liga revisión: refrescar en cada
clic puede invalidar el consentimiento indefinidamente. El contrato propuesto debe preservar todo
contenido firmado/consentido ante cambios técnicos inocuos, registrar observación aparte y hacer
que complete confronte oferta/revisión vigente atómicamente dentro del merchant. Un cambio
material sigue las reglas HP/HNP descritas arriba. D17 exige prueba; no está implementado.

## Versiones y fuentes

El baseline usa ACP `2026-04-17` y AP2 v0.2 fijado al commit indicado abajo. Las páginas consultadas
de UCP `2026-08-25`, MCP `2026-07-28` y A2A `1.0.0` sirven para la investigación; no se adoptan por
consultarlas. Al cerrar el diseño se debe registrar el perfil exacto y, cuando corresponda, commit
y digest de esquemas/SDK. Una versión nueva no reemplaza automáticamente el baseline experimental.

1. ACP: [arquitectura y separación Checkout / Delegate Payment](https://www.agenticcommerce.dev/docs/concepts/architecture).
2. UCP: [overview `2026-08-25`, capacidades, extensiones y transportes](https://ucp.dev/2026-08-25/specification/overview/).
3. AP2: [especificación v0.2, commit `e1ea56d`](https://github.com/google-agentic-commerce/AP2/blob/e1ea56db72a6385bce3e5c1112b3a56ce60acb43/docs/ap2/specification.md).
4. MCP: [arquitectura `2026-07-28`](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture).
5. A2A: [especificación oficial](https://a2a-protocol.org/latest/specification/) — al consultar, identifica `1.0.0` como versión publicada.
6. Visa: [Trusted Agent Protocol, especificaciones](https://developer.visa.com/capabilities/trusted-agent-protocol/trusted-agent-protocol-specifications/).
7. ACP: [OpenAPI `2026-04-17`, commit del bundle local](https://github.com/agentic-commerce-protocol/agentic-commerce-protocol/blob/7fdd78df677a94dce04c770644b0fbbb1401272b/spec/2026-04-17/openapi/openapi.agentic_checkout.yaml).

Fuentes de la implementación: [README del agente](../../README.md),
[perfil AP2](../ap2-profile.md), [compatibilidad](../compatibility.md),
[provenance ACP](../../src/commerce_agent/vendor/acp/provenance.json),
`tesis-commerce-merchant/README.md` y `tesis-payment-provider-sandbox/README.md`.
