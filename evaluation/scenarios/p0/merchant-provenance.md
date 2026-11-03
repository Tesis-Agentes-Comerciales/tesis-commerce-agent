# P2 — procedencia P0 y primera conexión Merchant–Agent

Estado: lectura de fuentes y tests de caracterización implementados; **integración no ejecutada**.
Base Agent: `99f570dfc8d07b7e02b210b7c0c2d4c2d758db1e`, validada en el
[run P1-FIX](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41525881797).
P1 permanece sin merge. Esta rama no implementa condiciones comerciales nuevas.

## Ficha de procedencia

| Fuente | Versión |
| --- | --- |
| Merchant principal | `abf30df3b6a611284dedcc26c00accac6a853329` |
| PSP principal | `e6ab67b5963434a9a3caae2012b70232a3cca65d` |
| Snapshot P0, SHA-256 de bytes Git | `724ab9ad656946924d06d75787cd094047b1b718960d804b0749c4ce45793815` |
| Archivo lateral catalog_snapshot.sha256 | `e82297917dfcfd1879f0e32daf1187569c802d8866cbbf363254fe67e4c5ea4d` |

[merchant-provenance.json](merchant-provenance.json) enumera las fuentes con hashes,
los 141 productos admisibles estáticamente y los dos excluidos. Los datos pertenecen al
proyecto: nombres/fotos del snapshot curado y cantidades, precios y configuraciones
experimentales generados por hash. No son observaciones de precios o inventario del mercado.

El snapshot declara **143 productos**. `generated._snapshot()` exige fotografía descargada
en `assets/products` y descarta `is_vector`. Con los assets del commit auditado quedan
**141 smartphones**. Los excluidos son Q140421681 (iPhone 18 Pro) y Q23309966
(iPhone SE de primera generación), ambos marcados vectoriales.
Esto no significa 141 productos en stock: cada variante tiene cantidad generada y puede
estar agotada. El número de variantes y su disponibilidad efectiva se medirán al exportar.

El hash lateral no coincide con los bytes actuales. El recolector original escribe el
hash de los bytes; no se debe usar el lateral como prueba vigente de integridad.
P2 fija el commit y recalcula los hashes de los archivos reales. No corrige el Merchant ni
atribuye el desajuste a una causa histórica que no esté demostrada.

## Catálogo administrado frente a episodios B0

- `p0.fresh_p0_offers()` llama al generador del snapshot y devuelve copias de Offer.
- `managed_catalog.import_current_catalog()` importa ese conjunto una sola vez,
  como variante merchant, con reloj de importación y vencimiento de un año.
  Los reinicios conservan precios, stock y revisiones persistidos.
- `db.seed(variant="B0")` llama al mismo fixture y crea otro run, actor, reloj fijo y
  catálogo independiente. No es una vista del catálogo administrado.
- `feed.current_feed()` y GET /feeds/current publican el catálogo administrado.
  `feed.export_feed(run_id=...)` exporta el episodio indicado. P2 siempre pasa run_id:
  no depende de cuál sea el episodio más reciente.
- El exportador agrupa por product_id y publica variantes, precio del ítem en COP,
  opciones y disponibilidad. El feed no acredita stock exacto, revisión ni total con envío.
- Las constantes PHONE/OTHER_PHONE de pruebas B0 anteriores eligen Sony Xperia 5 II y
  OnePlus 8T de 256 GB. Son un subconjunto usado por esas pruebas; en este Merchant el
  seed B0 carga el conjunto completo. No generalizar esos dos ejemplos a todo el surtido.
- Compartir offer_id entre runs no acredita compartir autoridad, revisión o estado.

## Primer conjunto ejecutable

Las tres pruebas están en
`tests/integration/test_live_p0_catalog_bridge.py`, sobre el LiveStack y PurchaseLane
existentes. El control Merchant corre en su intérprete/lock; el Agent nunca importa
paquetes Merchant ni consulta su PostgreSQL.

1. **P2-PROV-01:** importación administrada y seed B0 reales; compara contenido inicial
   separando sus relojes/runs; exporta con el exportador Merchant y carga el resultado
   con load_product_feed de producción. Registra hashes, productos, variantes, disponibilidad
   y metadata de ambos ámbitos. El products.jsonl exportado queda como artefacto.
2. **P2-PROV-02:** antes de actuar fija la adversidad: subir una unidad menor el precio y
   poner stock en cero. Usa managed_catalog.update_offer real, verifica revisión +1,
   consulta GET /feeds/current y ejecuta rank_candidates real para verificar el rechazo.
   Comprueba que el episodio B0 permanece intacto. Restaura precio/stock por la misma
   operación, con otra revisión explícita.
3. **P2-PROV-03:** create_app y TestClient de producción preparan un checkout del episodio
   a partir del feed Merchant. La interpretación está simulada con el router existente;
   se declaran la intención y el texto. Se verifican selección, total real con envío,
   un POST de creación, rechazo de completar sin aprobación, cero órdenes/tokens y
   cancelación posterior, con inventario intacto.

A y B se identifican por marca, título, condición nueva, almacenamiento 256 GB y
disponibilidad: Sony Xperia 5 II / OnePlus 8T. Los IDs y montos se leen del episodio;
no se fijan sus identificadores ni precios en el test. La elección conserva la relación
con las pruebas B0 históricas. Si no existe una variante única elegible por regla se
registra `insufficient_fixture_data`. Un fallo de integridad de fuentes es un error,
no una omisión silenciosa.

La tercera prueba es un control positivo de conexión y aprobación HP, con preparación ACP.
No ejercita AP2 ni compara B0/B2. El router simulado recibe una intención representable
de un solo producto: **no demuestra comprensión ni cumplimiento de UX03**.
No devuelve respuestas consultando los oráculos UX03.

## Puerta de seguridad antes de ejecutar

Se exige un checkout limpio de los SHAs auditados. El módulo crea un PostgreSQL nuevo y
un carril propios; no usa bases ni servicios compartidos. El arnés existente verifica
DSN, usuario, nombre único del cluster, directorio y esquema inicialmente vacío; publica
solo puertos efímeros loopback, limpia el entorno y desactiva .env en el control.
Las credenciales sintéticas permanecen en memoria; no se graban tokens ni DSNs.

Preparar un checkout **es un efecto**, no una cotización sin escritura. En P2-PROV-03 se
permite exclusivamente crear/actualizar y cancelar el checkout del episodio desechable;
no hay aprobación, delegación de pago ni finalización. Se comprueban directamente en
Merchant los checkouts, stock/revisiones y ausencia de órdenes, y en PSP cero tokens.
El PostgreSQL se elimina al finalizar. Las lecturas y sesiones pueden producir auditoría:
no se presentan como operaciones sin escrituras.

Esta prueba no prepara cotizaciones de A y B para decidir una rama. Ese protocolo queda
pendiente: antes de UX03 habrá que separar episodios de calibración y evaluación,
obtener totales autoritativos con idéntico destino y demostrar que la calibración no
altera el episodio evaluado. El total Offer sirve aquí para construir un presupuesto
de entrada; solo el checkout real se registra como total observado con envío.
Las cinco celdas UX03 y el adversarial de prioridad permanecen **not_executed** hasta
validar esa conexión y ese protocolo. No se inventa una API de cotización.

## Reproducción y evidencia

Usar el nightly existente, rama `codex/p2-merchant-provenance`, con:
- merchant_ref: `abf30df3b6a611284dedcc26c00accac6a853329`
- payment_ref: `e6ab67b5963434a9a3caae2012b70232a3cca65d`
- test_scope: `p0-catalog-bridge`

Linux/macOS con las dependencias de cada checkout preparadas con sus locks:

```sh
RUN_LIVE_STACK=1 MERCHANT_REPO=../merchant PAYMENT_REPO=../payment \
  uv run pytest tests/integration/test_live_p0_catalog_bridge.py -rs \
  --basetemp=.local/p2-p0 --junitxml=.local/p2-p0-junit.xml
```

El nightly conserva PostgreSQL desechable, acceso privado existente, JUnit y
live-stack-evidence; el scope por defecto sigue ejecutando toda la suite.
No se activa LLM real. Los tres tests se omiten en ci.yml conforme a la política actual:
ese CI puede revisar tipos y colección, pero no validar esta integración.

Los registros P2-PROV en evidence.jsonl enlazan los SHAs del bootstrap, hashes de fuentes
y feed, ámbitos, revisiones, entrada/intención simulada, candidatos/rechazos, resultado
y efectos. Los negativos funcionales fallan: no se usa xfail preventivo ni se adapta
el oráculo. Registros y JUnit deben leerse juntos si una falla ocurre antes de construir
el registro del caso. No se sobrescriben resultados históricos.

Bloqueos observados al preparar esta entrega: el arnés POSIX no soporta este Windows;
no hay entorno Python del worktree preparado. El [nightly inspeccionado](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41479421747)
omitió live-stack porque MERCHANT_REPO_TOKEN no estaba configurado. Esto prueba el estado
de ese run, no afirma conocer la configuración actual de secretos. No se han cambiado
secretos ni permisos. La importación/exportación/checkout reales de P2 siguen pendientes
hasta que live-stack se ejecute, produzca artefactos y se revisen sus resultados.

## Revisión de cierre de casos y acceso P2

P2-PROV-02 registra éxito solo después de restaurar precio/stock, releer el catálogo
persistido y comprobar sus valores y revisión, además de la inmutabilidad del episodio.
P2-PROV-03 registra éxito solo después de cancelar y verificar en Merchant el estado
canceled, inventario intacto, cero órdenes y cero tokens PSP. Un fallo del ejercicio
o de su limpieza genera un resultado failed; no se publica un passed provisional.
Los contadores del primer caso incluyen productos/variantes disponibles y agotados.

La falta de una variante única por regla es insufficient_fixture_data: el registro
queda blocked con failure_kind=data y el test falla, sin atribuir un fallo funcional
al Agent ni considerar un skip como validación. No se cambian datos ni expectativas.

El valor de MERCHANT_REPO_TOKEN solo se entrega a los checkouts Merchant y PSP,
ambos con persist-credentials=false. El preflight recibe un booleano de presencia,
no la credencial. Si falta en workflow_dispatch con scope p0-catalog-bridge, el
preflight termina con error; el nightly programado conserva el aviso/omisión anterior.

El usuario confirmó que configuró el secreto en GitHub. No se solicita ni lee su
valor. Esto actualiza el bloqueo operativo de acceso descrito históricamente arriba:
el acceso efectivo se comprobará al clonar en Actions, no se da por demostrado.
El despacho manual requiere una sesión GitHub autenticada o GitHub CLI:

```sh
gh workflow run nightly.yml \
  --repo Tesis-Agentes-Comerciales/tesis-commerce-agent \
  --ref codex/p2-merchant-provenance \
  -f test_scope=p0-catalog-bridge \
  -f merchant_ref=abf30df3b6a611284dedcc26c00accac6a853329 \
  -f payment_ref=e6ab67b5963434a9a3caae2012b70232a3cca65d
```

Las regresiones offline de orden de evidencia están en
tests/evaluation/test_p2_bridge_evidence.py; no son observaciones comerciales.
La regresión del script de preflight se ejecuta en POSIX y utiliza únicamente
booleanos sintéticos. Un run verde no cierra P2 si faltan alguno de los tres casos,
sus registros definitivos o su limpieza verificada en JUnit/evidence.jsonl.

## Corrección del proxy P2 tras el primer nightly real

El [run histórico 41553912401](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41553912401),
head 656ad3a00ef2cbb15871c79df1f8615afe6dc194, conserva **1 passed y 2 failed**.
Su artefacto live-stack-evidence tiene SHA-256
3b726e048ff0d77c98ab7902a5ad5f17991731d2be587f2f1079e8350c64497b.
No se modifica ni se vuelve a puntuar como una ejecución satisfactoria.

- P2-PROV-01 observó 141 productos y 310 variantes: 285 disponibles y 25 agotadas;
  137 productos tenían alguna variante disponible y 4 estaban totalmente agotados.
- P2-PROV-02 falló con HTTP 405 en GET /feeds/current. La restauración sí quedó
  verificada (revisión 3), pero el caso conserva failed.
- P2-PROV-03 seleccionó y preparó el checkout y bloqueó completar sin aprobación,
  pero falló al cancelar con HTTP 502. No hay cancelación final verificada.

PurchaseLane(acp) usa PURCHASE_ROUTES. Esa política no permite GET /feeds/current
ni POST /checkout_sessions/*/cancel. FaultProxy devuelve 405 antes de reenviar;
ACPClient convierte ese error HTTP y error_response de la API lo presenta como 502.
El log original contiene el 405 del feed y el 502 de cancelación; no contiene una
traza individual del 405 interno de cancelación. La regresión HTTP reproduce la cadena
405 -> ACPClientError -> 502 usando los componentes originales y la política ACP original.

Solo la fixture P2 amplía su instancia con esas dos rutas. PURCHASE_ROUTES, HNP_ROUTES,
FaultProxy y route_allowed permanecen intactos. La política efectiva se añade al registro
P2-source-pins de la nueva ejecución. No se autoriza un wildcard general ni otros métodos.

tests/evaluation/test_p2_proxy_policy.py separa controles del arnés:
la política global continúa bloqueando ambas operaciones, P2 permite sus dos adiciones
y las rutas ajenas siguen devolviendo HTTP 405 sin reenviarse. El upstream simulado
de estas regresiones solo prueba aislamiento/transporte, no seguridad comercial,
cancelación real ni comportamiento funcional del Agent.

Los tres casos vivos deben repetirse con los mismos refs auditados y evidencia nueva.
El éxito de las regresiones o del CI ordinario no sustituye el siguiente nightly.
Si aparece un rechazo del Merchant o un fallo real del Agent tras permitir esas rutas,
debe conservarse como resultado, sin cambiar expectativas ni código comercial.
