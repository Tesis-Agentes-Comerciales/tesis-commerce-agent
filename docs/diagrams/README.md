# Diagramas

Cada diagrama tiene una fuente editable (`.drawio`) y dos exportaciones: `.svg` y `.png` de
1920 px de ancho. La convención visual, incluida la de estado (hecho, planificado, futuro), está
en [`docs/b2-governance/visual-style.md`](../b2-governance/visual-style.md). El estado que muestran
los diagramas de gobernanza es el de `org/main` al 20 de octubre de 2026, con el piloto B2 ya
corrido: hecho es lo que está en `main` y usa el flujo; planificado, las piezas que ya están en
`main` pero el flujo no usa (D2-HP, D17 con lookup); futuro, lo que no está implementado.

## Sustentación: qué cambia con la gobernanza

Generados por [`scripts/tools/build_governance_diagrams.py`](../../scripts/tools/build_governance_diagrams.py).

| Diagrama | Qué muestra |
|---|---|
| [`architecture-b0`](architecture-b0.png) | C4 de contenedores de la línea base: UI web (React, assistant-ui), Auth (Better Auth, JWT), API FastAPI con los cinco pasos de la compra (conversación con LLM no confiable, `PurchaseService`, presentación de términos con huella, `ApprovalService`, `CompletionService`), SQLite, OpenAI, comercio (ACP, PostgreSQL) y PSP sandbox. `evaluation/` queda fuera del producto; el monitor de gobernanza retirado ya no aparece. |
| [`architecture-b2-hp`](architecture-b2-hp.png) | Los mismos contenedores con cada control B2 dentro del componente donde actúa: `GET /health` y la traza de la compra (`GET /purchases/{id}/trace`), recuperación gobernada (sin candidato, fuera de la lista corta o presupuesto excedido tras el checkout), gate D1 (hechos canónicos, POL-01/POL-02, transacción con evidencia, reserva y sobre HMAC, un solo POST, verificación post-creación), frescura al presentar y al releer, términos cambiados visibles (`terms_status`), continuación gobernada, frescura antes de firmar y los ledgers `governance_evidence` y `hp_evidence`. Dentro de la interfaz web, la traza en vivo y la tarjeta de términos cambiados. Pendiente: D17 y D2-HP (planificados); POL-01 como autoridad, presupuesto total, salida de estados atascados y evidencia hasta los recibos (futuros). |
| [`b0-vs-b2-delta`](b0-vs-b2-delta.png) | Una sola diapositiva (1920 × 1080): los cinco pasos de B0 arriba y, debajo de cada uno, lo que B2 agrega en ese punto. Abajo, lo verificable (`/health`, ledgers y traza) y lo pendiente, con su estado. |
| [`purchase-sequence-governed`](purchase-sequence-governed.png) | Secuencia Human Present con los puntos de control: condición en `/health`, recuperación gobernada (también tras cancelar por presupuesto), gate D1 antes del checkout (deny sin POST; allow con un solo POST, verificación y `confirmed → awaiting_confirmation`), frescura al presentar, al releer con «Aprobar» (en el chat el 409 `checkout_terms_changed` llega en ese GET) y antes de firmar (en el piloto llegó en el POST de aprobación), continuación gobernada y términos cambiados visibles. El pago y el pedido no cambian. |
| [`architecture-hnp-experiment`](architecture-hnp-experiment.png) | Experimento HNP offline en `evaluation/`: corpus, comprador guionado (LLM en la fase 2, futura), propuesta, validador (guionado en HNP-01, gpt-5-mini en HNP-02), controles deterministas del backend que siempre deciden (B ⊆ A), coordinador y sumidero ficticio en memoria. Brazo A sin validador, brazo B con validador. |
| [`pilot-design`](pilot-design.png) | Diseño experimental: piloto HP pareado B0' vs B2 (S01–S05 × 5 repeticiones, mismo commit, orden de la corrida, `/health` antes de cada repetición, criterio y mecanismos por escenario según el manifiesto) y HNP-02 (19 casos × 5 repeticiones × brazos A/B = 190 episodios). |
| [`pilot-results`](pilot-results.png) | Resultados del piloto B2-PILOT-v1 (1920 × 1080), generados desde [`b0prime-b2-20261020T201055Z.summary.json`](../../evaluation/results/b0prime-b2-20261020T201055Z.summary.json) sin modificarlo: oráculo por escenario B0' frente a B2, qué pasó, criterio pre-registrado (S02 parcial) y totales (16/25 → 25/25, 5xx, aprobaciones obsoletas y latencia de D1). |

Regenerar las fuentes y las exportaciones (requiere el CLI de draw.io; el script lo busca en
`DRAWIO`, en el `PATH` como `drawio` o en `/Applications/draw.io.app`):

```sh
uv run python scripts/tools/build_governance_diagrams.py --export               # los siete
uv run python scripts/tools/build_governance_diagrams.py --export b0-vs-b2-delta  # uno
```

Sin `--export` solo reescribe los `.drawio`. Los cambios se hacen en el script, no en draw.io: una
edición manual se pierde al regenerar.

## Detalle de B0 (vigente para B0')

Generados por [`scripts/tools/build_presentation_diagrams.py`](../../scripts/tools/build_presentation_diagrams.py);
los `.png` y `.svg` se exportan desde draw.io. Describen la línea base, que sigue siendo la
condición B0' del piloto (`AGENT_GOVERNANCE_MODE=off`).

| Diagrama | Qué muestra |
|---|---|
| [`agent-system-context-c4`](agent-system-context-c4.png) | Contexto C4: comprador, agente, comercio, proveedor de pago y modelo. |
| [`agent-components-c4`](agent-components-c4.png) | Componentes de la API por capas: entrada, orquestación, compra, conectores y datos. `lookup_catalog` es la única herramienta invocable por el modelo. |
| [`purchase-sequence`](purchase-sequence.png) | Interacción B0 de punta a punta: aprobación humana, mandatos AP2, delegación del token y confirmación del pago entre comercio y proveedor. También en PDF. |
| [`purchase-sequence-1-discovery`](purchase-sequence-1-discovery.png), [`purchase-sequence-2-payment`](purchase-sequence-2-payment.png) | Las dos mitades de la secuencia anterior, generadas desde el mismo contenido. |
| [`purchase-state-machine`](purchase-state-machine.png) | Ciclo de vida de una compra B0. Un total que excede el presupuesto cancela la sesión; los fallos de preparación o un pago rechazado terminan en `fallido`; `resultado incierto` permite reintento o conciliación. |
| [`purchase-sequence-compact`](purchase-sequence-compact.png) | Resumen B0 en dos fases; material de apoyo, sin generador. |

```sh
uv run python scripts/tools/build_presentation_diagrams.py
```

## Históricos

En [`historico/`](historico/). Se conservan para trazabilidad; no describen el sistema actual.

| Diagrama | Por qué es histórico |
|---|---|
| [`agent-architecture-c4`](historico/agent-architecture-c4.png) | Vista de contenedores B0 anterior, retocada a mano. La reemplaza `architecture-b0`. |
| [`checkout-protocol-sequence`](historico/checkout-protocol-sequence.png) | Secuencia técnica anterior, con otra paleta. La reemplazan `purchase-sequence` y `purchase-sequence-governed`. |
| [`b2-architecture-p1-p5`](historico/b2-architecture-p1-p5.png) | Propuesta B2 original de cinco puntos de decisión con OPA y `Trusted State`, antes de la revisión D1/D2. Estaba en `docs/b2-governance/`. B2 se implementó con un evaluador de referencia en Python y el gate D1. |
