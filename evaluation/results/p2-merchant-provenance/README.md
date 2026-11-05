# Cierre P2: procedencia Merchant y conexión determinista

Estado: **passed en el alcance P2-PROV-01/02/03**, verificado desde los artefactos.
No acredita UX03, interpretación LLM, dos agentes LLM, AP2 ni compra completada.
Fechas de Actions en UTC (ambas ejecuciones ocurrieron el 2 de noviembre en Bogotá).

## Cronología y procedencia

| Run | Agent SHA | Resultado JUnit y JSONL |
| --- | --- | --- |
| [41553912401](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41553912401) | `656ad3a00ef2cbb15871c79df1f8615afe6dc194` | 1 passed, 2 failed, 0 skipped |
| [41563374624](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41563374624) | `99252720a9418b52ad8189c280d8c49dc3d31d03` | 3 passed, 0 failed, 0 skipped |

En ambos: Merchant `abf30df3b6a611284dedcc26c00accac6a853329`;
PSP `e6ab67b5963434a9a3caae2012b70232a3cca65d`.
PostgreSQL 18.4 desechable, imagen fijada por digest en cada evidencia; Python 3.12.14.
El bootstrap registra checkouts limpios de Agent/Merchant; P2-source-pins fija ambos hermanos,
y la fixture exige su SHA y árbol limpio antes de iniciar.

La política global PURCHASE_ROUTES no autorizaba GET /feeds/current ni
POST /checkout_sessions/*/cancel. FaultProxy devolvía 405 antes de reenviar.
El primer fallo registra directamente ese 405; en cancelación el log registra 502.
El código ACPClient/error_response y las regresiones HTTP del fix reproducen 405 → error ACP → 502.
El artefacto histórico no incluye la llamada interna individual de cancelación; se distingue
esa reconstrucción de una captura HTTP original. Solo la instancia de la fixture P2
añade ambas rutas; las políticas globales y el bloqueo de rutas ajenas no cambian.
La segunda ejecución registra la lista efectiva en P2-source-pins.

## Resultados definitivos y fallos conservados

| Caso | Primer run | Segundo run y alcance observado |
| --- | --- | --- |
| P2-PROV-01 | passed | passed: importación administrada y episodio independiente; exportación y lectura por Agent |
| P2-PROV-02 | failed HTTP 405; restauración sí comprobada | passed: revisión 1 → 2 → 3; precio y stock mutados, producto rechazado como unavailable, restauración persistida |
| P2-PROV-03 | failed HTTP 502; cancelación no verificada | passed: selección/preparación reales, /complete sin aprobación devuelve 409, cancelación persistida y limpieza comprobada |

Ambas ejecuciones observan 143 productos fuente, 141 exportables y 310 variantes:
285 disponibles y 25 agotadas; 137 productos con disponibilidad y 4 sin ella.
Recuento independiente del feed retenido: 268 in_stock + 17 limited_stock + 25 out_of_stock.
No confundir estos conteos con 310 pruebas de compra. P2-PROV-02 muta una variante Sony
y P2-PROV-03 prepara solo esa variante. El par se obtiene por marca, título, almacenamiento,
condición y disponibilidad fijados previamente en merchant-provenance.json.

P2-PROV-02: precio de artículos 131000000 → 131000001 → 131000000 minor COP;
stock 11 → 0 → 11. El episodio independiente permanece intacto.
P2-PROV-03: Sony Xperia 5 II 256 GB; total autoritativo del checkout 133500000 minor COP,
un POST de creación, un checkout cancelado, 0 órdenes, 0 tokens de pago antes/después
y ofertas idénticas al estado inicial. El presupuesto se deriva de la oferta del laboratorio;
el total se verifica después contra el checkout, no se llama cotización sin efectos.

El router es StaticConversationRouter: entrega una intención programada al producto.
El selector determinista, API del Agent, Merchant/PostgreSQL y PSP arrancado son reales;
no se infiere comprensión del texto ni pago ejecutado de esos hechos.
P2-PROV-02 prueba directamente rank_candidates; P2-PROV-03 usa TestClient para el Agent
y HTTP hacia Merchant/PSP. El lector de control independiente del arnés consulta estados
del laboratorio; el producto Agent no accede a la DB de Merchant.

## Conservación e integridad

Las carpetas [41553912401](41553912401/) y [41563374624](41563374624/) conservan JUnit,
JSONL canónico, feeds y logs de servicio publicados. MANIFEST.json registra origen,
SHAs, tamaño, hash de cada miembro original y retenido, selección y sanitización.
SHA256SUMS fija también el manifiesto; .gitattributes impide conversiones de fin de línea.
Se excluyen los alias duplicados current y no se versiona el ZIP completo ni bases de datos.
Las copias de texto retenidas son byte a byte: el escaneo de PEM, bearer, JWT, claves sk-
y URLs PostgreSQL con contraseña no encontró coincidencias. No se leyó ningún .env.
Este escaneo no sustituye la revisión humana; los identificadores son de episodios sandbox.

Hashes SHA-256 de los ZIP originales, comprobados al descargar:
- 41553912401: `3b726e048ff0d77c98ab7902a5ad5f17991731d2be587f2f1079e8350c64497b`.
- 41563374624: `7cc4dc4049209fe06a8deac12ff607405298b5f9fc7134001143f44be9ec163f`.

Los feeds mutados y previos difieren legítimamente por revisión/tiempo.
El hash de la fixture común es `dabce996e2ee6431d377a25614bfe9df849d2675f33a5d28bc61f3aec7579e64`;
las fuentes y hashes de feeds concretos están en evidence.jsonl. No se reemplaza la historia.

Verificación portable desde cada carpeta de run: `sha256sum -c SHA256SUMS`.
Los resultados JUnit se contrastaron con los tres registros P2 y con sus indicadores cleanup_verified.
La publicación/retención no ejecutó compras, servicios ni modelos.

## CI y límites

[CI del fix 41556976012](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41556976012):
python-lint, python-tests, auth y ui passed; 3444 passed, 95 skipped; seis regresiones del proxy passed.
El CI offline no ejecutó la integración. El nightly posterior aporta evidencia separada,
con los mismos SHAs de Merchant/PSP. Los fallos históricos siguen siendo failed.
La copia no contiene snapshots SQL de todas las tablas; la verificación persistida se sustenta
en las aserciones ejecutadas y las mediciones del control del arnés, no permite reconsultar esa DB
ya eliminada. No demuestra seguridad universal, pagos reales, concurrencia general ni UX03.

Se conserva también [el artefacto CI del fix](ci-41556976012/), byte a byte, con su digest ZIP,
SHA256SUMS y distinción entre SHA de PR y merge temporal. Los logs de servicio P2 publicados
están vacíos; no aportan una traza HTTP adicional. Las mediciones persistidas proceden de los
registros de control y de las aserciones del test, no de mensajes inexistentes en esos logs.
