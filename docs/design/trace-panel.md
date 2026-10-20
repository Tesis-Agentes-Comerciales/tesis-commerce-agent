# Trazabilidad y gobernanza de la compra

Estado: **propuesta v2, pendiente de aprobación** · 20 de octubre de 2026 · reemplaza el monitor
de gobernanza (`/governance`).

## Problema

El monitor actual reproduce evidencia histórica congelada. No ve las conversaciones del usuario,
usa jerga interna (D1, Q, shadow) y muestra campos vacíos, así que no sirve para explicar la
gobernanza. Queremos dos cosas:

- que quien conversa con el agente vea **en vivo, dentro del chat**, qué controles pasó su compra
  y por qué se bloqueó si se bloqueó;
- que exista una **vista de auditoría** con todas las compras y la traza de cada una.

## Referentes

Investigación del 20 de octubre: LangSmith, LangGraph Studio, Mastra, OpenAI Agents SDK, Langfuse,
Braintrust, assistant-ui, AI SDK Elements, Stripe ACP/Link, AP2, Bedrock Guardrails y Credo AI.
Patrones que adoptamos:

1. **En el chat, una fila de actividad plegable por compra** (assistant-ui *Run activity* y
   *ToolGroup*, el bloque «Worked» de Braintrust). La línea de resumen cambia con el estado; al
   abrirla, aparece la línea de tiempo de los controles.
2. **Las decisiones quedan fuera del plegable.** La tarjeta de aprobación y la de bloqueo se ven
   siempre. Después de decidir, la de aprobación se convierte en recibo (assistant-ui
   *Approval card*).
3. **La decisión de política se registra por regla:** regla, resultado, valor observado, motivo y
   versión de la política (Bedrock, Credo AI, recibos de mandato de AP2).
4. **Estado como máquina finita**, con «esperando a una persona» separado de error (AI SDK, Stripe,
   ACP).
5. **Para auditar, una tabla de corridas** y, al hacer clic, el detalle a dos columnas:
   conversación y línea de tiempo (LangSmith, Braintrust, Mastra *Thread View*).
6. **Base monocroma.** El estado se marca con un icono y una etiqueta, nunca solo con color, y un
   bloqueo por política se ve distinto de un error técnico.

Queda **fuera**, porque en un flujo lineal de cinco controles no aporta y en el chat es ruido:

- grafo del agente, waterfall y flame graph;
- tokens, costo y latencias del modelo;
- JSON crudo.

Si hace falta un diagrama de secuencia, va como figura de la tesis.

## Lenguaje

La UI no muestra códigos internos. El código queda en el detalle técnico para quien lo busque.

| Interno | En la UI |
| --- | --- |
| D1, POL-01, POL-02 | Política de compra: «producto dentro de lo que pediste», «comercio permitido» |
| `shadow` | «Solo observando: registra, no detiene» |
| `enforce` | «Activa: detiene la compra si no cumple» |
| `off` | «Política desactivada» |
| Aprobación B0 | «Tu aprobación» |
| Mandatos AP2 | «Autorización firmada» |
| Pago delegado | «Pago autorizado» |

## Alcance

**v1 (demo del 23 de octubre):**

1. Borrar el monitor y su visor. La evidencia histórica de la tesis se conserva en
   `docs/governance-monitor/sources/`, junto con `provenance.json` y el README.
2. **Chat en vivo:**
   - la fila de actividad de la compra;
   - la línea de tiempo de controles;
   - la tarjeta de bloqueo por política;
   - el recibo de aprobación.
3. **API:**
   - `GET /purchases/{run_id}/trace` para una compra;
   - `GET /me/purchases` para el historial.
4. **Auditoría:**
   - página «Compras» con tabla y filtros;
   - detalle a dos columnas con panel por paso;
   - exportar la traza a JSON.
5. Mensajes claros cuando la política bloquea. Hoy salen como «Ocurrió un error inesperado».

El punto 4 se construye después de 1–3. Si el tiempo no alcanza, la demo sale con 1–3 y la
auditoría entra la semana siguiente.

**Fuera de v1:**

- streaming propio de eventos: se relee la traza cuando termina cada paso;
- diagrama de secuencia;
- Q, D2 y D17 guardado: no están conectados al runtime y no se inventan en la traza.

## Contrato `PurchaseTrace`

Se escribe dos veces, en `ui/src/lib/trace-contract.ts` y en un modelo Pydantic en
`src/commerce_agent/`. Las fixtures de `ui/src/storybook/trace-fixtures/*.json` las validan los dos
lados.

```ts
type TraceActor = "user" | "agent" | "policy" | "merchant" | "payment"
type PurchaseStatus =
  | "in_progress" | "awaiting_approval" | "completed"
  | "blocked" | "canceled" | "failed" | "uncertain"
type StepState = "done" | "running" | "waiting" | "blocked" | "error" | "skipped"

interface PurchaseTrace {
  run_id: string
  status: PurchaseStatus
  policy_mode: "off" | "shadow" | "enforce"
  request: string
  merchant: string | null
  total: { minor: number; currency: string } | null
  started_at: string            // ISO-8601 UTC
  updated_at: string
  steps: TraceStep[]            // en orden cronológico
}

type TraceStep = {
  id: string                    // estable dentro del run
  at: string | null             // null solo si la fuente no tiene hora
  duration_ms: number | null
  actor: TraceActor
  state: StepState
} & (
  | { kind: "request" }
  | { kind: "selection"; product: string; total_minor: number; currency: string }
  | { kind: "policy_decision"; decision: PolicyDecision }
  | { kind: "checkout_created"; checkout_id: string }
  | { kind: "approval"; fingerprint: string; expires_at: string; approved_at: string | null }
  | { kind: "authorization_signed"; mandates: ("checkout" | "payment")[] }
  | { kind: "payment_authorized"; max_minor: number; currency: string }
  | { kind: "order_confirmed"; order_id: string; receipts: ("checkout" | "payment")[] }
  | { kind: "order_event"; event_type: string }
  | { kind: "canceled"; by: "user" | "budget" }
  | { kind: "failed"; code: string }
)

interface PolicyDecision {
  mode: "shadow" | "enforce"
  outcome: "allow" | "deny"
  effect: "observed" | "blocked" | "continued"
  policy: { set_id: string; revision: number }
  rules: {
    id: string                  // "POL-01", "POL-02"
    result: "pass" | "fail" | "not_evaluated"
    observed: string | null     // p. ej. el origen del comercio
    expected: string | null     // p. ej. la lista de comercios permitidos
  }[]
  reason_codes: string[]
  latency_ms: number | null
  decision_id: string
  evidence_id: string
}

interface PurchaseSummary {       // filas de GET /me/purchases
  run_id: string
  started_at: string
  request: string
  merchant: string | null
  total: { minor: number; currency: string } | null
  status: PurchaseStatus
  policy: { outcome: "allow" | "deny"; failed_rules: string[] } | null
  approved_at: string | null
  order_id: string | null
  duration_ms: number | null
}
```

**Reglas del contrato:**

- **Sin datos sensibles:** nunca se devuelven JWT, mandatos, tokens de pago, entradas canónicas,
  fuentes cifradas ni el id del dueño. Solo identificadores, digests, códigos, montos, comercios y
  productos. Un test lo verifica.
- **Textos:** se arman en la UI a partir de los datos, en `ui/src/components/trace/trace-copy.ts`.
- **Validación en la UI:** `parsePurchaseTrace` comprueba la forma exacta (un campo de más también
  falla). Las fixtures JSON no se pueden comprobar con `satisfies`, porque TypeScript ensancha sus
  literales a `string`; la historia `Trace/Contract` valida cada archivo de la carpeta.
- **Forma, igual en los dos lados:** el modelo Pydantic (`src/commerce_agent/trace/models.py`)
  aplica las mismas reglas que `parsePurchaseTrace` y `tests/test_trace_contract.py` valida con él
  cada fixture:
  - textos no vacíos;
  - enteros ≥ 0 en `*_ms`, `minor`, `total_minor`, `max_minor` y `revision`;
  - monedas de tres letras;
  - horas ISO-8601 en UTC terminadas en `Z`; `+00:00` se rechaza;
  - ningún campo de más.

  El backend normaliza todas las horas a UTC con `Z` y milisegundos, incluida `expires_at`, que
  llega del comercio con cualquier desfase. Una hora del comercio sin desfase se lee como UTC.
- **Duración:** `duration_ms` solo va cuando los dos extremos se midieron: la elección (del inicio
  del turno a la elección), la política (`latency_ms`) y la aprobación (de mostrar los términos a
  aprobar). En el resto va `null`. En el historial, la de la compra llega hasta `order_confirmed`
  si se completó, como en `purchaseSummary`, para que un aviso tardío del comercio no la alargue;
  hasta `updated_at` si terminó bloqueada, cancelada o fallida, y es `null` mientras sigue en curso.
- **Comercio:** `merchant` y los valores de POL-02 son orígenes. La decisión guarda un seudónimo
  (`merchant_ref`); el backend lo traduce con los orígenes configurados (`MERCHANT_BASE_URL` y
  `AGENT_D1_ALLOWED_MERCHANT_ORIGINS`). Si no lo reconoce, POL-02 muestra el seudónimo y, sin
  términos de aprobación, `merchant` queda en `null`. Los datos de la compra no guardan un nombre
  legible del comercio, así que no se expone ninguno.
- **Dueño:** se valida con `purchase_for_owner`. Un run de otro usuario o que no existe responde 404.
- **Historial:** `GET /me/purchases?limit=20&before=<run_id>` devuelve
  `{ purchases: PurchaseSummary[], next_cursor: string | null }`. Solo lista las compras propias,
  de la más reciente a la más antigua; `next_cursor` va en `before` para pedir la página siguiente.
  Un `before` que no es del usuario responde 400. Si la evidencia de gobernanza no pasa su
  verificación, los dos endpoints responden 503 en lugar de una traza incompleta.

### Fuentes por paso

| Paso | Fuente | Hora |
| --- | --- | --- |
| `request` | `purchases.request_text` | primer checkpoint del turno en el grafo; si no hay, `purchases.created_at` (se escribe después de elegir) |
| `selection` | checkpoint del grafo escrito al terminar `select_product`: la variante elegida y su precio observado. Sin checkpoint, la línea de `approval_terms` | la de ese checkpoint; sin checkpoint, `null` |
| `policy_decision` | `SqliteGovernanceEvidenceStore.for_run`. POL-01: variante elegida y elegibles. POL-02: origen del comercio y orígenes de la política (`governance_episodes`, verificada contra la decisión) | `decided_at` |
| `checkout_created` | `purchases.checkout_id` | `operation_keys` (`checkout.create`), que se escribe justo antes de llamar al comercio; el modo activo no la guarda ahí y queda en `null` |
| `approval` | `approval_terms` y `approvals` | `approved_at`; si no hay aprobación, `approval_terms.updated_at` |
| `authorization_signed` | `approvals`: solo si existe algún mandato, nunca su contenido | `approved_at` |
| `payment_authorized` | `operation_keys` (`payment.delegate`), solo si la etapa prueba que la delegación terminó (`completing`, `uncertain`, `completed`, `failed`); monto de `approval_terms` | `created_at` |
| `order_confirmed` | `completion_results`: los recibos solo como presencia | `completed_at` |
| `order_event` | `webhook_events` de la orden | `received_at` |
| `canceled` | `operation_keys` (`checkout.cancel.user` o `.budget`) con la etapa `canceled` | `created_at` |
| `failed` | etapa `failed`. El código del error no se guarda: va `purchase_failed`, con actor `merchant` si el comercio rechazó el cierre | `purchases.updated_at` |

**Estado de la compra:**

| Etapa o evento | Estado |
| --- | --- |
| `completed` | `completed` |
| `awaiting_confirmation` | `awaiting_approval` |
| última decisión con `effect = blocked` | `blocked` |
| `canceled`, `failed`, `uncertain` | el mismo |
| cualquier otro | `in_progress` |

## Componentes

Son de presentación: reciben datos del contrato por props, no hacen peticiones y cada uno tiene
historias por estado con `play` y axe. Usan el sistema visual de `docs/workflow.md`.

### Primitivas compartidas (`ui/src/components/trace/`)

| Componente | Responsabilidad | Historias |
| --- | --- | --- |
| `StepStatusIcon` | Icono y etiqueta de texto por `StepState`. Monocromo con acento; `blocked` y `error` se distinguen | Una por estado |
| `PurchaseStatusLabel` | Estado de la compra con los tonos de `chip` | Una por `PurchaseStatus` |
| `ControlStep` | Fila de un control: icono, verbo, detalle corto y duración | Una por `kind` y por `state` |
| `ControlTimeline` | Lista de `ControlStep`, incluidos los pasos no ejecutados | Completa, bloqueada, en curso, política desactivada |
| `PolicyRuleTable` | Reglas con resultado, valor observado y esperado | Todas cumplen, una falla, no evaluada |
| `EvidenceRefs` | Identificadores en mono con botón de copiar | Con y sin ids; `play` para copiar |

La traza solo trae lo que pasó. `ControlTimeline` deduce dos filas que no vienen en ella: la
política desactivada (`policy_mode: off`), entre la elección y el checkout, y, si la compra terminó
bloqueada, cancelada o fallida, los controles que no alcanzó («No se ejecutó»). Una compra por
confirmar no las agrega, porque no se sabe qué pasó. En el chat, `PolicyRuleTable` muestra el valor
observado y el esperado solo de las reglas que fallan; la auditoría los muestra todos.

### Chat en vivo (`ui/src/components/agent/`)

| Componente | Responsabilidad | Historias |
| --- | --- | --- |
| `PurchaseActivity` | Fila plegable por compra («Comprando…», «Esperando tu aprobación», «Compra completada en 21 s», «Bloqueada por la política de compra»); al abrirla muestra `ControlTimeline` | Una por estado; `play` para abrir y cerrar |
| `PolicyBlockedCard` | Tarjeta fuera del plegable: qué regla no se cumplió y qué no ocurrió | Comercio no permitido, política no disponible |
| `PurchaseApprovalCard` (existente) | Al decidir, se convierte en recibo («Aprobado por ti · 10:42 · autorización firmada») | Se añaden las historias del recibo |

El recibo llega a la tarjeta como prop `receipt`: la hora sale de `approval.approved_at` y la firma,
de que exista el paso `authorization_signed`. La tarjeta solo lo muestra con la aprobación ya
registrada. Los identificadores y códigos internos de la fila de actividad y de la tarjeta de
bloqueo quedan en un desplegable «Detalle técnico», cerrado por defecto.

### Auditoría (`ui/src/components/audit/`)

| Componente | Responsabilidad | Historias |
| --- | --- | --- |
| `PurchasesTable` | Tabla de compras con filtros por estado y resultado de la política | Con datos, vacía, cargando, error; `play` para filtrar |
| `PurchaseDetail` | Dos columnas: conversación y `ControlTimeline`, con panel del paso elegido | Completada, bloqueada |
| `StepDetailPanel` | Detalle del paso: actor, hora, `PolicyRuleTable`, versión de la política, `EvidenceRefs` y la cadena de ids autorización → pago → orden | Uno por `kind` |
| `ExportTraceButton` | Descarga la traza en JSON | `play`: genera el archivo |

**Integración:**

- `features/purchase-trace.ts` pide la traza cuando aparece `run_id`, cuando termina cada paso
  del stream y después de aprobar, completar o cancelar.
- `features/audit/` carga la tabla y el detalle.
- La app gana una navegación mínima entre Chat y Compras.

## Criterios de aceptación

1. **Compra en modo observando:**
   - la fila de actividad avanza mientras el agente trabaja;
   - al terminar dice «Compra completada»;
   - al abrirla, la política aparece como permitida, con sus dos reglas.
2. **Compra bloqueada** (modo activo, con una lista de comercios que excluye la tienda):
   - el chat muestra `PolicyBlockedCard`, con la regla del comercio y «no se creó ningún checkout
     ni se hizo ningún cobro»;
   - no aparece «error inesperado».
3. **Política desactivada:** todo funciona y el control figura como desactivado.
4. **Auditoría:** la tabla lista las compras del usuario y el detalle de cada una muestra la misma
   traza que el chat.
5. **API:** no expone campos sensibles, y el run de otro usuario responde 404. Ambos casos tienen
   test.
6. **Storybook:** todos los componentes tienen historias, y `test-storybook` y axe pasan en verde.
7. **Monitor:** no queda rastro en código, CI ni scripts, y la evidencia histórica sigue en `docs/`.

## Plan de PRs

Cada PR depende del anterior y se fusiona con CI en verde y con la aprobación del dueño.

| PR | Tema | Contenido |
| --- | --- | --- |
| A | `chore(ui): remove the governance monitor` | Borra el visor, los scripts, los tests de Node, el exportador y la proyección. Ajusta Playwright, Storybook, CI y docs. No añade funcionalidad |
| B | `feat(ui): purchase trace components` | Contrato TS, fixtures, primitivas y componentes del chat, con historias. Sin cablear |
| C | `feat(api): purchase trace and history endpoints` | TDD del ensamblador y de los dos endpoints. Modelo Pydantic validado contra las fixtures de B. Hora de fin por paso del turno y mensajes para los códigos `d1_*` |
| D | `feat(ui): live purchase activity in the chat` | Cableado del chat, E2E con API simulada y una prueba live en `tests/integration` |
| E | `feat(ui): purchases audit view` | Componentes de auditoría con historias, cableado, navegación y E2E |

## Riesgos y decisiones

- **Permitir en modo activo:** termina en `d1_dispatch_blocked` y deja la compra en `selected`,
  pendiente y sin poder cancelarse. Para la demo, la compra completa va en modo observando y el
  bloqueo en modo activo. Un bloqueo deja la compra en `received`, que no impide compras nuevas.
  Arreglar el despacho en modo activo queda fuera de este alcance.
- **Valores observado y esperado de cada regla:** salen de la entrada de la decisión (origen del
  comercio, candidatos elegibles) y de la política (lista de comercios). Se exponen solo esos
  campos, nunca la entrada canónica completa.
- **Hora de los pasos:** PR C la lee de los checkpoints de LangGraph (`agent.graph.db`), sin tocar
  el flujo de conversación. Depende de los canales `turn_id`, `selected_variant_id` y
  `ranked_candidates` del grafo; si cambian, la elección pierde su hora y sus tests fallan.
- **Reloj del sandbox:** con `AGENT_SANDBOX_CLOCK_AT`, `approved_at` es la hora fija del sandbox y
  la aprobación aparece fuera del orden cronológico.
- **Refresco:** si releer la traza no se siente en vivo, se emite dentro del stream de `/assistant`
  (`state.trace`), que ya tiene patrón.
