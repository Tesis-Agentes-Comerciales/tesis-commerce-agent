# Diagramas

Vistas vigentes del agente base: el contexto y los componentes en C4, la compra de punta a punta y
su ciclo de vida. No dibujan la gobernanza; esa capa está en las [specs de diseño](../design/README.md).
Los diagramas de gobernanza de octubre, hechos antes de la compra delegada, están en el
[archivo](../archive/diagrams/README.md).

Cada diagrama tiene su fuente editable (`.drawio`) y dos exportaciones, `.svg` y `.png`.

| Diagrama | Qué muestra |
|---|---|
| [`agent-system-context-c4`](agent-system-context-c4.png) | Contexto C4: el comprador, el agente, el comercio, el proveedor de pagos y el modelo de lenguaje. |
| [`agent-components-c4`](agent-components-c4.png) | Componentes de la API por capas: entrada, orquestación, compra, conectores y datos. `lookup_catalog` es la única herramienta que el modelo puede invocar. |
| [`purchase-sequence`](purchase-sequence.png) | La compra con el comprador presente, de punta a punta: su aprobación, los mandatos AP2, la delegación del token y la confirmación del pago entre comercio y proveedor. También en [PDF](purchase-sequence.pdf). |
| [`purchase-sequence-1-discovery`](purchase-sequence-1-discovery.png) y [`purchase-sequence-2-payment`](purchase-sequence-2-payment.png) | Las dos mitades de la secuencia anterior: la conversación con la sesión de checkout, y la aprobación con el pago de prueba. |
| [`purchase-sequence-compact`](purchase-sequence-compact.png) | La misma compra resumida en dos fases. Se editó a mano y no tiene generador. |
| [`purchase-state-machine`](purchase-state-machine.png) | El ciclo de vida de una compra: un total sobre el presupuesto cancela la sesión, una preparación fallida o un pago rechazado terminan en `fallido` y un resultado incierto admite reintento o conciliación. |

## Cómo se regeneran

[`scripts/tools/build_presentation_diagrams.py`](../../scripts/tools/build_presentation_diagrams.py)
escribe los `.drawio` de todos menos `purchase-sequence-compact` y sobrescribe los que hay. Los
`.png` y `.svg` se exportan desde draw.io.

```sh
uv run python scripts/tools/build_presentation_diagrams.py
```
