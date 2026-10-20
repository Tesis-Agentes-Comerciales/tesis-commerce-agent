# B2: agente de compra con gobernanza de decisión

Estado: enfoque de gobernanza aprobado; arquitectura final en elaboración  
Actualización: 2026-10-11, con reporte de la reunión del 2026-10-09  
Base experimental: `b0-baseline-2026-10-06`

## Acuerdo más reciente

Según el reporte de Yair sobre la reunión del 9 de octubre, la gobernanza recibió el visto bueno.
El comentario pendiente es investigar los protocolos con mayor profundidad y justificar cuáles
y qué partes necesita el sistema. El trabajo inmediato es cerrar la arquitectura y el system
design para comenzar a implementar aproximadamente una semana después de esta actualización.

El visto bueno corresponde al enfoque de gobernanza; no se interpreta como aprobación de OPA,
del número de decisiones, de un protocolo particular ni de todos los contratos del borrador.
El diseño detallado está activo. Véanse el [registro y plan de cierre](design-closure-2026-10-11.md)
y la [investigación de protocolos](protocol-research.md).

Yair confirmó en esta conversación que la arquitectura final debe cubrir **Human Present y
Human Not Present**. El [roadmap de diseño](system-design-v1/plan.mdx) y su
[registro de decisiones](system-design-v1/decisions.json) desarrollan ambos modos,
con 50 decisiones, dependencias, artefactos y criterios de cierre. B2B queda como dimensión
independiente. HNP todavía no está implementado en la base actual. Vista estable:
[roadmap en Notion](https://app.notion.com/p/3f965dca59e781f393ebd77838dba95b).

Los documentos P1–P5, el diagrama y `evaluation/data/b2-design.json` conservan la propuesta original.
La revisión D1/D2 es el punto de partida HP. El roadmap añade autorización delegada y D3-HNP
junto al compromiso de despacho. Todavía hay que reconciliar contratos, evaluación, diagramas
y manifiesto antes de usarlos para implementar; las secciones P1–P5 conservadas abajo son históricas.

## Propósito

B2 añade gobernanza verificable a una base equivalente por modo. El B0 congelado actual cubre
Human Present. Human Not Present exige primero construir su base con consentimiento delegado,
cadenas AP2, reservas de uso y revocación. La comparación B0/B2 conservará modelo, prompt, catálogo,
perfil comercial/AP2, proveedor sintético y controles comunes dentro de cada modo. No se atribuirá
a gobernanza la diferencia entre una compra HP y otra HNP.

B2 no pretende que el LLM sea juez de sus propias acciones. Las decisiones con efecto se construyen
con estado tipado y se evalúan mediante reglas deterministas fuera del modelo. AP2 sigue siendo la
capa de autorización y evidencia de la transacción; la gobernanza B2 decide si el flujo puede llegar
a esa capa.

![Arquitectura B2 con gobernanza Human Present](../diagrams/architecture-b2-hp.svg)

Arquitectura vigente en `org/main` al 20 de octubre, después del piloto B2: lo hecho, lo
planificado (piezas en `main` que el flujo todavía no usa) y lo futuro. El índice de diagramas está en [docs/diagrams](../diagrams/README.md). El diagrama
P1–P5 original (OPA, Trusted State) quedó como histórico en
[`historico/b2-architecture-p1-p5.svg`](../diagrams/historico/b2-architecture-p1-p5.svg).

## Decisión principal

> La arquitectura P1–P5 descrita abajo es la propuesta original. La revisión de diseño del
> 2026-10-07 recomienda simplificarla antes de implementar: dos decisiones explícitas de
> gobernanza, controles deterministas para flujo/frescura y AP2 + merchant como barrera final.
> Véase [Revisión crítica del diseño](design-review.md).

- **Motor de políticas:** OPA/Rego detrás de un puerto `PolicyEvaluator`, con bundle fijado por
  versión y digest.
- **Resultado canónico:** `allow`, `deny` o `ask_for_authority`, acompañado de códigos de razón y
  obligaciones estructuradas.
- **Reglas dinámicas:** una guarda de estado en Python verifica vigencia, revisión y huella del
  checkout. Un motor declarativo no puede volver vigente una cotización obsoleta.
- **Fallo:** cerrado. Timeout, respuesta inválida, bundle ausente o política indefinida producen
  `deny` con una explicación segura y reintentable.
- **Despliegue experimental:** replay offline → modo sombra → piloto B2 en enforcement → evaluación
  ampliada.

## Documentos

- [Reunión del 9 de octubre y plan para cerrar el diseño](design-closure-2026-10-11.md)
- [Investigación de protocolos: capacidades, perfil y decisiones](protocol-research.md)
- [Revisión crítica del diseño](design-review.md)
- [Panorama 2026 y pregunta de investigación](landscape-research-question.md)
- [Síntesis de investigación](research-synthesis.md)
- [Arquitectura y flujo](architecture.md)
- [Diagramas B0, B2-HP, delta, secuencia gobernada, HNP y piloto](../diagrams/README.md)
- [Convención visual](visual-style.md)
- [Contrato de política](policy-contract.md)
- [Modelo de amenazas](threat-model.md)
- [Plan de evaluación](evaluation-plan.md)
- [Plan de implementación](implementation-plan.md)
- [Manifiesto legible por máquina](../../evaluation/data/b2-design.json)
- [Reportes de ejecución EC-GOV (históricos)](../archive/b2-governance/README.md)

Actualización 2026-10-19, posterior a la integración HP:

- [D1 enforce opt-in en el runtime](d1-enforce-runtime-optin.md): configuración, deny en vivo (POL-02) y límites (PR #13).
- [EC-GOV-06B, sección M: adenda de runtime, reintento y latencias](../archive/b2-governance/ec-gov-06b-d1-enforce-results.md): mediciones antes y después del PR #13 en la misma máquina. Conserva la tabla original.
- [Handoff de integración HP del 17-oct](hp-integration-handoff-2026-10-17.md), con notas fechadas sobre enforce, D17, D2 y la reproducibilidad de la evidencia live.

## Qué cambia frente a B0

| Aspecto | B0 congelado | B2 gobernado |
|---|---|---|
| Filtros duros | Deterministas, antes de seleccionar | Se conservan; la política verifica que la elección pertenezca al conjunto elegible |
| Selección | LLM elige entre candidatos válidos | Igual, pero la elección no progresa sin decisión de política |
| Falta de autoridad | Recuperación conversacional ad hoc | Resultado explícito `ask_for_authority` con campos requeridos |
| Checkout | Se valida al prepararlo | Se valida al prepararlo, al mostrarlo, al aprobarlo y antes del efecto |
| Precio modificado | La instantánea del checkout puede quedar obsoleta | `OfferFreshnessGuard` exige revisión vigente o invalida la aprobación |
| Explicación | Texto del agente o error técnico | Código de razón estable + mensaje determinista para el usuario |
| Evidencia | Trazas parciales de aplicación | Registro append-only con decisión, bundle, hashes, razones, obligaciones y latencia |
| Disponibilidad de la política | No aplica | Fallo cerrado; nunca se degrada silenciosamente a B0 |

## Invariantes

1. El modelo no recibe tokens, mandatos, identidad autoritativa, evidencias de aprobación ni claves
   de idempotencia.
2. El modelo no tiene una herramienta para aprobar, pagar, completar ni modificar políticas.
3. Catálogo, descripciones y respuestas de herramientas son datos no confiables; no otorgan
   autoridad.
4. HP requiere consentimiento sobre términos exactos; HNP requiere consentimiento previo sobre
   límites explícitos en una Trusted Surface y cierre posterior por una clave de agente autorizada.
5. En HP, un cambio material requiere nueva presentación. En HNP, términos nuevos deben satisfacer
   la delegación vigente; fuera de sus límites se detiene o solicita consentimiento nuevo.
6. En enforcement ningún efecto avanza sin autoridad vigente, evidencia vinculada y verificaciones
   AP2 válidas. Revocación, reserva y compromiso HNP tienen orden durable explícito.
7. Los artefactos de evaluación no contienen secretos ni tokens de pago.

## Fuera del alcance de B2

- pagos reales, 3DS, reembolsos o liquidación;
- compras recurrentes, subdelegación o presupuesto empresarial compartido, salvo ampliación explícita;
- certificación ISO 42001 o conformidad integral con NIST AI RMF;
- aprendizaje automático de políticas;
- múltiples agentes, delegación entre agentes o memoria de largo plazo;
- reemplazar filtros deterministas, consentimiento humano o verificación AP2.
