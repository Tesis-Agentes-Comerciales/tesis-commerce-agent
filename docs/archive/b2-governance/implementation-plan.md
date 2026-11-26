# Plan de implementación B2

> Este backlog corresponde al borrador P1–P5. Primero se ejecuta el
> [plan de cierre de arquitectura y protocolos](design-closure-2026-10-11.md), a partir del
> visto bueno al enfoque de gobernanza recibido el 9 de octubre. Las fases y los paquetes de
> abajo deben reconciliarse con el diseño final antes de iniciar su implementación.

## Orden recomendado

### Fase 0 — congelar contrato y corpus

- aprobar `PolicyInput`, `PolicyDecision`, razón y obligaciones;
- sellar payloads reservados de evaluación;
- registrar commits de agente, comercio y proveedor;
- ejecutar B0 una última vez solo si cambia la instrumentación, no la lógica.

Salida: esquemas 1.0, manifiesto B2 y corpus con checksum.

### Fase 1 — evidencia y puerto, sin enforcement

- crear `PolicyEvaluator`, modelos Pydantic y tabla `policy_decisions`;
- agregar `trace_id`, `decision_id`, digest de bundle y redacción;
- implementar un evaluador de prueba con respuestas deterministas;
- garantizar que fallar al guardar evidencia bloquea cualquier efecto.

Salida: tests de contratos, migración y evidencia append-only.

### Fase 2 — políticas OPA en modo sombra

- crear bundle `commerce-governance/v1` y tests Rego para GOV-001–GOV-010;
- integrar sidecar OPA fijado por versión y readiness;
- ejecutar P1–P5 en `shadow` sin alterar resultados B0;
- reinyectar las 25 trazas B0 y comparar decisión contrafactual/oráculo.

Salida: reporte de desacuerdos, latencia y cobertura por regla.

### Fase 3 — vigencia de oferta y vínculo de aprobación

- crear `OfferFreshnessGuard` y `offer_snapshots` append-only;
- exigir revisión/expiración actuales del merchant;
- invalidar aprobación ante todo cambio material;
- volver a preparar y mostrar términos antes de aceptar otro clic;
- conservar verificación AP2 y CAS/idempotencia existentes.

Salida: S04 y variantes TOCTOU pasan de forma determinista.

### Fase 4 — enforcement y UX de gobernanza

- activar P1–P5 con `GOVERNANCE_MODE=enforce`;
- mapear códigos a mensajes y estados UI;
- mostrar “necesito confirmación”, “no cumple” y “la oferta cambió” sin errores técnicos;
- exponer `decision_id` corto para soporte, no payloads internos.

Salida: piloto B2 de 25 ejecuciones completo.

### Fase 5 — evaluación ampliada

- ejecutar fallos de política, ataques adaptativos, replay e identidad;
- medir seguridad, utilidad, falsos bloqueos, evidencia y latencia;
- abrir el conjunto reservado solo después de congelar bundle v1;
- documentar resultados negativos y riesgo residual.

Salida: informe B0 vs B2 y decisión de iterar, aceptar o revertir políticas.

## Paquetes de trabajo

| ID | Entregable | Dependencia | Criterio de terminado |
|---|---|---|---|
| B2-01 | Esquemas y puerto `PolicyEvaluator` | Ninguna | Validación estricta y tests de salidas inválidas |
| B2-02 | Persistencia de decisiones | B2-01 | Append-only, hashes, redacción y migración probada |
| B2-03 | Bundle Rego GOV-001–010 | B2-01 | Tests positivos, negativos e `undefined` |
| B2-04 | Adaptador OPA + readiness | B2-03 | Timeout/error fallan cerrado; versión registrada |
| B2-05 | Integración P1–P3 | B2-02/04 | Modo sombra no modifica la conducta B0 |
| B2-06 | Freshness y snapshots | B2-02 | Todo cambio material invalida aprobación |
| B2-07 | Integración P4–P5 | B2-04/06 | Sin efecto con evidencia ausente/obsoleta |
| B2-08 | Mensajes y estados UI | B2-05/07 | Razón canónica y recuperación comprensible |
| B2-09 | Runner y métricas B2 | B2-05/07 | Pares B0/B2 y métricas de policy/evidence |
| B2-10 | Evaluación reservada | B2-09 | Corpus sellado, ejecución reproducible, reporte |

## Decisiones que deben quedar explícitas en código

- `off`, `shadow` y `enforce` son valores cerrados; ningún string desconocido habilita compras.
- B2 no está ready si OPA o el bundle fijado no están ready.
- un modo B2 no cae a `off` ante error;
- la política recibe datos, no objetos de red ni clientes con capacidad de efecto;
- la escritura de evidencia ocurre antes de la transición de estado que autoriza el efecto;
- la explicación puede enriquecerse, pero outcome/código/obligaciones no son editables por el modelo.

## Riesgos de ejecución

| Riesgo | Mitigación |
|---|---|
| OPA agrega complejidad/latencia | Puerto aislado, benchmark temprano, p95 separado y bundle local |
| Regla duplica filtros B0 y diverge | La política verifica invariantes; `rank_candidates` sigue siendo la única fuente del conjunto elegible |
| Sobreajuste a cinco escenarios | Payloads reservados y mutaciones adversarias después de congelar v1 |
| “Más logs” filtran datos | Evidencia por allowlist, hashes y tests de secretos |
| Bloqueos opacos degradan UX | Códigos estables, mensajes deterministas y caminos de recuperación |
| Política se trata como verdad ética | Documentar propietario, versión, justificación y riesgo residual de cada regla |

## Definition of Done de B2

- misma configuración experimental de B0 salvo la capa de gobernanza;
- GOV-001–GOV-010 versionadas y probadas;
- P1–P5 persisten decisión completa;
- S01–S05 cumplen el oráculo B2 cinco veces;
- ninguna falla de política abre el flujo;
- S04 y replay de aprobación siempre exigen nuevos términos;
- tests unitarios, integración, tipos y linters en verde;
- reporte B0/B2 reproducible y páginas de proyecto actualizadas.
