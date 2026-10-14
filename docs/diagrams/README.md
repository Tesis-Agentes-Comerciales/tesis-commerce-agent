# Diagramas para la presentación

1. `agent-architecture-c4.drawio`: vista C4 de contenedores. Incluye al comprador, los
   contenedores del agente, OpenAI, el comercio y el proveedor de pago de prueba.
2. `purchase-sequence.drawio`: interacción de punta a punta; muestra la aprobación humana,
   los mandatos AP2, la delegación del token y la confirmación del pago entre comercio y proveedor.
3. `agent-components-c4.drawio`: vista detallada de componentes de la API organizada por capas. No
   repite al comprador ni los sistemas externos: muestra entrada, orquestación, compra,
   conectores y datos. `lookup_catalog` es la única herramienta invocable por el modelo.
4. `purchase-state-machine.drawio`: ciclo de vida de una compra del agente. `sin compra` es el
   punto previo al episodio; los demás estados se persisten. Un total que excede el presupuesto
   cancela la sesión de compra; los fallos de preparación o un pago rechazado terminan en `fallido`.
   `resultado incierto` permite reintento o conciliación.

Los archivos `.drawio` son editables; los `.png` y `.svg` son exportaciones. La vista de
contenedores muestra protocolos entre sistemas, no cada operación ACP. La secuencia describe
el camino AP2 con comprador presente: las tarjetas previas a la sesión son informativas; el agente
propone una variante o pide aclaración, y el comprador aprueba los términos cerrados después de
ver producto, entrega y total. Tras ese clic, el agente obtiene el token del proveedor y envía
el token y el mandato de compra directamente al comercio. El comercio confirma el token con el
mismo proveedor de prueba, que cumple dos roles de AP2: proveedor de credenciales y procesador
de pagos del comercio. Las dos vistas parciales de la secuencia se generan desde el mismo contenido
que la vista completa. El resumen compacto y la secuencia técnica anterior se conservan como
material de apoyo; no forman parte del conjunto principal.
