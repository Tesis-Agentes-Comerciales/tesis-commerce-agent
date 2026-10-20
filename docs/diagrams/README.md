# Diagramas

Cada diagrama tiene una fuente editable (`.drawio`) y dos exportaciones: `.svg` y `.png` de
1920 px de ancho. La convención visual, incluida la de estado (hecho, en curso, futuro), está en
[`docs/b2-governance/visual-style.md`](../b2-governance/visual-style.md). El estado que muestran
los diagramas de gobernanza es el de `org/main` al 20 de octubre de 2026.

## Sustentación: qué cambia con la gobernanza

Generados por [`scripts/tools/build_governance_diagrams.py`](../../scripts/tools/build_governance_diagrams.py).

| Diagrama | Qué muestra |
|---|---|
| [`architecture-b0`](architecture-b0.png) | C4 de contenedores de la línea base: UI web (React, assistant-ui), Auth (Better Auth, JWT), API FastAPI con los cinco pasos de la compra (conversación con LLM no confiable, `PurchaseService`, presentación de términos con huella, `ApprovalService`, `CompletionService`), SQLite, OpenAI, comercio (ACP, PostgreSQL) y PSP sandbox. `evaluation/` queda fuera del producto; el monitor de gobernanza retirado ya no aparece. |
| [`architecture-b2-hp`](architecture-b2-hp.png) | Los mismos contenedores con cada control B2 dentro del componente donde actúa: `GET /health`, recuperación gobernada, gate D1 (hechos canónicos, POL-01/POL-02, transacción con evidencia, reserva y sobre HMAC, un solo POST idempotente, verificación post-creación), frescura de la oferta al presentar y al aprobar, continuación gobernada y los ledgers `governance_evidence` y `hp_evidence`. Incluye el trabajo futuro (D17, D2-HP completo, evidencia hasta los recibos) y la leyenda de estado. |
| [`b0-vs-b2-delta`](b0-vs-b2-delta.png) | Una sola diapositiva (1920 × 1080): los cinco pasos de B0 arriba y, debajo de cada uno, lo que B2 agrega en ese punto, con su estado. |
| [`purchase-sequence-governed`](purchase-sequence-governed.png) | Secuencia Human Present con los puntos de control: condición en `/health`, recuperación gobernada, gate D1 antes del checkout (deny sin POST, allow con un solo POST y verificación), frescura antes de firmar (409 `checkout_terms_changed`, cierra S04) y escritura de evidencia. El pago y el pedido no cambian. |
| [`architecture-hnp-experiment`](architecture-hnp-experiment.png) | Experimento HNP offline en `evaluation/`: corpus, comprador guionado (LLM en la fase 2, futura), propuesta, validador (guionado en HNP-01, gpt-5-mini en HNP-02), controles deterministas del backend que siempre deciden (B ⊆ A), coordinador y sumidero ficticio en memoria. Brazo A sin validador, brazo B con validador. |
| [`pilot-design`](pilot-design.png) | Diseño experimental: piloto HP pareado B0' vs B2 (S01–S05 × 5 repeticiones, mismo commit, intercalado, `/health` antes de cada repetición, criterio por escenario, plan B) y HNP-02 (19 casos × 5 repeticiones × brazos A/B = 190 episodios). |

Regenerar las fuentes y las exportaciones (requiere el CLI de draw.io; el script lo busca en
`DRAWIO`, en el `PATH` como `drawio` o en `/Applications/draw.io.app`):

```sh
uv run python scripts/tools/build_governance_diagrams.py --export               # los seis
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
