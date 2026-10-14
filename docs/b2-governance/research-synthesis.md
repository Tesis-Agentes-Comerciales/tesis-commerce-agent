# Síntesis de investigación para B2

Fecha de corte: 2026-10-06. Se priorizaron estándares, documentación oficial, artículos técnicos de
los equipos que construyen los sistemas y trabajos académicos con artefactos reproducibles. Los
posts de proveedores se usan como patrones de ingeniería, no como prueba independiente de eficacia.

## Conclusiones que cambian el diseño

1. **La gobernanza debe vivir en el límite del efecto.** OpenAI recomienda evaluar herramienta,
   argumentos, identidad y contexto antes de ejecutar, y fallar cerrado si el revisor no responde.
   Esto respalda los puntos P2–P5 de B2, no una única revisión del prompt.
2. **El modelo no debe ser el árbitro final.** Microsoft describe la capa de aplicación como la que
   convierte comportamiento probabilístico en resultados deterministas. Anthropic recomienda
   privilegio mínimo, pausas ante ambigüedad y defensa en profundidad. B2 mantiene selección
   agéntica, pero separa decisión, autoridad y ejecución.
3. **Autorización declarativa y estado dinámico son controles distintos.** Cedar y OPA son adecuados
   para reglas reproducibles; el patrón de AWS combina política con interceptores cuando la regla
   requiere estado vivo. Por eso la vigencia del precio se verifica en `OfferFreshnessGuard` y la
   autorización se expresa en OPA.
4. **Los flujos importan más que llamadas aisladas.** Invariant Labs modela “toxic flows” a través
   de secuencias de herramientas. MITRE ATLAS y OWASP incluyen abuso de herramientas, identidad,
   memoria, configuración, comunicación y fallos en cascada. B2 evalúa la traza completa y exige que
   las decisiones previas sigan vigentes al completar.
5. **La defensa contra prompt injection no puede depender solo del modelo.** Google DeepMind reporta
   que las defensas estáticas se degradan frente a ataques adaptativos. CaMeL separa control y datos
   y aplica capacidades en una capa de sistema. En B2, el texto del catálogo nunca se convierte en
   hecho de política ni instrucción.
6. **La evidencia debe explicar qué regla actuó.** Los decision logs de OPA incluyen consulta,
   entrada, resultado, revisión del bundle, `decision_id` y `trace_id`. B2 adopta ese núcleo, con
   hashes y redacción en lugar de guardar secretos o payloads completos.
7. **AP2 y la gobernanza no compiten.** AP2 Human Present exige una superficie confiable,
   consentimiento explícito y mandates vinculados criptográficamente al checkout. B2 decide si es
   lícito presentar o ejecutar ese checkout; AP2 prueba qué términos fueron autorizados.
8. **La evaluación debe separar seguridad y utilidad.** AgentDojo y ToolEmu miden éxito de tarea y
   fallos de seguridad bajo ataques o fallas de herramientas. B2 añade falsos bloqueos, calidad de
   explicación, completitud de evidencia y costo de latencia.

## Matriz de fuentes y adopción

| Fuente primaria | Hallazgo útil | Decisión en B2 |
|---|---|---|
| [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework) y [perfil GenAI NIST AI 600-1](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence) | Gobernar, mapear, medir y gestionar durante el ciclo de vida | Riesgos, controles, métricas y responsables quedan trazables en el plan |
| [ISO/IEC 42001 — resumen oficial](https://www.iso.org/artificial-intelligence/ai-management-systems) | Gestión continua tipo PDCA, no control puntual | B2 incluye observación, enforcement y revisión versionada; no se afirma certificación |
| [OWASP Agentic Threats](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/) | Goal hijack, tool misuse, privilegio, memoria, supply chain y fallos en cascada | Amenazas se mapean a controles y pruebas concretas |
| [MITRE ATLAS](https://atlas.mitre.org/) | Taxonomía viva de técnicas adversarias contra sistemas de IA | IDs ATLAS se usan como procedencia de casos adversarios |
| [OpenAI: guardrails y approvals](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals) | Control justo antes del side effect, contexto rico y fallo cerrado | Puntos P2–P5; timeout/resultado inválido = `deny` |
| [OpenAI: seguridad en Agent Builder](https://developers.openai.com/api/docs/guides/agent-builder-safety) | Datos no confiables no deben conducir directamente el comportamiento; usar salidas estructuradas | Contratos Pydantic, separación de datos y autoridad, modelo sin acceso al pago |
| [OpenAI: agent evals](https://developers.openai.com/api/docs/guides/agent-evals) | Evaluar trazas completas, tool calls y guardrails | Evidencia por fase y comparación emparejada B0/B2 |
| [Anthropic: Building and Evaluating Trustworthy Agents](https://www.anthropic.com/research/trustworthy-agents) | Pausar ante ambigüedad, privilegio mínimo y defensa en profundidad | `ask_for_authority`, herramientas mínimas y controles independientes |
| [Google SAIF 2.0](https://blog.google/innovation-and-ai/technology/safety-security/ai-security-frontier-strategy-tools/) | Controlador humano definido, poderes limitados y acciones observables | Propietario de compra autenticado, superficie Human Present y registro auditable |
| [Google DeepMind: defensas ante prompt injection](https://deepmind.google/discover/blog/advancing-geminis-security-safeguards/) | Las defensas adaptativas requieren múltiples capas y evaluación continua | Se prueba contenido adaptativo; política y taint no dependen del clasificador del modelo |
| [Microsoft: threat modeling de aplicaciones de IA](https://www.microsoft.com/en-us/security/blog/2026/02/26/threat-modeling-ai-applications/) | Nondeterminismo, herramientas y memoria cambian el modelo de amenazas | Se analizan flujos, identidad, herramientas y persistencia, no solo prompts |
| [Microsoft: defense in depth para agentes](https://www.microsoft.com/en-us/security/blog/2026/05/14/defense-in-depth-autonomous-ai-agents/) | La aplicación debe imponer resultados deterministas | El `PolicyGateway` y las guardas de estado son autoritativos |
| [OPA decision logs](https://www.openpolicyagent.org/docs/management-decision-logs) | Decisión correlacionable con entrada, resultado y revisión de política | Registro de decisiones con digest, hashes, razón, latencia y `trace_id` |
| [Cedar authorization](https://docs.cedarpolicy.com/auth/authorization.html) | Default deny, forbid-wins, validación por esquema | Se conserva como alternativa del puerto; no se usa en B2 porque la salida requerida incluye obligaciones |
| [AWS AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-core-concepts.html) | Política por tool call, default deny y esquemas | Reglas tipadas y evaluación en cada frontera relevante |
| [AWS: Policy + interceptors](https://aws.amazon.com/blogs/machine-learning/secure-ai-agents-with-policy-and-lambda-interceptors-in-amazon-bedrock-agentcore-gateway/) | Política determinista más interceptores para datos dinámicos | OPA para autorización; guarda Python para vigencia del checkout |
| [NVIDIA NeMo Guardrails](https://docs.nvidia.com/nemo/guardrails/about-nemo-guardrails-library/overview) | Validación de tool calls, aislamiento de autenticación y trazabilidad | Patrón adoptado; NeMo no se agrega porque duplicaría el runtime actual |
| [Invariant: toxic flow analysis](https://invariantlabs.ai/blog/toxic-flow-analysis) | Riesgo emergente en secuencias de datos y herramientas | Revalidación acumulativa antes de completar y pruebas de secuencia |
| [AgentDojo](https://arxiv.org/abs/2406.13352) | Benchmark de tareas y ataques indirectos con herramientas realistas | Casos adversarios reproducibles y métricas separadas de utilidad/seguridad |
| [CaMeL](https://arxiv.org/abs/2503.18813) | Separación de control/datos y capacidades fuera del LLM | Proveniencia tipada; datos de catálogo no pueden otorgar autoridad |
| [ToolEmu](https://openreview.net/pdf?id=GEcwtMk1uA) | Fallos de larga cola en herramientas y evaluación en sandbox | Pruebas de timeout, respuestas inválidas y fallos parciales |
| [UK AISI: sandboxing de agentes](https://www.aisi.gov.uk/blog/the-inspect-sandboxing-toolkit-scalable-and-secure-ai-agent-evaluations) | Separar inferencia, herramientas, host y red | Las pruebas no usan credenciales reales y corren contra servicios sintéticos |
| [AP2 v0.2](https://github.com/google-agentic-commerce/AP2/blob/main/docs/ap2/specification.md) | Trusted Surface, consentimiento explícito y mandates vinculados al checkout | Se preserva Human Present y se invalida cualquier aprobación de términos cambiados |

## Recursos audiovisuales seleccionados

Estos recursos sirven para profundizar el marco teórico y preparar la defensa; no reemplazan la
especificación ni los artículos enlazados arriba.

| Recurso | Por qué vale la pena |
|---|---|
| [NIST — introducción oficial al AI RMF](https://www.nist.gov/video/introduction-nist-ai-risk-management-framework-ai-rmf-10-explainer-video) | Vocabulario común de riesgo y ciclo de vida |
| [OWASP — State of Agentic AI Security and Governance v2](https://www.youtube.com/watch?v=7oB9PTTV-9E) | Panorama 2026 de amenazas y madurez de controles |
| [Stanford Real-World AI Security 2026 — programa](https://seclab.stanford.edu/RealWorldAIsec/program-26.html) | Curaduría universitaria con investigadores y equipos industriales |
| [Edoardo Debenedetti — Evaluating and Defending Against Prompt Injection](https://www.youtube.com/watch?v=xW3n8sbeNsA) | Diseño de evaluación adaptativa de prompt injection |
| [OpenAI — Beyond Prompt Injection: Agentic AI Attacks in the Real World](https://www.youtube.com/watch?v=lgpz6oeHFKc) | Casos de ataque que cruzan datos, herramientas y ejecución |
| [Vitaly Shmatikov — The Road to Hell Is Paved with Helpful Agents](https://www.youtube.com/watch?v=d-stad0eQaY) | Perspectiva académica sobre el riesgo de agentes útiles con exceso de autoridad |
| [Microsoft AI Show — red teaming de agentes](https://learn.microsoft.com/en-us/shows/ai-show/ai-red-teaming-agent-in-azure-ai-foundry) | Ejemplo práctico de ataque, evaluación y lectura de resultados |

## Qué no se adopta todavía

- **NeMo Guardrails:** sus patrones son útiles, pero introducir otro runtime de guardrails reduce la
  claridad causal del experimento. B2 implementa controles mínimos con OPA y guardas locales.
- **Cedar como motor principal:** es fuerte para autorización principal–acción–recurso y se mantiene
  como alternativa futura. B2 necesita respuestas con obligaciones, campos faltantes y razones
  operativas; Rego ofrece una salida estructurada más natural.
- **LLM-as-a-judge para enforcement:** puede apoyar análisis offline, pero no autoriza efectos.
- **Políticas aprendidas:** dificultarían reproducibilidad, auditoría y atribución causal.
- **Automatización Human Not Present:** cambia el modelo de consentimiento y queda fuera del alcance.

## Limitaciones de la revisión

No es una revisión sistemática exhaustiva ni una certificación. El panorama agentic cambia rápido;
la versión de cada fuente y la fecha de corte deben conservarse con los artefactos. Las afirmaciones
de proveedores requieren validación en el prototipo y no se tratan como resultados independientes.
