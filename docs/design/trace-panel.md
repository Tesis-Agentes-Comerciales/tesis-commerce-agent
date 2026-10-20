# Panel de trazabilidad de la compra

Estado: **propuesta, pendiente de aprobación** · 20 de octubre de 2026 · reemplaza el monitor de
gobernanza (`/governance`).

## Problema

El monitor actual es un visor de evidencia histórica congelada: no ve las conversaciones del
usuario, muestra jerga interna y campos vacíos, y no sirve para explicar la gobernanza en una
demo. Queremos que quien conversa con el agente vea, **en vivo y junto al chat**, qué hizo el
agente, qué decidió la gobernanza y qué pasó con el comercio y el pago, con la evidencia a un clic.

## Alcance

**Dentro (v1, para la demo del 23 de octubre):**

- Borrar por completo el monitor y su visor. La evidencia histórica de la tesis se conserva en
  `docs/governance-monitor/sources/`, `provenance.json` y el README.
- Endpoint `GET /purchases/{run_id}/trace` que arma la línea de tiempo de una compra con lo que ya
  persiste el agente.
- Panel lateral de traza en el chat, con estos elementos:
  - estado de la compra;
  - flujo de actores (Tú → Agente → Gobernanza → Comercio → Pago);
  - línea de tiempo de eventos;
  - tarjeta de la decisión D1 con sus reglas;
  - pestaña de evidencia con los identificadores.
- Mensajes claros en el chat cuando D1 bloquea (`d1_denied`, `d1_unavailable`,
  `d1_dispatch_blocked`). Hoy salen como «Ocurrió un error inesperado».

**Fuera (después de la demo):**

- Vista de traza completa como diagrama de secuencia.
- Historial de compras en tabla (`GET /me/purchases`).
- Streaming de eventos por run (websocket o SSE propio). En v1 basta con releer la traza cuando
  termina cada paso.
- Q, D2 y D17 guardado: hoy no están cableados al runtime y no se inventan en la traza.

Referencia visual: canvas «Trazabilidad del agente», con dos pantallas: compra completada en
shadow y compra bloqueada en enforce.

## Contrato `PurchaseTrace`

Lo definen `ui/src/lib/trace-contract.ts` y un modelo Pydantic en `src/commerce_agent/`. Las
fixtures de `ui/src/storybook/trace-fixtures/*.json` las validan los dos lados.

```ts
type TraceActor = "user" | "agent" | "governance" | "merchant" | "payment"
type TraceStatus =
  | "in_progress" | "awaiting_approval" | "completed"
  | "blocked" | "canceled" | "failed" | "uncertain"
type EventOutcome = "ok" | "blocked" | "error" | "pending"

interface PurchaseTrace {
  run_id: string
  status: TraceStatus
  governance_mode: "off" | "shadow" | "enforce"
  started_at: string            // ISO-8601 UTC
  updated_at: string
  events: TraceEvent[]          // en orden cronológico
}

type TraceEvent = {
  id: string                    // estable dentro del run, p. ej. "governance:1"
  at: string | null             // null solo si la fuente no tiene hora
  from: TraceActor
  to: TraceActor | null
  outcome: EventOutcome
} & (
  | { kind: "request"; text: string }
  | { kind: "intent"; constraints: string[] }
  | { kind: "search"; matches: number; rejected: number }
  | { kind: "selection"; product: string; total_minor: number; currency: string }
  | { kind: "governance_decision"; decision: GovernanceDecision }
  | { kind: "checkout_created"; checkout_id: string; total_minor: number; currency: string }
  | { kind: "approval_requested"; fingerprint: string; expires_at: string }
  | { kind: "approval_granted" }
  | { kind: "mandates_signed"; mandates: ("checkout" | "payment")[] }
  | { kind: "payment_delegated" }
  | { kind: "order_confirmed"; order_id: string; receipts: ("checkout" | "payment")[] }
  | { kind: "webhook_received"; event_type: string }
  | { kind: "canceled"; by: "user" | "budget" }
  | { kind: "failed"; code: string }
)

interface GovernanceDecision {
  phase: "D1"
  mode: "shadow" | "enforce"
  outcome: "allow" | "deny"
  disposition: "observed_shadow" | "blocked" | "transition_committed"
  rules: { policy_id: string; result: "pass" | "fail" | "not_evaluated" }[]
  reason_codes: string[]
  latency_ms: number | null
  decision_id: string
  evidence_id: string
}
```

**Reglas del contrato:**

- **Sin datos sensibles:** la respuesta nunca incluye JWT, mandatos, tokens de pago, entradas
  canónicas, fuentes cifradas ni el id del dueño. Solo identificadores, digests, códigos y montos.
  Un test lo verifica.
- **Textos:** se arman en la UI desde los datos estructurados; el backend no envía textos de
  presentación.
- **Dueño:** se valida con `purchase_for_owner`. El run de otro usuario responde 404, igual que
  uno que no existe.

### De dónde sale cada evento

| Evento | Fuente | Hora |
| --- | --- | --- |
| `request` | `purchases.request_text` | `purchases.created_at` |
| `intent`, `search`, `selection` | tool-calls del turno guardado (`conversation_turns.response`) | `finished_at` del paso (nuevo, ver PR C) |
| `governance_decision` | `SqliteGovernanceEvidenceStore.for_run` (D1) | `decision.decided_at` |
| `checkout_created` | `operation_keys` (`checkout.create`) y `purchases.checkout_id` | `operation_keys.created_at` |
| `approval_requested` | `approval_terms` | `approval_terms.updated_at` |
| `approval_granted`, `mandates_signed` | `approvals` (presencia de mandatos, nunca su contenido) | `approvals.approved_at` |
| `payment_delegated` | `operation_keys` (`payment.delegate`) | `operation_keys.created_at` |
| `order_confirmed` | `completion_results` y `orders` | `completion_results.completed_at` |
| `webhook_received` | `webhook_events` | `webhook_events.received_at` |
| `canceled`, `failed` | `operation_keys` (`checkout.cancel.*`) y `purchases.stage` | `created_at` y `updated_at` |

**Estado de la compra:**

| `status` | Cuándo |
| --- | --- |
| `completed` | `stage = completed` |
| `awaiting_approval` | `stage = awaiting_confirmation` |
| `blocked` | la última decisión D1 tiene `disposition = blocked` |
| `canceled`, `failed`, `uncertain` | la etapa del mismo nombre |
| `in_progress` | el resto |

## Componentes

Componentes de presentación en `ui/src/components/trace/`. Cada uno recibe datos del contrato por
props, no hace peticiones y tiene historias por estado.

| Componente | Responsabilidad | Historias |
| --- | --- | --- |
| `TraceStatusBadge` | Estado de la compra con los tonos de `chip` | Uno por `TraceStatus` |
| `ActorFlow` | Los cinco actores y hasta dónde llegó la compra | Completada, bloqueada en gobernanza, gobernanza apagada, en curso |
| `TraceEventRow` | Un evento: título, actor, hora relativa y resumen | Uno por `kind` y `outcome` |
| `GovernanceDecisionCard` | Decisión D1: resultado, modo, reglas, efecto e identificadores | Allow shadow, deny enforce, deny shadow (observado), latencia ausente |
| `PolicyRuleList` | Reglas POL-01 y POL-02 con su resultado | Todas cumplen, una falla, no evaluada |
| `EvidenceRefs` | Identificadores en mono con botón copiar | Con y sin ids; `play`: copiar |
| `TraceTimeline` | Lista ordenada de `TraceEventRow` y pasos no ejecutados | Completa, bloqueada, en curso |
| `TracePanel` | Cabecera, `ActorFlow`, pestañas (Línea de tiempo y Evidencia) y `TraceTimeline`; estados de carga, vacío y error | Vacío, cargando, en curso (simulado), esperando aprobación, completada, bloqueada, gobernanza apagada, incierta, error |
| `GovernanceBlockedNotice` | Mensaje del chat cuando D1 bloquea | `d1_denied`, `d1_unavailable`, `d1_dispatch_blocked` |

**Integración (PR D):**

- **Datos:** `features/purchase-trace.ts` pide la traza cuando aparece `run_id`, cuando termina
  cada paso del stream y después de aprobar, completar o cancelar.
- **Diseño:** `App.tsx` añade el botón «Traza» en la cabecera. El panel va a la derecha desde
  1024 px y debajo del chat en pantallas más angostas.

## Criterios de aceptación

1. En una compra en shadow, el panel muestra cada evento persistido menos de un segundo después
   de que termina su paso, y la decisión D1 aparece con POL-01 y POL-02.
2. En enforce, con una allowlist que excluye al comercio:
   - el panel muestra «Bloqueada», POL-02 «No cumple» y «ningún checkout creado»;
   - el chat explica el bloqueo sin «error inesperado».
3. Con `AGENT_GOVERNANCE_MODE=off`, el panel funciona y marca la gobernanza como desactivada.
4. La API no expone campos sensibles, y el run de otro usuario responde 404 (ambos con tests).
5. Cada componente tiene sus historias, con `test-storybook` y axe en verde.
6. No queda rastro del monitor en código, CI ni scripts. La evidencia histórica sigue en `docs/`.

## Plan de PRs

Cada PR depende del anterior y se fusiona con CI en verde y aprobación del dueño.

| PR | Tema | Contenido |
| --- | --- | --- |
| A | `chore(ui): remove the governance monitor` | Borra el visor, sus scripts, los tests de Node, el exportador y la proyección. Ajusta Playwright, Storybook, CI y los docs. Sin funcionalidad nueva |
| B | `feat(ui): purchase trace components` | Contrato TS, fixtures, componentes e historias con `play` y axe. Sin cablear |
| C | `feat(api): purchase trace endpoint` | TDD del ensamblador y del endpoint, modelo Pydantic validado contra las fixtures de B, hora de inicio y fin por paso del turno, y mensajes para los códigos `d1_*` |
| D | `feat(ui): live trace panel in the chat` | Cableado, disposición y E2E con API simulada. Una prueba live del panel en `tests/integration` |

## Riesgos y decisiones

- **Enforce con allow** termina en `d1_dispatch_blocked` y deja la compra en `selected`: queda
  pendiente y no se puede cancelar. Para la demo, la compra completa va en shadow y el bloqueo en
  enforce con una allowlist que excluye al comercio. Un deny deja la compra en `received`, que no
  bloquea compras nuevas. Arreglar el despacho en enforce queda fuera de este alcance.
- **Hora de los pasos:** hoy los pasos del turno no guardan hora. PR C la añade, sin cambiar el
  contrato del stream.
- **La traza de un turno en curso** se completa al releer. Si el refresco no se siente en vivo en
  la demo, la mejora es emitir la traza dentro del stream de `/assistant` (`state.trace`), que ya
  tiene patrón.
