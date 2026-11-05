# P3 — Matriz de dependencias del protocolo UX03/UX05

Complemento del [diseño técnico](p3-ux03-protocol.md). Las filas conservan las dependencias
de la auditoría del 3-nov-2026. La evidencia posterior de #160 y #176 se distingue
en el [plan de validación](p3-ux03-validation.md). Los responsables se indican por rol.

| ID | Dependencia / responsable propuesto | Evidencia disponible | Falta / criterio de desbloqueo | Bloquea |
| --- | --- | --- | --- | --- |
| DEP-01 | Checkpoint y pila / mantenedor Agent | P3 deriva de `ad86bdd`. P1 y P2 ya están en `main` | CI documental y regresión `p0-catalog-bridge` sobre #158. Revisión posterior de #160 y #176 en orden | Publicación final revisada |
| DEP-02 | Fuentes reproducibles / responsable de datos | Registro P2 fija Merchant abf30df y PSP e6ab67b, fuentes y par | Revalidar SHAs/locks/árboles y hashes al preparar clones | Cualquier calibración |
| DEP-03 | A/B por atributos / responsable de datos | Reglas P2 y resolución única histórica | Resolver de nuevo por atributos, stock y active; sin empates ni reemplazo oportunista; comprobar B más barata para C | Cinco celdas |
| DEP-04 | Runs equivalentes / arnés Merchant | seed por run, fixture y reloj fijos; independencia administrado/B0 observada en P2 | Comparador completo de Offer/config/reloj y mapping de identidades; prueba de diferencias adversas | Oráculo real |
| DEP-05 | Preparación ACP / arnés de integración | create/update/get existentes; un checkout P2 preparado y cancelado | Dos variantes calibradas por HTTP, totales globales y desglose, repeticiones V iguales; no scorer Agent | T_A/T_B y límites |
| DEP-06 | Limpieza y reconciliación / arnés de integración | P2 comprueba sesión canceled, ofertas iguales, cero órdenes/tokens | Medir todas las sesiones, mutaciones, eventos, intentos de pago, guardas/outbox según esquema; respuesta perdida y cancel fallida | Éxito del protocolo |
| DEP-07 | Separación de estado / arnés de integración | Run/actor en Merchant y DSN desechable guardado; P2 prueba aislamiento parcial | Runs/calibración/Agent SQLite/historia/caché/sesiones/keys independientes; baseline E antes/después; PSP global sin efectos | Medición defendible |
| DEP-08 | Mutaciones por episodio / control Merchant | changed_offer y rewrite_offer/edit_offer/reprice_offer existen; update_offer es administrado | Estímulos stock/envío/revisión en el run correcto, exportación y barreras; pruebas negativas de scope | Controles stock/frescura |
| DEP-09 | Oráculo independiente / responsable experimental + revisor humano | Oráculo P1 sintético y semántica cinco celdas | Esquema de evidencia, sello pre-turno, hashes y fórmula de límites derivados de respuestas Merchant; sin expected accesible al Agent | Medición UX03/UX05 |
| DEP-10 | Propuesta genuina Agent / mantenedor Agent | Contrato tipado, evaluador y registro en #190 | Observar la propuesta en el recorrido HP con ambas alternativas visibles; reportar carencias y mantener la selección a cargo del Agent | Inferencia semántica |
| DEP-11 | UNKNOWN / arnés + mantenedor Agent | FaultProxy existente y distinción unknown en oráculo | Punto real de lectura de A, fallo dirigido contado y alcanzado, sin convertirlo en condición falsa | UX03-UNKNOWN-A |
| DEP-12 | Identidad HP / mantenedor API/auth | Owner, ApprovalTerms, complete protegido; P2 local/TestClient | Control autenticado con comprador equivocado/sin aprobación, sin token ni compra; consentimiento final no heredado | Afirmaciones HP multiusuario |
| DEP-13 | Cotas de efectos / dueño de protocolo | Límites acordados en las secciones 6 y 7 del diseño | Comprobar máximo por etapa y cero en D/UNKNOWN. Cualquier exploración requeriría otro protocolo | Implementación de arnés |
| DEP-14 | Modelo real / dueño de presupuesto | No ejecutado ni autorizado aquí | Corpus sellado, proveedor/modelo/presupuesto/repeticiones aprobados después; pruebas sin modelo no validan interpretación | Evaluación LLM posterior |
| DEP-15 | Compra sandbox / dueño del experimento | Excluida en esta unidad | Nueva autorización y criterios de aprobación/frescura/conciliación | Complete posterior |

Orden mínimo del diseño: acordar DEP-13 → verificar DEP-02/03 → implementar validadores y
controles offline DEP-04/06/07/09 → autorizar validación con servicios DEP-05/08/11/12 →
revisar evidencia y resolver DEP-10 → decidir implementación/evaluación UX03.
DEP-14/15 no son prerrequisitos para validar el protocolo y no se adelantan.
No modificar `ci.yml` ni agregar switches; pruebas reales futuras en `tests/integration/`
mediante `nightly.yml` existente, solo cuando se autoricen.
