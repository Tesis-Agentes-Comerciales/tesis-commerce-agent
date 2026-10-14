# Reunión del 9 de octubre y cierre del system design

Registro: 2026-10-11. Fuente del acuerdo: reporte de Yair en esta conversación; no es una
transcripción ni un acta validada por los asistentes.

## Resultado de la reunión

- El balance de la reunión del 2026-10-09 fue positivo.
- La observación pendiente es investigar bien los protocolos y determinar cuáles y qué partes
  necesita el proyecto.
- El enfoque de gobernanza recibió el visto bueno.
- La prioridad pasa a diseñar la arquitectura final y cerrar el system design para poder
  empezar a implementar en aproximadamente una semana.

No se reportó aprobación de un protocolo concreto, de OPA, de D1/D2 ni de un alcance B2B.
Esas decisiones no se atribuyen a los asesores.

## Alcance confirmado después del reporte de reunión

Yair aclaró el 2026-10-11 que la arquitectura final debe cubrir **Human Present y Human Not
Present**. Es una instrucción de alcance en esta conversación, no un acuerdo adicional atribuido
a los asesores. HNP requiere delegación abierta, cierre de mandatos, gestión de uso/revocación y
ejecución durable. B2B es independiente y no queda confirmado por esa aclaración.

El [roadmap detallado](system-design-v1/plan.mdx) es el documento de trabajo
actual; el [registro JSON](system-design-v1/decisions.json) contiene 50 decisiones
y sus dependencias. Incluye una prueba obligatoria del puente HNP ACP–AP2 antes de elegir ACP
definitivamente, porque el helper del SDK interpreta un checkout UCP.

## Objetivo de esta semana

Cerrar un paquete de diseño v1 que permita implementar B2 sobre la base existente sin tener
que decidir durante la programación quién autoriza una acción, qué contrato cruza cada frontera
o cómo se recupera una operación incierta.

Ventana de trabajo propuesta: **12–18 de octubre de 2026**. Inicio tentativo de implementación:
**19 de octubre**, condicionado al cierre del diseño. Estas fechas son una traducción de
«en una semana o así», no un compromiso de capacidad ni una nueva fecha acordada con asesores.
No se asignan responsables ni horas sin conocer la disponibilidad de los integrantes.

## Punto de partida y decisiones pendientes

El README del agente y los de merchant y proveedor describen una base B0 con ACP REST, perfil
AP2 Human Present, persistencia separada y pago sintético. El nuevo trabajo se concentra en el
diseño definitivo y la implementación de B2. Esta actualización revisa documentación; no acredita
una nueva ejecución del flujo integrado.

| Decisión | Punto de partida | Cierre necesario |
|---|---|---|
| Alcance funcional | HP existente; HP + HNP confirmados para diseño final | Cerrar límites de autonomía; B2B sigue independiente |
| Protocolos | ACP `2026-04-17` y AP2 v0.2 ya usados en B0 | Justificar módulos, versiones, exclusiones y puentes propios; contrastar UCP |
| Gobernanza | Enfoque aprobado; D1/D2 de partida HP | D2 por modo, D3-HNP/compromiso, hechos resueltos y dueño de cada control |
| Motor de políticas | OPA detrás de `PolicyEvaluator` es candidato | Elegirlo con una justificación y fijar versión, bundle y presupuesto de fallo/latencia |
| Evaluación | B0 congelado y cinco escenarios piloto | Precisar el tratamiento y separar la contribución de políticas, frescura y evidencia |

## Entregables del system design v1

| Entregable | Contenido necesario para implementar |
|---|---|
| Alcance y decisiones de arquitectura | Requisitos funcionales; límites de disponibilidad, latencia, persistencia y despliegue; alternativas y motivos de elección |
| Perfil de protocolos | Requisito → módulo → operación/campo → emisor/verificador → versión/fuente → prueba de aceptación; distinguir estándar, subconjunto y extensión propia |
| Arquitectura | Contexto, contenedores y componentes; dueño de cada dato; fronteras de confianza; ubicación de UI, agente, política, merchant, proveedor y almacenes |
| Contratos y modelo de datos | Interfaces y errores; decisión de política; intención, checkout, aprobación, mandatos, operación, orden, recibos y evidencia; identificadores, relaciones y retención |
| Estados y secuencias | Compra válida, ambigüedad, cero candidatos, cambio de términos, rechazo, timeout, respuesta perdida, replay, concurrencia y fallo del motor o de persistencia |
| Operación | Configuración, claves y confianza; readiness; reinicio, reconciliación e idempotencia; límites del prototipo de una instancia |
| Evaluación y backlog | Invariantes, oráculos, métricas y amenazas trazables; tareas por repositorio, dependencias y criterio de terminado |

Los diagramas deben coincidir con los contratos y las secuencias. Una caja «gobernanza» sin
entradas, decisión, punto de aplicación y efecto observable no cierra el diseño.

## Secuencia propuesta

| Fecha | Trabajo | Resultado revisable |
|---|---|---|
| 12 oct. | Mapear requisitos y leer ACP/AP2 por operaciones y actores; inventariar lo existente | Matriz de capacidades con fuentes y brechas |
| 13 oct. | Contrastar UCP; evaluar necesidad de MCP, A2A y TAP; resolver versiones y extensiones | Perfil de protocolos v1 propuesto y decisiones justificadas |
| 14 oct. | Cerrar alcance, componentes, fronteras y dueño de cada control; decidir D1/D2 y motor | Arquitectura y registros de decisión |
| 15 oct. | Especificar contratos, datos, estados, frescura e idempotencia | Secuencias de éxito y fallos con payloads de ejemplo |
| 16 oct. | Cerrar evidencia, operación, amenazas y comparación experimental | Matriz de invariantes, métricas y aceptación |
| 17 oct. | Reconciliar documentos, diagramas y manifiesto; derivar tareas entre repositorios | Paquete de diseño coherente y backlog por dependencias |
| 18 oct. | Recorrer una compra y los fallos contra el diseño; resolver contradicciones | Diseño v1 congelado o lista concreta de decisiones que impiden el inicio |

La investigación se acota a preguntas de arquitectura. Cada lectura debe terminar en una decisión,
una capacidad reutilizable o una brecha explícita, con su impacto sobre el sistema.

## Condición para empezar a implementar

1. Cada protocolo incluido tiene una necesidad concreta y un perfil/versionado fijado. Los demás
   tienen una exclusión justificada o una condición clara para incorporarlos.
2. D1/D2 por modo y D3-HNP o su alternativa están definidos; la aplicación, la política y el merchant tienen
   responsabilidades explícitas. Pedir datos al usuario se distingue de autorizar una compra.
3. Se especifica cómo obtener términos actuales del merchant y cómo invalidar la aprobación.
   Leer el checkout almacenado no se considera prueba suficiente de frescura del catálogo.
4. El vínculo aprobación → mandatos → operación → orden/recibos está especificado, incluidos
   concurrencia, idempotencia y recuperación después de un timeout.
5. Están definidos los efectos de un fallo de OPA, una salida inválida o la ausencia de evidencia.
6. La evaluación identifica qué añade B2 y qué comparten B0/B2. Si cambia el perfil de protocolos,
   se aplica el cambio a ambas condiciones y se genera una nueva base, o se evalúa por separado;
   no se atribuye ese cambio a gobernanza.
7. HNP tiene definidos consentimiento, claves emisor/Agent Key, cadenas, límites, reservas,
   revocación frente a compromiso, worker y fallos entre verificadores.
8. La base HNP y el corpus por modo permiten comparar gobernanza sin confundir cambios de autonomía.
9. Arquitectura, contrato, evaluación, backlog, diagramas y manifiesto representan el mismo diseño.

El enfoque de gobernanza ya tiene el visto bueno. Este cierre resuelve las decisiones técnicas
necesarias para implementarlo; no exige volver a justificar ese visto bueno.

## Documentos de trabajo

- [Investigación y matriz inicial de protocolos](protocol-research.md).
- [Revisión D1/D2](design-review.md): recomendación técnica de partida.
- [Arquitectura original](architecture.md) y [contrato original](policy-contract.md): por reconciliar.
- [Evaluación](evaluation-plan.md), [amenazas](threat-model.md) y
  [backlog original](implementation-plan.md): por alinear con el diseño v1.
- `evaluation/b2-design.json`: manifiesto original; se actualiza al cerrar las decisiones.
