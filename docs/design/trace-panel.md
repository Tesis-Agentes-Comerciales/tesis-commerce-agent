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
| D1, POL-01, POL-02, POL-11 | Política de compra: «producto dentro de lo que pediste», «comercio permitido», «precio dentro de tu presupuesto» |
| `shadow` | «Solo observando: registra, no detiene» |
| `enforce` | «Activa: detiene la compra si no cumple» |
| `off` | «Política desactivada» |
| Aprobación B0 | «Tu aprobación» |
| D2, POL-03..06 (al aprobar, `b2-hp-v2`) | «Autorización verificada», «La política no autorizó la compra»; reglas «tu cuenta puede aprobar esta compra», «es la misma compra que revisó la política», «total y condiciones dentro de lo que pediste», «la política no cambió desde que eligió el producto» |
| Mandatos AP2 | «Autorización firmada» |
| Pago delegado | «Pago autorizado» |
| Frescura de la oferta (`freshness_check`) | «Condiciones confirmadas con el comercio», «Las condiciones del comercio cambiaron» |
| Recuperación gobernada (`governed_recovery`) | «El agente explicó por qué no siguió» |
| Compra delegada (HNP): consentimiento, claim, reserva, cierre, D2/D3, revocación, escalamiento | «Delegación autorizada», «Compra delegada en marcha», «Uso único reservado», «Autorización cerrada por el agente», «Autorización delegada verificada», «Despacho autorizado», «Delegación revocada», «Necesito tu decisión» (ver «Compra delegada») |

La frescura y la recuperación son **controles de la aplicación** del flujo HP gobernado
(`docs/design/b2-governed-pilot.md`), no decisiones de la política de compra (D1). La UI nunca los
nombra como política ni como regla.

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
  | "blocked" | "stale_terms" | "canceled" | "failed" | "uncertain"
type StepState = "done" | "running" | "waiting" | "blocked" | "error" | "skipped"
type Money = { minor: number; currency: string }  // unidades menores; la UI lo escribe con formatMoney

interface PurchaseTrace {
  run_id: string
  status: PurchaseStatus
  policy_mode: "off" | "shadow" | "enforce"
  request: string
  merchant: string | null       // origen
  merchant_name: string | null  // nombre legible del comercio; null si no se conoce
  total: Money | null
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
  | { kind: "authorization_decision"; decision: PolicyDecision }  // D2, actor "policy"
  | { kind: "authorization_signed"; mandates: ("checkout" | "payment")[] }
  | { kind: "payment_authorized"; max_minor: number; currency: string }
  | { kind: "order_confirmed"; order_id: string; receipts: ("checkout" | "payment")[] }
  | { kind: "order_event"; event_type: string }
  | { kind: "canceled"; by: "user" | "budget" }
  | { kind: "failed"; code: string }
  | {
      kind: "freshness_check"   // control de la aplicación, no de la política
      stage: "present" | "approve"
      mode: "shadow" | "enforce"
      outcome: "fresh" | "changed" | "unavailable"
      changes: FreshnessChange[]  // solo los campos que cambiaron
      currency: string | null     // moneda de los términos presentados, para los montos
      offer_revision: number | null
      read_error: string | null   // código si no se pudo leer la oferta
      evidence_digest: string     // digest de la fila de hp_evidence
    }
  | {
      kind: "governed_recovery" // control de la aplicación, no de la política
      recovery: "no_candidate" | "selection_outside_shortlist" | "budget_exceeded_after_checkout"
      cause: RecoveryCause
      variant_id: string | null
      budget_minor: number | null
      observed_minor: number | null   // precio del catálogo, no el total del comercio
      currency: string | null
      evidence_digest: string
    }
)

type FreshnessChange =            // valor presentado frente al de la oferta viva
  | { field: "subtotal_minor" | "shipping_minor" | "total_minor" | "delivery_days" | "stock"
      presented: number | null; current: number | null }
  | { field: "currency"; presented: string | null; current: string | null }
  | { field: "expires_at"; presented: string | null; current: string | null }  // UTC con Z

type RecoveryCause =
  | "budget" | "unavailable" | "currency" | "category" | "condition" | "option" | "term"
  | "not_shortlisted" | "not_in_catalog" | "no_match"

interface PolicyDecision {
  mode: "shadow" | "enforce"
  outcome: "allow" | "deny"
  effect: "observed" | "blocked" | "continued"
  policy: { set_id: string; revision: number }
  rules: {
    id: string                  // "POL-01", "POL-02", "POL-11"; D2: "POL-03" a "POL-06"
    result: "pass" | "fail" | "not_evaluated"
    observed: string | Money | null  // p. ej. el origen del comercio; POL-11: precio del catálogo
    expected: string | Money | null  // p. ej. los comercios permitidos; POL-11: presupuesto total
  }[]
  reason_codes: string[]
  latency_ms: number | null
  decision_id: string
  evidence_id: string
}

interface PurchaseSummary {       // filas de GET /me/purchases
  run_id: string
  started_at: string | null       // null solo en trace_unavailable, con la fila de compra mal formada
  request: string | null          // ídem
  merchant: string | null
  merchant_name: string | null
  total: { minor: number; currency: string } | null
  status: PurchaseStatus | "trace_unavailable"  // el segundo, solo en el historial
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
  términos de aprobación, `merchant` queda en `null`.
- **Nombre del comercio:** `merchant_name` es el `seller.name` del feed que el agente lee al
  arrancar, solo para el origen configurado (`MERCHANT_BASE_URL`) y solo si todas las variantes
  declaran el mismo nombre (sin espacios sobrantes, no vacío y de hasta 80 caracteres). Cualquier
  otro origen, o un feed sin un nombre único, deja `null`; la UI no lo reemplaza por el origen. Es el
  nombre actual del comercio, no el que tenía al comprar: las compras no lo guardan. El nombre del
  beneficiario en los mandatos AP2 («Tesis Commerce Merchant») es una constante del agente, no del
  comercio, así que no se usa.
- **Dueño:** se valida con `purchase_for_owner`. Un run de otro usuario o que no existe responde 404.
- **Historial:** `GET /me/purchases?limit=20&before=<run_id>` devuelve
  `{ purchases: PurchaseSummary[], next_cursor: string | null }`. Solo lista las compras propias,
  de la más reciente a la más antigua; `next_cursor` va en `before` para pedir la página siguiente.
  Un `before` que no es del usuario responde 400.
- **Traza no disponible:** la traza no se arma con datos que no se pueden verificar. Pasa cuando:
  - la evidencia no verifica: el registro de decisiones de la política, el artefacto de la política
    frente a la última decisión, o la cadena de `hp_evidence`;
  - un cuerpo de `hp_evidence` no tiene la forma que lee la traza;
  - un dato guardado queda fuera del contrato, como un `expires_at` mal formado en los términos o
    un texto vacío.

  `GET /purchases/{run_id}/trace` responde 503 en lugar de una traza incompleta. El historial no
  cae por una compra: esa fila sale con `status: "trace_unavailable"` y solo con lo que guarda la
  fila de `purchases` (`run_id`, `request`, `order_id` y su hora de creación como `started_at`). El
  resto va en `null`, porque el estado, la política, el comercio, el total, la aprobación y la
  duración salen de la evidencia o de los términos. Si la propia fila de `purchases` trae un valor
  fuera del contrato (un `created_at` que no es ISO, un `request_text` u `order_id` vacíos, algo que
  el store nunca escribe), ese campo también va en `null`: la fila conserva su `run_id`. Ese estado
  solo existe en `PurchaseSummary`, y solo él puede dejar `started_at` o `request` en `null`.

  Un error pasajero también marca la fila como `trace_unavailable`: SQLite ocupado o bloqueado
  (`database is locked`) por otra conexión más allá del tiempo de espera. La traza lo distingue con
  `Retry-After: 1` en el 503, porque la misma lectura puede funcionar enseguida; el historial no lo
  distingue y basta con volver a pedirlo. El log dice `Purchase trace unavailable` con el `run_id` y
  el tipo de error, nunca su texto.
- **Lectura del grafo:** el historial lee una vez los checkpoints de cada conversación y los
  comparte entre sus compras, en lugar de deserializar el hilo entero por fila.

### Fuentes por paso

| Paso | Fuente | Hora |
| --- | --- | --- |
| `request` | `purchases.request_text` | primer checkpoint del turno en el grafo; si no hay, `purchases.created_at` (se escribe después de elegir) |
| `selection` | checkpoint del grafo escrito al terminar `select_product`: la variante elegida y su precio observado. Sin checkpoint, la línea de `approval_terms` | la de ese checkpoint; sin checkpoint, `null` |
| `policy_decision` | `SqliteGovernanceEvidenceStore.for_run`. POL-01: variante elegida y elegibles. POL-02: origen del comercio y orígenes de la política (`governance_episodes`, verificada contra la decisión). POL-11 (`b2-hp-v2`): `selected_unit_minor` y `intent.max_total_minor` de la entrada D1, como `Money` en COP; sin presupuesto total, `expected` es `null`. Las reglas `not_evaluated` de una guarda son las que lista el artefacto de la política | `decided_at` |
| `checkout_created` | `purchases.checkout_id` | `operation_keys` (`checkout.create`), que se escribe justo antes de llamar al comercio. El modo activo no la guarda ahí: usa el `prepared_at` del sobre D1 (`d1_checkout_envelopes`), que es la hora de la decisión que lo permitió, así que empata con la política. Del sobre solo sale esa hora |
| `approval` | `approval_terms` y `approvals`. `state`: `done` si se aprobó, `waiting` si la compra espera la aprobación y `skipped` si no la hubo (cancelada, o la frescura bloqueó los términos) | `approved_at`; si no hay aprobación, `approval_terms.updated_at` |
| `authorization_decision` | Registros D2 de `SqliteGovernanceEvidenceStore.for_run` (`hp-p3-d2-authorization.md`), uno por aprobación evaluada, entre `approval` y `authorization_signed`. `state` `blocked` si la decisión detuvo la aprobación. POL-03 nunca trae valores (es la identidad del dueño). Cada regla trae los valores que explican su resultado: POL-04, la variante aprobada y la que revisó D1 solo si difieren; POL-05, el total autoritativo del checkout y `intent.max_total_minor` como `Money` si se cumplió o si el total lo supera; POL-06, la política de D1 y la vigente (`set · revisión N`) solo si difieren. Una guarda deja POL-03..06 `not_evaluated`. Si la última decisión D2 detuvo la aprobación y no hay aprobación, `approval` queda `skipped` y la compra `blocked` | `decided_at` |
| `authorization_signed` | `approvals`: solo si existe algún mandato, nunca su contenido | `approved_at` |
| `payment_authorized` | `operation_keys` (`payment.delegate`), solo si la etapa prueba que la delegación terminó (`completing`, `uncertain`, `completed`, `failed`) o si existe la clave del comercio (`checkout.complete`) y es posterior: el cierre la crea solo cuando el proveedor devolvió el token. Así un 4xx del comercio a `/complete`, que devuelve la compra a `approved`, conserva el paso, y una delegación fallida no lo muestra aunque después algo escriba la fila (`updated_at`, `failure_code`). Las compras anteriores a esta regla creaban esa clave antes de delegar: en `approved` no muestran el paso, en lugar de un éxito falso. Monto de `approval_terms` | `created_at` |
| `order_confirmed` | `completion_results`: los recibos solo como presencia | `completed_at` |
| `order_event` | `webhook_events` de la orden. Se intercala por hora: un aviso puede llegar antes de que el agente registre la orden que el comercio ya confirmó | `received_at` |
| `canceled` | `operation_keys` (`checkout.cancel.user` o `.budget`) con la etapa `canceled` | `created_at` |
| `failed` | etapa `failed`. `code` sale de `purchases.failure_code`: el código del comercio al rechazar el cierre (`payment_declined`, `out_of_stock`) o el del servicio antes de aprobar (`no_candidate`, `invalid_candidate`, `checkout_mismatch`…). Solo se guarda un código (`[a-z][a-z0-9_]{0,63}`), nunca un mensaje. Una fila anterior a la columna, o un fallo sin código, va como `purchase_failed`. Actor `merchant` si el comercio rechazó el cierre | `purchases.updated_at` |
| `freshness_check` | `hp_evidence` (`freshness_check`), con la cadena de digests y la forma del cuerpo verificadas. `changes` sale de `changed_fields` y `compared`, y `read_error` de `reason`; `state` es `blocked` si la decisión fue `block` y `done` si no. Actor `agent`. Una comprobación igual a la anterior (misma etapa, resultado, cambios y error de lectura) no agrega fila: `GET /me/pending-purchase` vuelve a comprobar cada vez que se recarga | `checked_at` |
| `governed_recovery` | `hp_evidence` (`governed_recovery`), con la cadena y la forma del cuerpo verificadas. Actor `agent`, `state` `done`. Solo la ve una compra que existe: hoy, `budget_exceeded_after_checkout` | `null`: el registro no guarda hora; va al final |

Los pasos de `hp_evidence` y los avisos del comercio se intercalan por hora entre los demás: cada
uno va antes del primer paso posterior, y los pasos sin hora no se mueven. La traza muestra
milisegundos, pero un aviso del comercio y el pago, la orden, la cancelación o el fallo pueden caer
en el mismo: entonces decide la hora guardada, que tiene microsegundos y se toma justo antes de cada
escritura, así que conserva el orden en que se escribieron (no hay una secuencia común a
`webhook_events` y las demás tablas). Si aun así empatan, o en cualquier otro empate, la frescura va
antes del paso que protege y un aviso, después. Si la cadena de `hp_evidence` o la forma de un
cuerpo no se verifica, la traza no está disponible, como con la evidencia de la política. Ninguno
expone el texto del comprador ni la explicación: la evidencia guarda sus digests y la traza ni
siquiera los copia.

**Estado de la compra:**

| Etapa o evento | Estado |
| --- | --- |
| `completed` | `completed` |
| `awaiting_confirmation` (sin bloqueo de frescura) | `awaiting_approval` |
| última decisión con `effect = blocked` | `blocked` |
| `awaiting_confirmation` y la última comprobación de frescura bloqueó | `stale_terms` |
| `canceled`, `failed`, `uncertain` | el mismo |
| cualquier otro | `in_progress` |
| solo en el historial: la traza no está disponible | `trace_unavailable` |

### Compra delegada (HNP-11)

Una compra con `purchases.grant_id` es delegada (`docs/design/hnp-governed.md` §10): la traza y la
fila del historial llevan `purchase_mode: "HNP"`; en HP el campo se omite y sus bytes no cambian. No
hay `request`, `selection`, `approval` ni `authorization_signed`: el comprador consintió antes de
irse y el agente no lee el precio del catálogo en `off`. `request` de la traza es el texto fijo
del ejecutor («Compra delegada»).

| Paso | Fuente | Hora |
| --- | --- | --- |
| `delegation_consented` | evento `consented` de `delegation_events`; `total_max` y el comercio salen de `delegation_grants.scope_json` (SQLite extrae `total_max_minor` y `merchant_refs[0]`, nada más). Actor `user` | `recorded_at` |
| `task_activated` | cada `task_claimed` (uno por claim: el segundo, `task_activated:2`, es una reanudación). `trigger: "service"` | `recorded_at` |
| `policy_decision`, `authorization_decision` | D1 y D2-HNP de `governance_evidence`, como en HP. D2 lista POL-03..09; POL-05 trae los montos y POL-07..09 nunca traen valores | `decided_at` |
| `dispatch_decision` | D3-HNP de `governance_evidence` (POL-09, POL-10, sin valores) | `decided_at` |
| `freshness_check` | `hp_evidence` con `stage: "close"` | `checked_at` |
| `reservation` | `reservation_reserved`, `reservation_released`, `reservation_uncertain` y `consumed`; el monto es el reservado | `recorded_at` |
| `mandates_closed` | evento `mandates_closed`, sin los mandatos | `recorded_at` |
| `payment_authorized` | `receipt_recorded` con `kind: "payment"` y `status: "success"`: el recibo verificado del PSP, que la UoW-3 anota con la orden. Actor `payment`; `max_minor` es el monto reservado del uso único y `currency`, `COP`, la misma forma que en HP. Del cuerpo solo se leen `kind` y `status`: ni el recibo ni su digest llegan a la traza. Un recibo de error del PSP, el de checkout, la etapa (`completing` desde UoW-2) y `payment.delegate` (se anota antes del primer envío al PSP) no son un pago autorizado | `recorded_at` |
| `order_confirmed` | `completion_results`, como en HP: los recibos solo como presencia | `completed_at` |
| `revoked` | `revoked` con `revoked_reason` `owner` (actor `user`) u `operator` (actor `operator`, el kill switch); `effect` del evento. Las revocaciones `escalated` y `failed` las explica su terminal | `recorded_at` |
| `escalated` | `task_finished` con `state: "escalated"`; `reason` es el `outcome_code`. `state: "blocked"` | `recorded_at` |
| `canceled` | `expired`: `by: "expiry"` (nombre nuevo de HNP-11). Y, como en HP, el guardia de presupuesto de `start` (`checkout.cancel.budget`); una escalada o una revocación cancelan la compra sin esa operación, y las explica su terminal | `recorded_at`; el del guardia, la operación |
| `failed` | la compra `failed`, como en HP; y `task_finished` `failed` si su código difiere del de la compra (#93): `failed` si la compra no falló, `failed:task` si sí. Su `budget_guard` ya lo explica la cancelación del guardia | `recorded_at` |

Toda la cadena del grant se verifica (`delegation.models.chain_is_valid`) y cada cuerpo leído se
valida; si no, la traza no está disponible y su fila del historial conserva `purchase_mode`. Una
revocación y la liberación que causa comparten la hora: va primero la revocación. El pago y la
orden también suelen compartir el milisegundo, porque la UoW-3 los escribe en la misma transacción:
va primero el pago, que la UoW-3 anota antes que la orden. Su hora guardada no es
`payment.delegate`, que en HP sí desempata. El total de la traza es el último monto reservado (con
envío).

**Estado de una compra delegada:** `completed` si la compra se completó; `canceled` si la tarea
terminó `revoked_by_owner`, `kill_switch` o `grant_expired`; `stale_terms` si la última frescura
bloqueó; `blocked` si la última decisión D1/D2/D3 lo hizo (salvo un `state_conflict`); `uncertain`
si la tarea quedó incierta o terminó con la compra todavía `selected` (K15: D1 permitió sin su
resultado de despacho; la traza dice «La compra espera la recuperación de la política de compra»,
que la delegación quedó cerrada sin cobro, y el resumen «Por confirmar: recuperación pendiente»);
si no, la etapa de la compra, como en HP. Una revocación con el despacho comprometido deja la compra
`in_progress`.

**Pago de una compra delegada:** `payment_authorized` sale solo con la prueba del token, el recibo de
pago `success` que la UoW-3 anota en `receipt_recorded`. Un rechazo, un timeout o una clave sin
envío no son un pago autorizado: una compra `completing` sin ese recibo no muestra el paso. La
misma UoW-3 escribe `completion_results` (`order_confirmed`) y el `consumed` del ledger (la reserva
`consumed`). Las fixtures completadas (`trace-fixtures/hnp/`) los muestran en el mismo milisegundo:
pago, orden y uso consumido.

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

El comercio se nombra por `merchant_name`, nunca por su origen, en tres lugares: la fila de
actividad («Sony WH-1000XM6 negro · Synthetic Commerce Merchant»), el detalle del checkout creado y
el valor observado de la regla de comercios (POL-02), donde el origen que evaluó la política queda
debajo, en gris y en mono. La lista de comercios permitidos sigue en orígenes, porque es lo que dice
la política. Sin nombre, la fila de actividad muestra solo el producto y el checkout no lleva detalle.

La traza solo trae lo que pasó. `ControlTimeline` deduce dos filas que no vienen en ella: la
política desactivada (`policy_mode: off`), entre la elección y el checkout, y, si la compra terminó
bloqueada, detenida por términos que ya no valen, cancelada o fallida, los controles que no alcanzó
(«No se ejecutó»). Una compra por confirmar no las agrega, porque no se sabe qué pasó. En el chat,
`PolicyRuleTable` muestra el valor observado y el esperado solo de las reglas que fallan; la
auditoría los muestra todos.

La frescura y la recuperación no están en esa lista de controles: solo existen con
`AGENT_HP_FLOW=governed` (la frescura también en `shadow`), así que una fila «No se ejecutó» suya
diría algo falso en `off`. Aparecen solo cuando la traza las trae.

#### Controles de la aplicación: frescura y recuperación

Usan `ControlStep` sin estilos nuevos. Un bloqueo de frescura lleva el escudo ámbar de `blocked`,
igual que la política, pero su título nunca nombra la política; por eso la etiqueta de estado de
`blocked` es «Detenido», sin atribuir. Los códigos (`stage`, `outcome`, `read_error`, la revisión de la
oferta y el digest de la evidencia) van al «Detalle técnico».

| Paso | Título | Detalle |
| --- | --- | --- |
| Frescura `fresh` al presentar | «Condiciones confirmadas con el comercio» | «Antes de mostrarte los términos» |
| Frescura `fresh` al aprobar | «Condiciones confirmadas con el comercio» | «Antes de registrar tu aprobación» |
| Frescura `changed`, bloqueada (al presentar o al aprobar) | «Las condiciones del comercio cambiaron» | «No se registró ninguna aprobación ni se firmó ninguna autorización.» y la lista de cambios |
| Frescura `unavailable`, bloqueada | «No se pudieron confirmar las condiciones con el comercio» | «Por precaución, la compra se detuvo.» |
| Frescura en `shadow` (`changed` o `unavailable`) | el mismo título | «Solo observando: registra, no detiene» y, si cambió, la lista |
| Recuperación | «El agente explicó por qué no siguió» | La causa y lo que no pasó, p. ej. «Superaba tu presupuesto de $ 1.000.000,00 COP. Se canceló el checkout antes de cobrar.» o «La opción elegida no estaba entre las verificadas para tu pedido. No se creó ningún checkout.» |

La lista de cambios usa la forma de las reglas (término sobre valor): por cada campo, «En el
checkout» y «Oferta actual». Campos: «Precio del producto», «Envío», «Total», «Moneda», «Vigencia de
la oferta», «Días de entrega» y «Disponibilidad». Los montos llevan la moneda de los términos.

- **Vigencia de la oferta:** siempre con segundos (`formatPreciseDateTime`). Una oferta renovada
  vence un año después de cambiar, así que las dos vigencias suelen caer en el mismo minuto; si aun
  con segundos se leen iguales, se agregan los milisegundos.
- **Disponibilidad:** el checkout no guarda existencias sino la cantidad pedida, que se compara con
  las unidades que el comercio puede vender. Por eso sus valores dicen «Unidades pedidas» y
  «Unidades disponibles» en vez de «En el checkout» y «Oferta actual».

El detalle de un cambio bloqueado es el mismo en las dos etapas porque la etapa no dice si los
términos ya se mostraron. Al pulsar «Aprobar», el chat relee los términos con
`GET /purchases/{run_id}/approval` antes del POST, y esa lectura corre la frescura en `present`. Así
que en el S04 de la demo la parada es un segundo `present`, después del que confirmó los términos
al mostrarlos, y el POST nunca sale. La etapa `approve` es la del piloto, cuyo runner envía el POST
directo. En los dos caminos es cierto que no hubo aprobación ni firma.

Una compra en `stale_terms` dice en la fila de actividad «Detenida: cambiaron las condiciones» o
«Detenida: condiciones sin confirmar», según la última comprobación; en la etiqueta de estado,
«Detenida: condiciones del comercio». Son cortas para no recortarse en un teléfono de 375 px. La tarjeta de términos que ya no valen
(`StaleTermsCard`) sigue fuera del plegable con su único botón, «Cancelar compra».

#### Compra delegada

`ControlTimeline` usa otro orden de controles para una compra con `purchase_mode: "HNP"`
(delegación, claim, política, checkout, reserva, cierre, pago y orden); el de HP no cambia. D2, D3,
la frescura, la revocación, el escalamiento y la revisión de la elección solo aparecen si la traza
los trae.

La compra delegada no tiene mensaje en el chat: su `PurchaseActivity` va en la tarjeta de la
delegación, bajo «Gobernanza de la compra» y sin el nombre del producto ni del comercio, que la
tarjeta ya muestra (`showNames={false}`). La traza se lee al aparecer el `run_id` de la tarea, cada
vez que la tarea avanza y cada 2 s mientras pueda cambiar, solo con el diálogo abierto
(`ui/src/features/delegation-traces.ts`).

| Paso | Título | Detalle |
| --- | --- | --- |
| `delegation_consented` | «Delegación autorizada» | «Hasta $ 2.000.000,00 COP · vence …» |
| `task_activated` | «Compra delegada en marcha»; el segundo, «Compra delegada reanudada» | «Sin tu intervención» |
| `choice_reviewed` (solo con `AGENT_HNP_REVIEWER=llm`, `hnp-intent-review.md`) | «Revisión de la elección» | «Confirmada: coincide con lo que pediste.» o «No confirmada: » y el motivo; detenida, en la fila de actividad, «Detenida: elección sin confirmar» |
| Frescura `fresh` al cerrar | «Oferta vigente al cerrar» | «Antes de cerrar la compra delegada»; bloqueada: «No se reservó el uso único. No se hizo ningún cobro.» |
| `authorization_decision` (D2-HNP) | «Autorización delegada verificada» o los títulos de D2 | «Antes de reservar el uso único» o, detenida, lo que no pasó |
| `reservation` | «Uso único reservado», «… liberado», «… consumido», «… por confirmar» | El monto |
| `mandates_closed` | «Autorización cerrada por el agente» | «Dentro de lo que autorizaste» |
| `dispatch_decision` (D3) | «Despacho autorizado», «La política no autorizó el despacho», «No se pudo verificar el despacho» | «Antes de enviar el pago» |
| `payment_authorized` | «Pago autorizado», como en HP | «Hasta $ 1.924.000,00 COP»: el monto del uso único |
| `revoked` | «Delegación revocada» | Quién (tú o el operador) y su efecto, con las frases de §8.3 |
| `escalated` | «Necesito tu decisión» | La causa de §8.3 y «No se hizo ningún cobro.» |
| `canceled` por vencimiento | «La delegación venció» | «Venció antes de que el agente comprara. No se hizo ningún cobro.» |
| `failed` de una compra delegada | «La compra no se pudo completar» | La causa solo si es cierta en todos los caminos que dan ese código, y «No se hizo ningún cobro.». Si no, «Por precaución, la compra se detuvo. No se hizo ningún cobro.» |
| `failed:task` | «La compra delegada se detuvo» | Igual que `failed` |
| K15: `failed` `governance_unavailable` con la compra `uncertain` | «La compra espera la recuperación de la política de compra» | «La delegación quedó cerrada. No se hizo ningún cobro.»; en la fila de actividad, «Por confirmar: recuperación pendiente» |

Una causa solo se nombra si es cierta en todos los caminos de `application/hnp_execution.py` que
dan ese código:

- `checkout_failed` también sale con el checkout ya creado: si el comercio rechaza el update, si
  falla el cierre o la Agent Key, o con `d1_post_create_uncertain`. Por eso no culpa al comercio.
- `governance_unavailable` también sale si no se pudo leer la oferta al cerrar. Por eso no culpa a
  la política: la frescura no se presenta como una decisión de la política.
- `policy_denied` sí nombra la política: solo sale de un deny de D2 o D3 por sus reglas.

### Chat en vivo (`ui/src/components/agent/`)

| Componente | Responsabilidad | Historias |
| --- | --- | --- |
| `PurchaseActivity` | Fila plegable por compra («Comprando…», «Esperando tu aprobación», «Compra completada en 21 s», «Bloqueada por la política de compra», «Detenida: cambiaron las condiciones»); al abrirla muestra `ControlTimeline` | Una por estado; `play` para abrir y cerrar |
| `PolicyBlockedCard` | Tarjeta fuera del plegable: qué regla no se cumplió y qué no ocurrió | Comercio no permitido, política no disponible |
| `PurchaseApprovalCard` (existente) | Al decidir, se convierte en recibo («Aprobado por ti · 10:42 · autorización firmada») | Se añaden las historias del recibo |

El recibo llega a la tarjeta como prop `receipt`: la hora sale de `approval.approved_at` y la firma,
de que exista el paso `authorization_signed`. La tarjeta solo lo muestra con la aprobación ya
registrada. Los identificadores y códigos internos de la fila de actividad y de la tarjeta de
bloqueo quedan en un desplegable «Detalle técnico», cerrado por defecto.

#### Mientras carga un turno

El mensaje no muestra nada que vaya a quitar un instante después, y siempre dice qué está haciendo.
Las reglas, con su historia en `chat/Thread` y `agent/*`:

- **Una línea viva.** Mientras el turno corre, al pie del mensaje hay una sola línea con el paso
  actual («Buscando en el catálogo…», «Preparando el checkout…») y el tiempo, también con el grupo
  «Pensando…» abierto. El contador tiene ancho fijo y un decimal por debajo de 10 s («2,0 s», no
  «2 s»), así no salta entre un tic y otro. La cabecera del grupo ya no repite el tiempo.
- **Elegir el producto.** Mientras el agente elige, y mientras prepara el checkout del producto
  elegido, en el lugar del producto va un marcador bajo con rótulo (`ProductChoiceMarker`:
  «Eligiendo entre 3 opciones», o el nombre y el precio del elegido), no un esqueleto anónimo del
  tamaño de la tarjeta grande. La tarjeta grande entra con el checkout listo, o al terminar el turno
  si no hubo checkout.
- **Carrusel.** Su esqueleto sale solo si la búsqueda tarda más de 300 ms; una búsqueda rápida ya
  no lo pinta un cuadro.
- **Bloqueo de la política (D1).** La compra está explicada desde el primer cuadro en que
  `prepare_checkout` falla con `d1_denied`, `d1_unavailable` o `d1_allow_unapplied`, como
  `staleTermsOf` hace con los términos: no espera a la traza. `PolicyBlockedCard` sale con su título
  y deja en esqueleto lo que solo la traza sabe (el resumen, la regla, el detalle técnico y el modo),
  con `aria-busy`. Si la traza no llega, la tarjeta se queda con lo que dice el código: «No pude
  mostrar qué regla no se cumplió.» con `d1_denied`, y el texto de precaución con los otros dos.
  Nunca hay aviso rojo ni «Se detuvo por un error». En el grupo, el paso es «Compra detenida antes
  del checkout», en ámbar, con «No se creó ningún checkout ni se hizo ningún cobro.».
- **El producto de una compra bloqueada** va dentro de la tarjeta de bloqueo (imagen, nombre y
  precio de catálogo); no hay tarjeta grande del producto. El orden final es el plan, «Pensó durante
  N s», la fila de actividad y la tarjeta de bloqueo. El plan queda detenido en «Checkout», en ámbar
  y con «La política la detuvo»: no es un error, así que no va en rojo.
- **Seguimiento al aprobar o cancelar.** El mensaje que solo trae `purchase_update` no muestra plan
  ni grupo «Pensando…»: solo la línea viva y, al terminar, su texto. El plan de la compra sigue en
  su propio mensaje.
- **El plan no se quita a la vista.** Al terminar el turno (compra resuelta, bloqueo o pregunta), el
  plan queda en su último estado («6 de 6», «Cancelaste la compra», «Esperando tu respuesta»). Se
  retira cuando la persona vuelve a escribir, que es cuando el chat se mueve igual. Quitarlo al
  aprobar subía de golpe el recibo unos 120 px. Un turno cortado (error o «Detener») no lo deja: su
  paso quedó a medias. Así quieto, el paso que espera no late: con la página quieta nada se anima.
  Solo late mientras una compra abierta espera la aprobación.
- **El grupo no se abre solo.** Mientras corre, «Pensando…» queda plegado, con la cantidad de pasos
  al lado, y la persona puede abrirlo. Abierto, cada paso que llegaba (el carrusel mide unos 600 px)
  empujaba la línea viva, y al terminar el grupo se plegaba de golpe. El encabezado no repite el
  paso actual: ya lo dicen la línea viva y el plan, y una tercera copia sería otra cosa pegada.
- **La fila de actividad sale con el stream.** Aparece cuando empieza `prepare_checkout`, con el
  `run_id` de sus `args`, ya a su altura final: «Comprando…», «Esperando tu aprobación» o «Bloqueada
  por la política de compra», con el producto. No se abre hasta que llega la traza, que la completa
  en su lugar con el comercio y la línea de tiempo. Si la traza no llega, la fila se va, salvo con un
  bloqueo, que el stream ya da por terminado.
- **Al enviar.** Mientras llega el primer frame, la librería muestra un mensaje provisional. Ese
  mensaje ya lleva el encabezado «Pensando…» (todavía sin nada que abrir) y la línea viva, en el
  mismo lugar que tendrán en el mensaje del servidor. La primera etiqueta de la línea entra sin
  fundido; solo se funden los cambios.

### Auditoría (`ui/src/components/audit/`)

| Componente | Responsabilidad | Historias |
| --- | --- | --- |
| `PurchasesTable` | Tabla de compras con filtros por estado y resultado de la política | Con datos, vacía, cargando, error; `play` para filtrar |
| `PurchaseDetail` | Dos columnas: conversación y `ControlTimeline`, con panel del paso elegido | Completada, bloqueada |
| `StepDetailPanel` | Detalle del paso: actor, hora, `PolicyRuleTable`, versión de la política, `EvidenceRefs` y la cadena de ids autorización → pago → orden | Uno por `kind` |
| `ExportTraceButton` | Descarga la traza en JSON | `play`: genera el archivo |

**Integración:**

- `features/purchase-trace.ts` pide la traza cuando aparece `run_id`, cuando termina cada paso
  del stream y después de aprobar, completar o cancelar. Así quedó construido:
  - **De dónde sale el `run_id`:** del paso que empezó la compra en ese mensaje, cuando ya
    terminó: `prepare_checkout` o, en una compra restaurada, `request_approval`. Un bloqueo de la
    política falla `prepare_checkout` sin `result`, así que el backend pone el `run_id` también en
    sus `args` (`docs/agent-ui-contract.md`). Se espera a que el paso termine para no pedir la traza
    de una compra que aún no existe. Los mensajes de seguimiento (`purchase_update`) no tienen fila
    propia: actualizan la del mensaje que empezó la compra.
  - **Cuándo se relee:** cada vez que cambia el número de pasos terminados, el estado del mensaje,
    la etapa de `state.purchase` de esa compra o la acción local de la tarjeta (aprobar, completar,
    cancelar). Una lectura en curso se aborta si llega otra.
  - **Errores:** un 404, un 503, la red caída o una traza fuera de contrato (`parsePurchaseTrace`, o
    un `run_id` distinto) no rompen el chat. Se conserva la última traza leída; si no hay ninguna, la
    fila no aparece y queda el aviso del mensaje, que el backend ya redacta en español.
  - **En el mensaje:** la fila va debajo del grupo «Pensando…» y antes de las tarjetas. Con la compra
    bloqueada, `PolicyBlockedCard` reemplaza al aviso rojo del mensaje y a la tarjeta del producto,
    y sale del código del stream antes que la traza («Mientras carga un turno»); el grupo
    «Pensando…» tampoco se marca como error. Una compra que sigue «en curso» en un mensaje
    que terminó con error o se detuvo no muestra la fila, porque ese turno ya no la moverá.
    Con términos que ya no valen (S04), aprobar responde 409 y la traza se relee: llega en
    `stale_terms`, así que la fila se ve con la comprobación que bloqueó y qué cambió, y la tarjeta
    de términos va fuera del plegable. Una traza anterior que aún diga `awaiting_approval` no se
    muestra mientras tanto, porque contradiría a la tarjeta.
  - **Recibo:** la tarjeta de aprobación lee la misma traza (una por compra, compartida en la sesión).
  - **Anuncios:** cada cambio de la compra se anuncia una sola vez con `role="status"`. Si el
    mensaje tiene una tarjeta de la compra (aprobación o términos que ya no valen), ella anuncia lo
    que pasa al aprobar o cancelar, y la fila solo anuncia que la compra espera la aprobación: la
    fila ya estaba en el mensaje, y una región que aparece con su texto no siempre se lee. Si no hay
    tarjeta (bloqueo, recuperación), anuncia la fila. Los errores van en su aviso (`role="alert"`).
    La región de estado de la tarjeta queda fuera de la sección con `aria-busy`, para que un lector
    no la calle mientras la compra avanza.
  - **Foco:** el botón que se pulsa conserva el foco mientras trabaja (`aria-disabled`, no
    `disabled`). Cuando desaparece (completada, cancelada, o la tarjeta de términos reemplaza a la
    de aprobación) y se había pulsado con el teclado (`:focus-visible`), el foco pasa al título de
    la tarjeta, y esa vez la región de estado no repite el anuncio; el turno de `purchase-updated`
    no se lo quita. Con el mouse, el foco vuelve al compositor al empezar ese turno, listo para
    escribir. Si el foco ya está en otra parte, no se mueve. Siempre que el foco no llega al título,
    anuncia la región de estado.
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

Los criterios 1–3 tienen E2E con la API simulada (`ui/e2e/chat.spec.ts`), y el 3, además, la
prueba live del navegador (`tests/integration/test_live_browser.py`, con el router guionado). Con
`enforce` y `AGENT_HP_FLOW=governed`, una compra permitida en modo activo también llega a la orden
(criterio 1 en modo activo).

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
| D | `feat(ui): live purchase activity in the chat` | Cableado del chat, E2E con API simulada y una prueba live en `tests/integration`. Además, el `run_id` en los `args` de `prepare_checkout`, el stream real de un bloqueo (`purchase-blocked.sse`) y los seguimientos de la revisión de B |
| E | `feat(ui): purchases audit view` | Componentes de auditoría con historias, cableado, navegación y E2E |

## Riesgos y decisiones

- **Permitir en modo activo:** con `AGENT_HP_FLOW=governed` (despacho D1 y continuación gobernada,
  #36 y #37), una compra permitida en `enforce` crea el checkout, pide la aprobación y llega a la
  orden. Solo sin ese flujo (`AGENT_HP_FLOW=blocked`, el valor por defecto) termina en
  `d1_dispatch_blocked` y queda en `selected`, sin poder cancelarse; el chat lo explica sin «error
  inesperado». Un bloqueo deja la compra en `received`, que no impide compras nuevas.
- **Valores observado y esperado de cada regla:** salen de la entrada de la decisión (origen del
  comercio, candidatos elegibles) y de la política (lista de comercios). Se exponen solo esos
  campos, nunca la entrada canónica completa.
- **Hora de los pasos:** PR C la lee de los checkpoints de LangGraph (`agent.graph.db`), sin tocar
  el flujo de conversación. Depende de los canales `turn_id`, `selected_variant_id` y
  `ranked_candidates` del grafo; si cambian, la elección pierde su hora y sus tests fallan.
- **Reloj del sandbox:** con `AGENT_SANDBOX_CLOCK_AT`, `approved_at` es la hora fija del sandbox.
  La aprobación y la firma conservan esa hora, pero no se usa para ordenar: cuando `approved_at` es
  exactamente la del sandbox, toda comprobación de frescura va antes de ellas y cualquier aviso del
  comercio, después. Límite: una comprobación `present` posterior a la aprobación (releer
  `GET /purchases/{run_id}/approval` después de aprobar) quedaría delante de ella; si repite la
  anterior no agrega fila. La duración de la aprobación queda en `null` si la hora del sandbox es
  anterior a la de los términos.
- **Refresco:** si releer la traza no se siente en vivo, se emite dentro del stream de `/assistant`
  (`state.trace`), que ya tiene patrón. Hoy el `run_id` llega al terminar `prepare_checkout`, así
  que la elección y la política del turno en curso aparecen en la fila de una vez, con el checkout.
- **E2E y fases en curso:** Playwright entrega el stream simulado de una sola vez, así que el E2E no
  observa la fila «Comprando…»; ese estado lo cubren las historias de `PurchaseActivity`.
- **Recuperación sin compra (S02):** cuando el modelo elige fuera de la lista corta o no hay
  candidatos, la recuperación ocurre antes de crear la compra: no hay fila en `purchases` contra la
  cual validar el dueño, y el chat no recibe `run_id`. Esa traza no existe y el chat muestra solo la
  explicación del agente. La traza muestra la recuperación cuando ya hay compra
  (`budget_exceeded_after_checkout`). Mostrar la otra exige atar el run al dueño sin crear la compra;
  queda para después de la demo.
- **Hora de la frescura:** `checked_at` sale del reloj real, mientras que `approved_at` usa el del
  sandbox si `AGENT_SANDBOX_CLOCK_AT` está puesto; por eso el orden no compara esas dos horas (ver
  «Reloj del sandbox»).
