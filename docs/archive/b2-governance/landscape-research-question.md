# Panorama 2026 y pregunta de investigación

Estado: síntesis de investigación para validar con el asesor  
Fecha de corte: 2026-10-08  
Alcance: agentes de compra B2C, Human Present, checkout autoritativo y pagos sintéticos

## Conclusión ejecutiva

Ya existen soluciones maduras o emergentes para casi todas las piezas aisladas del comercio
agéntico: descubrimiento y checkout, identidad del agente, prueba de consentimiento, credenciales de
pago acotadas, policy enforcement, observabilidad y auditoría. No encontramos evidencia pública de
una solución única, interoperable y evaluada reproduciblemente que coordine todas esas garantías
durante una compra cuyo estado cambia y que mida su valor incremental sobre un flujo Human Present
que ya usa AP2.

Por tanto, la tesis no debe presentar como novedad “usar mandatos”, “agregar políticas” o “poner un
humano antes del pago”. Su aporte defendible es **diseñar y evaluar la coordinación temporal entre
decisión, estado comercial, consentimiento y efecto**, manteniendo AP2 y el protocolo comercial como
infraestructura existente.

## Qué existe hoy

| Capa | Soluciones representativas | Qué resuelven | Qué no resuelven por sí solas |
|---|---|---|---|
| Comercio y checkout | OpenAI ACP; Google/Shopify UCP; Shopify Catalog API | Descubrimiento, carrito, checkout, handoff y orden | No deciden toda la autoridad del comprador ni reconstruyen la decisión del agente |
| Consentimiento verificable | Google AP2; Mastercard Verifiable Intent; trabajo FIDO | Vinculan intención/consentimiento con checkout y pago; generan evidencia verificable | No determinan cómo el agente interpretó, comparó o eligió; AP2 deja el comercio fuera de alcance |
| Identidad del agente | Visa TAP; Web Bot Auth; Microsoft Entra Agent ID | Reconocimiento, identidad, claves, actor y ciclo de vida | Identidad válida no equivale a autoridad válida para una compra concreta |
| Credencial y riesgo de pago | Stripe Shared Payment Tokens; Mastercard Agent Pay; Visa Intelligent Commerce | Credenciales acotadas por comercio, importe o tiempo; fraude y red de pagos | El límite monetario no prueba que el producto o los términos coincidan con la intención |
| Policy enforcement | AWS AgentCore Policy/Cedar; OPA; Invariant Guardrails | Decisiones externas, control de tool calls, data flow y fail closed | Son genéricos; el integrador debe modelar estado de checkout, consentimiento y evidencia comercial |
| Gobierno empresarial | Microsoft Agent 365; IBM watsonx.governance | Inventario, identidad, propietarios, ciclo de vida, observabilidad y cumplimiento | No constituyen por sí solos una prueba transaccional de selección–consentimiento–efecto |
| Startups y protocolos emergentes | Kachyng, Skyfire, Catena ACK, Nevermined | Wallets, identidad, límites, acceso, pagos o recibos para agentes | Cobertura y madurez heterogéneas; predominan piezas de pago o acceso, no una evaluación integral |
| Estándares en desarrollo | NIST NCCoE; IETF Intent Admission y Authorization Evidence; EMVCo | Lenguaje común para identidad, delegación, evidencia, frescura y auditoría | Trabajo aún en evolución; no reemplaza la validación empírica de una arquitectura concreta |

UCP 2026-08 ya permite adjuntar `policies[]` a catálogo, carrito, checkout y orden. Esas políticas
describen términos del negocio —por ejemplo, devolución o garantía—; no son equivalentes a un PDP
que decide si el agente tiene autoridad para ejecutar. AP2 v0.2, por su parte, exige que Human
Present apruebe un checkout cerrado y que los verificadores comprueben los hashes y restricciones,
pero deja fuera de alcance cómo se obtiene la intención y cómo se implementa el protocolo comercial.

## Brecha de investigación

La brecha relevante es **cross-layer y temporal**:

1. la intención se interpreta antes de conocer todos los términos;
2. el agente selecciona una opción usando datos que pueden ser no confiables;
3. el merchant mantiene el estado comercial autoritativo;
4. la persona aprueba una representación concreta;
5. precio, disponibilidad, revisión o políticas pueden cambiar antes del efecto;
6. después se necesita reconstruir por qué el sistema consideró autorizada la acción.

Los protocolos distribuyen estas responsabilidades entre actores y etapas. La literatura reciente
coincide en que una única credencial o mensaje no puede garantizar por sí solo la consistencia entre
autoridad delegada y resultado económico. Lo que falta medir en nuestro contexto es cuánto aporta
una capa externa que coordine esas dependencias sobre un sistema que ya tiene controles
deterministas y AP2.

## Pregunta de investigación recomendada

> **En un flujo B2C Human Present de comercio agéntico que ya utiliza checkout autoritativo y AP2,
> ¿en qué medida una capa externa de gobernanza de decisiones que valida la selección contra
> restricciones explícitas, revalida las dependencias comerciales al momento de la aprobación y
> conserva evidencia enlazada de decisión–consentimiento–efecto reduce las violaciones de autoridad
> y mejora la auditabilidad frente al mismo flujo sin esa capa, y cuál es su costo en utilidad y
> latencia?**

### Por qué esta formulación

- reconoce que AP2, Human Present y los controles deterministas ya existen;
- estudia valor incremental, no atribuye novedad a un protocolo o producto;
- delimita población, intervención, comparación y resultados;
- permite implementar el PDP con OPA sin convertir OPA en la pregunta;
- conecta directamente con S04 y con la baja completitud de evidencia de B0;
- permite reportar un resultado negativo si la capa no agrega valor o agrega demasiado costo.

## Operacionalización

| Elemento | Definición experimental |
|---|---|
| Contexto | Compra B2C sintética, Human Present, checkout merchant-autoritativo y AP2 |
| Control B0 | Agente, prompt, modelo, catálogo, ACP, AP2 y validaciones actuales |
| Tratamiento B2 | Mismo sistema + capa de gobernanza explícita, D1 selección, revalidación de frescura, D2 autorización y evidencia enlazada |
| Variable primaria 1 | Violaciones de autoridad / oportunidades de violación |
| Variable primaria 2 | Episodios con cadena de evidencia completa y verificable |
| Utilidad | Resultado válido del escenario sin bloqueos incorrectos |
| Costo | Latencia end-to-end y overhead de gobernanza p50/p95/p99; llamadas y fallos |
| Resultados diagnósticos | Recuperación, explicación canónica, replay, prompt injection e idempotencia |

Una **violación de autoridad** incluye aceptar una selección o aprobación que no corresponde a las
restricciones, al principal o al estado vigente, aunque una barrera posterior impida finalmente el
pago. Esta definición conserva S04: que el merchant bloquee `complete()` evita el efecto económico,
pero no vuelve válida una aprobación sobre términos obsoletos.

## Hipótesis

### H1 — Seguridad y control

B2 reducirá las violaciones de autoridad por oportunidad frente a B0, en particular ante cambios de
estado entre presentación y aprobación.

### H2 — Auditabilidad

B2 aumentará la proporción de episodios en los que un verificador puede enlazar intención,
restricciones, candidato elegido, decisión de política, checkout mostrado, aprobación, mandates,
recibos y resultado.

### H3 — No inferioridad de utilidad

B2 no reducirá de forma material el éxito de compras válidas. El margen de no inferioridad se fija
después del piloto de variabilidad y antes de abrir el corpus reservado.

### H4 — Costo operacional

B2 añadirá overhead medible de latencia y disponibilidad. El presupuesto aceptable se congela antes
de la evaluación principal; no se selecciona después de ver los resultados.

## Qué no afirma la tesis

- que OPA sea superior universalmente a Cedar o a controles tipados;
- conformidad integral con ACP, UCP, AP2, TAP, EMVCo o FIDO;
- seguridad de producción, cumplimiento jurídico o eliminación total de prompt injection;
- que mandatos, Human Present, policy-as-code o registros append-only sean invenciones propias;
- validez para Human Not Present, B2B o agentes múltiples sin experimentos separados.

## Contribución esperada

1. Un perfil de integración reproducible entre protocolo comercial, AP2 y control externo de
   decisiones.
2. Un modelo explícito de dependencias protegidas que invalida decisiones cuando cambia el estado
   autoritativo.
3. Una cadena de evidencia que permite reconstruir selección, consentimiento y efecto.
4. Un experimento emparejado B0/B2 con oráculos deterministas, utilidad, seguridad, auditabilidad y
   costo reportados por separado.
5. Un corpus pequeño pero extensible de fallos temporales y cross-layer en comercio agéntico.

## Riesgo metodológico y control recomendado

Si B2 incluye además mejoras de UX no relacionadas con gobernanza —por ejemplo, convertir un 502
en una explicación útil cuando no hay candidato— no debe afirmarse que OPA causó esa mejora. Hay dos
opciones válidas:

1. declarar B2 como un **paquete de gobernanza** y atribuir resultados al paquete completo; o
2. aplicar las correcciones no relacionadas con gobernanza a un control común y aislar el efecto de
   D1/D2/evidencia.

Para el alcance del semestre se recomienda la primera, acompañada de resultados por mecanismo y sin
afirmaciones causales sobre OPA. Si el tiempo permite una tercera condición, un control endurecido
sin PDP permitiría una atribución más fuerte.

## Fuentes principales consultadas

- OpenAI, [Agentic Commerce: conceptos](https://developers.openai.com/commerce/guides/key-concepts) y
  [guardrails y aprobaciones](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals).
- Google, [AP2 v0.2](https://github.com/google-agentic-commerce/AP2/blob/main/docs/ap2/specification.md).
- Shopify/Google, [UCP 2026-08](https://www.shopify.com/ucp/2026-08) y
  [`policies[]` en UCP](https://github.com/Universal-Commerce-Protocol/ucp/pull/572).
- Mastercard, [Verifiable Intent](https://www.mastercard.com/us/en/news-and-trends/stories/2026/verifiable-intent.html)
  y [Trust for agentic commerce](https://www.mastercard.com/global/en/news-and-trends/Insights/2026/mastercard-agentic-commerce-trust-white-paper.html).
- Visa, [Trusted Agent Protocol](https://developer.visa.com/capabilities/trusted-agent-protocol/trusted-agent-protocol-specifications/).
- Stripe, [Shared Payment Tokens y Agentic Commerce](https://stripe.com/blog/introducing-our-agentic-commerce-solutions).
- AWS, [AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-scope.html).
- Microsoft, [Entra Agent ID y Agent 365](https://learn.microsoft.com/en-us/microsoft-agent-365/guidance/entra-agent-365).
- IBM, [Agentic Control Plane](https://www.ibm.com/products/watsonx-orchestrate/governance-and-observability).
- NIST NCCoE, [Agent Identity and Authorization](https://pages.nist.gov/nccoe-ai-identity/) y
  [síntesis de comentarios](https://pages.nist.gov/nccoe-ai-identity/summary-of-comments.html).
- IETF, [Intent Admission Assertions](https://datatracker.ietf.org/doc/draft-jiang-oauth-intent-admission/)
  y [Authorization Evidence](https://datatracker.ietf.org/doc/draft-liu-oauth-authorization-evidence/).
- EMVCo, [framework de agentic payments](https://www.emvco.com/news/emvco-requests-feedback-on-framework-for-secure-interoperable-and-scalable-card-based-agentic-payments/).
- Jiang et al., [A Formal Analysis of Agent Payment Protocols](https://arxiv.org/abs/2609.00060).
- Sfiris, [A Decision-Centered Reference Architecture for Trustworthy Agentic Commerce](https://arxiv.org/abs/2607.18347).
- Mao et al., [SoK: Security of Autonomous LLM Agents in Agentic Commerce](https://arxiv.org/abs/2604.15367).
