# Diagramas

Cada diagrama tiene una fuente editable (`.drawio`) y dos exportaciones: `.svg` y `.png` de
1920 px de ancho. Los de una sola diapositiva (`b0-vs-b2-delta`, `purchase-sequence-slide`,
`policy-map` y `pilot-results`) miden exactamente 1920 × 1080. La convención visual, incluida la de estado (hecho, planificado, futuro), está
en [`docs/b2-governance/visual-style.md`](../b2-governance/visual-style.md). El estado que muestran
los diagramas de gobernanza es el de `org/main` al cierre del 20 de octubre de 2026 (hora de
Bogotá), con el piloto B2 ya corrido y la ola «HP al 100 %» fusionada (POL-11, D2-HP y la cadena de
evidencia hasta los recibos): hecho es lo que está en `main` y usa el flujo; planificado, las piezas
que ya están en `main` pero el flujo no usa (D17 con lookup); futuro, lo que no estaba
implementado. El código HNP se fusionó después, del 21 al 22 de octubre: por eso `policy-map` aún
muestra POL-07 a POL-10 como futuras.

## Sustentación: qué cambia con la gobernanza

Generados por [`scripts/tools/build_governance_diagrams.py`](../../scripts/tools/build_governance_diagrams.py).

| Diagrama | Qué muestra |
|---|---|
| [`architecture-b0`](architecture-b0.png) | C4 de contenedores de la línea base: UI web (React, assistant-ui), Auth (Better Auth, JWT), API FastAPI con los cinco pasos de la compra (conversación con LLM no confiable, `PurchaseService`, presentación de términos con huella, `ApprovalService`, `CompletionService`), SQLite, OpenAI, comercio (ACP, PostgreSQL) y PSP sandbox. `evaluation/` queda fuera del producto; el monitor de gobernanza retirado ya no aparece. |
| [`architecture-b2-hp`](architecture-b2-hp.png) | Los mismos contenedores con cada control B2 dentro del componente donde actúa: `GET /health` y la traza de la compra (`GET /purchases/{id}/trace`), recuperación gobernada (sin candidato, fuera de la lista corta o presupuesto excedido tras el checkout), gate D1 (hechos canónicos, POL-01/POL-02, transacción con evidencia, reserva y sobre HMAC, un solo POST, verificación post-creación), frescura al presentar y al releer, términos cambiados visibles (`terms_status`), continuación gobernada, frescura antes de firmar, D2-HP al aprobar, presupuesto total (POL-11 en D1), evidencia hasta los recibos y los ledgers `governance_evidence` y `hp_evidence`. Dentro de la interfaz web, la traza en vivo y la tarjeta de términos cambiados. Pendiente: D17 (planificado); POL-01 como autoridad y salida de estados atascados (futuros). |
| [`b0-vs-b2-delta`](b0-vs-b2-delta.png) | Una sola diapositiva (1920 × 1080): los cinco pasos de B0 arriba y, debajo de cada uno, lo que B2 agrega en ese punto. Abajo, lo verificable (`/health`, ledgers, traza y `ap2_evidence`) y lo pendiente, con su estado. |
| [`purchase-sequence-governed`](purchase-sequence-governed.png) | Secuencia Human Present con los puntos de control: condición en `/health`, recuperación gobernada (también tras cancelar por presupuesto), gate D1 antes del checkout (deny sin POST; allow con un solo POST, como máximo, verificación y `confirmed → awaiting_confirmation`; un timeout queda `uncertain` hasta D17), frescura al presentar, al releer con «Aprobar» (en el chat el 409 `checkout_terms_changed` llega en ese GET) y antes de firmar (en el piloto llegó en el POST de aprobación), D2-HP antes de firmar con `b2-hp-v2`, continuación gobernada y términos cambiados visibles. El pago y el pedido no cambian; `ap2_evidence` enlaza el consentimiento AP2 con los recibos. |
| [`purchase-sequence-slide`](purchase-sequence-slide.png) | La misma compra en una diapositiva (1920 × 1080), sin detalle técnico: pedido, selección, D1 (un deny se detiene sin POST), checkout (como máximo un POST), términos con frescura, aprobación con frescura al releer y antes de firmar, firma AP2, pago y orden. Debajo, las ramas que detienen la compra: S02 · presupuesto incompatible, con sus dos caminos (elección fuera de la lista corta, explicada sin checkout, que en B0' daba 502; total sobre el presupuesto tras el checkout, que B0 cancela y B2 explica), S04 (409 sin firma) y D2-HP, hecho con la política `b2-hp-v2` (el piloto v1 corrió con `b2-pilot-v1`, sin él). Los números del piloto salen del `.summary.json` y su `.jsonl`: 5xx 4/5 → 0/5 se atribuye a la recuperación en general y 3/5 checkouts cancelados al camino del presupuesto. Aclara que S04 y S02 no son denegaciones del PDP: D1 permitió cada compra que evaluó (conteo de `allow` en `d1_decisions`). |
| [`policy-map`](policy-map.png) | Mapa de políticas (1920 × 1080). Arriba, el PDP: POL-01 y POL-02 en D1 (observado frente a esperado como en la traza, configuración —la lista de comercios de POL-02 sale de `AGENT_D1_ALLOWED_MERCHANT_ORIGINS`—, `decision_record` en `governance_evidence`, prueba live de #48 y modo `pol02-deny`), POL-11 en D1 y POL-03 a POL-06 de D2-HP, las dos con la política `b2-hp-v2` (el piloto v1 corrió con `b2-pilot-v1`, sin ellas), y POL-07 a POL-10 de HNP (futuro al 20 de octubre). Abajo, los controles de aplicación que no son el PDP: frescura de la oferta, recuperación gobernada, presupuesto autoritativo (B0) y continuación gobernada, con su momento, efecto, evidencia en `hp_evidence` o en el diario post-create y su resultado en el piloto. |
| [`architecture-hnp-experiment`](architecture-hnp-experiment.png) | Experimento HNP offline en `evaluation/`: corpus, comprador guionado (LLM en la fase 2, futura), propuesta, validador (guionado en HNP-01, gpt-5-mini en HNP-02), controles deterministas del backend que siempre deciden (B ⊆ A), coordinador y sumidero ficticio en memoria. Brazo A sin validador, brazo B con validador. |
| [`pilot-design`](pilot-design.png) | Diseño experimental: piloto HP pareado B0' vs B2 (S01–S05 × 5 repeticiones, mismo commit, orden de la corrida, `/health` antes de cada repetición, criterio y mecanismos por escenario según el manifiesto) y HNP-02 (19 casos × 5 repeticiones × brazos A/B = 190 episodios). |
| [`pilot-results`](pilot-results.png) | Resultados del piloto B2-PILOT-v1 (1920 × 1080), generados desde [`b0prime-b2-20261020T201055Z.summary.json`](../../evaluation/results/b0prime-b2-20261020T201055Z.summary.json) y su `.jsonl`, sin modificarlos: oráculo por escenario B0' frente a B2 (un cuadro por repetición, en su orden), qué pasó, criterio pre-registrado (S02 parcial, sin regresión frente a B0') y totales (16/25 → 25/25, 5xx, aprobaciones obsoletas y latencia de D1). |

Regenerar las fuentes y las exportaciones (requiere el CLI de draw.io; el script lo busca en
`DRAWIO`, en el `PATH` como `drawio` o en `/Applications/draw.io.app`):

```sh
uv run python scripts/tools/build_governance_diagrams.py --export               # los nueve
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
