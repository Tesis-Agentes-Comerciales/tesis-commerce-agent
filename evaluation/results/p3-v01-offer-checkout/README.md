# P3-V01 — Offer → Checkout: resultado técnico

**Validado dentro del alcance técnico controlado. P3 continúa BLOCKED.**
PR en borrador [#160](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/pull/160),
rama `codex/p3-v01-offer-checkout`, apilado sobre P3 #158 con base
`a11f78bd5e5c18cd3a81ab74c2edc96e274367af`. No se ha hecho merge.

## Ejecución de referencia

[Actions 41750611778](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41750611778)
ejecutó los dos jobs previstos: `sibling-access` y `live-stack`, ambos satisfactorios.

| Fuente | SHA exacto |
|---|---|
| Agent: pruebas ejecutadas | `674ebd66b438fcfc8037657a0056bb008bf8ef0a` |
| Agent: producto base sin cambios | `a11f78bd5e5c18cd3a81ab74c2edc96e274367af` |
| Merchant | `abf30df3b6a611284dedcc26c00accac6a853329` |
| PSP | `e6ab67b5963434a9a3caae2012b70232a3cca65d` |

El árbol Git de `src/commerce_agent` es `96e6d9ba669d2d3dc4bd5efaf5f4c2c1dbdf028e`
tanto en la base como en el commit ejecutado. Los commits posteriores de esta unidad solo
publican documentación y evidencia: no deben confundirse con el SHA ejecutado arriba.
Los manifiestos JSONL incluyen los pines y hashes de ambos `uv.lock` hermanos.

Ubuntu 24.04, Python 3.12 y `uv sync --frozen --all-groups`; se reutilizaron `nightly.yml`,
LiveStack, PurchaseLane ACP, FaultProxy y PostgreSQL desechable. Las lecturas HTTP proceden
de servicios reales ejecutados en CI sobre un catálogo **sintético** del Merchant fijado.
No hubo servicios ni pruebas live locales, modelos, aprobación humana, delegación o pago.

| Estado final de las pruebas | Número |
|---|---:|
| Collected | 24 |
| Passed | 24 |
| Failed | 0 |
| Error | 0 |
| Skipped | 0 |
| Blocked | 0 |
| Not executed | 0 |

Estos estados de ejecución son distintos del resultado comercial. El observador técnico
registró 9 `true`, 7 `false` y 8 `unknown`; no son aciertos conversacionales del Agent.
El detalle íntegro por control está en
[p3-v01-report.json](41750611778/p3-v01-report.json) y
[JUnit original](41750611778/live-junit.xml).

## Correspondencia y controles negativos

En cada control se sembraron dos episodios completos independientes: 310 variantes por clon,
distinto run/actor/credencial, reloj inicial `2026-09-10T14:00:00Z` y contexto
`CO / Bogota / 110111`. Se comprobó igualdad con el fixture completo del Merchant, no una
lista reducida elegida para pasar. La calibración preparó, actualizó, consultó y canceló
su checkout antes de validar. Hay 24 registros `P3-V01-frozen-calibration`, con digest,
emitidos antes del ejercicio de validación. El producto no recibió ese oráculo independiente.

Importes observados por HTTP, en unidades menores COP (exponente 2), no precios comerciales
externos ni valores tomados de `ux03-oracle.json`:

| Control estable | Oferta observada | Artículos | Envío | Total Offer = create = update = get |
|---|---|---:|---:|---:|
| Envío cero | `Q67216142-256GB` | 506000000 | 0 | 506000000 |
| Envío positivo | `BBiPhone11-256GB` | 142000000 | 2500000 | 144500000 |

Los IDs son resultados de selección de fixtures por atributos/stock/envío del catálogo;
no están codificados como ganadores en el Agent. Se comprobaron moneda, cantidad uno,
contexto, revisión, expiración y pricing persistido. La actualización utilizó IDs de
fulfillment y línea devueltos por ese mismo checkout.

- T−1 resulta `false`; T y T+1, `true`. Esta comparación pertenece al observador del
  contrato: no acredita políticas condicionales, comprensión ni aprobación del producto.
- Una revisión distinta con importes iguales conserva la condición comercial `true`, pero
  la correspondencia con la observación anterior se registra `unknown`. ACP ordinario usa
  la revisión actual; no acredita vínculo atómico con una lectura previa.
- Incrementar envío una unidad menor sin aumentar revisión deja el feed reexportado
  idéntico, pero cambia Offer/pricing y el checkout. El cambio de precio también se detecta.
  Son mutaciones adversariales declaradas en V, derivadas de valores observados; no se
  presentan como observaciones originales ni se aplican a C.
- Stock cero e inactividad dan `false` y la creación se rechaza. A un segundo de expirar
  la oferta es válida; en la expiración exacta y un segundo después, la respuesta tipada
  `OFFER_EXPIRED` se correlaciona con auditoría y da `false`.
- Timeout antes de reenviar: cero `offer.read`. Respuesta perdida después de la lectura:
  un `offer.read` con resultado `ok`. Ambos son `unknown` y no crean checkout en V.
- Respuesta malformada, oferta distinta, error de red y autorización ausente/ajena producen
  `unknown`. Un bearer de C lee C aunque los IDs coincidan con V; no prueba autoridad sobre V.
  El Merchant rechaza emitir una sesión para otro actor en V. La consulta de un checkout de
  C con credencial V también se rechaza.
- Se validó idempotencia de creación, conflicto de petición con la misma clave y cancelación
  repetida tras perder la respuesta. La creación incierta se reconcilió mediante el lookup
  existente: run, actor, hash de clave, fingerprint de petición, identidad, hash de respuesta
  original y correspondencia del checkout con Offer. Es recuperación histórica, sin replay
  de compra ni permiso de continuación.

## Efectos e aislamiento

Los eventos, snapshots y trazas sanitizadas están en
[evidence.jsonl](41750611778/live-stack/p3-v010/evidence.jsonl). Incluyen el estado inicial,
calibración congelada, lecturas, estímulos, estado final, efectos globales y hashes de cuerpos
HTTP; no incluyen cabeceras de autorización. La lectura de DB la hace el control del Merchant
en su propio proceso/intérprete y con DSN restringida al cluster desechable; el Agent no
importa Merchant ni consulta su DB.

Estado global al finalizar el último control, **antes** de retirar PostgreSQL:

| Efecto persistente | Observación |
|---|---:|
| Checkouts | 37, todos `canceled`; 24 de calibración y 13 de validación |
| Sesiones de laboratorio | 48, una por cada clon; retenidas hasta destruir el entorno |
| Mutaciones de checkout | 72, retenidas como evidencia |
| Observaciones de solicitudes ACP | 214, retenidas como evidencia |
| Órdenes | 0 |
| Intentos de completar pagos | 0 |
| Creaciones/intentos guarded | 0 |
| Webhook deliveries / attempts | 0 / 0 |
| Delegaciones/tokens PSP | 0; única tabla PSP del pin, hash idéntico al inicial |

Todos los checkouts se descubrieron desde persistencia, incluidos los de respuesta perdida,
se cancelaron y se consultaron después. Se comprobaron sesiones globales para detectar
creaciones fuera de C/V; ninguna apareció. Las tablas ajenas a los efectos explícitamente
permitidos permanecieron iguales. Los snapshots de C permanecieron intactos durante V,
salvo su lectura adversarial explícita en los controles de credencial ajena.

`offer.read` sí escribe auditoría: se verificaron contenido, actor, request ID y resultado.
Sus lecturas no cambiaron otras tablas ni sesiones globales. No se borró auditoría para
simular ausencia de efectos. Las credenciales de laboratorio no se revocaron individualmente;
su entorno quedó retirado. El último registro `shutdown` confirma `proxy_closed=true` y
`postgres_removed=true`. No se publican bases de datos, JWT, claves ni credenciales.

## Intentos conservados

Son iteraciones del arnés, no replicaciones estadísticas ni nuevos experimentos UX03.
No se suman para calcular una tasa de éxito experimental.

| Actions | Commit ejecutado | Passed / failed / error | Diagnóstico |
|---|---|---|---|
| [41749071709](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41749071709) | `92653b2fa50e16184212abc6b154029d9aba0368` | 0 / 0 / 24 | Setup: checkout superficial sin ancestro P3. No se ejecutaron cuerpos comerciales. |
| [41749296705](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41749296705) | `fdaac4fa7cfb2772bb8f9620e609d66094c948a8` | 24 / 0 / 0 | Contrato inicial satisfactorio. |
| [41749841699](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41749841699) | `f558b71b9db3afe1eacda4ef0722f7dfd10bcea4` | 0 / 24 / 0 | Observador: AttributeError por módulo incorrecto para fixture. Limpieza no verificable en ese intento; falló antes de calibrar. |
| [41750188767](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41750188767) | `c05832c7c30c87a401e63bcc6ce2612f8fb3bb05` | 24 / 0 / 0 | Controles globales y congelación temporal satisfactorios. |
| [41750611778](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41750611778) | `674ebd66b438fcfc8037657a0056bb008bf8ef0a` | 24 / 0 / 0 | Referencia final: añade vínculo de fingerprint/petición en reconciliación. |

Todos recogieron 24 pruebas, con cero skipped. Los informes por intento distinguen también
blocked/not_executed y el estado de evidencia; por ejemplo, el primer intento tiene 24
errores de setup y 24 cuerpos sin evidencia ejecutada. Se conservan JUnit, reportes y JSONL
originales sin reescribirlos. El alias `p3-v01current` del artefacto duplica los mismos bytes
de `p3-v010`; se conserva una única copia canónica para evitar contar casos dos veces.

Cada carpeta incluye `p3-v01-SHA256SUMS` emitido en CI, verificado después de la descarga.
`SHA256SUMS` cubre el paquete publicado. `.gitattributes` conserva los bytes de la evidencia.
Los 192 exports completos `products.jsonl` se preservan sin editar mediante deduplicación
por contenido: [feed-manifest.json](feed-manifest.json) asigna cada ruta original a su hash
y a uno de los 10 archivos de [feed-objects](feed-objects). Copiar el objeto a la ruta indicada
reconstruye exactamente el export correspondiente; no se descartan catálogos adversariales.
Los artefactos completos y logs siguen accesibles en los runs anteriores durante su retención
de Actions; los JSONL/JUnit/checksums se preservan además en Git.

## Reproducción y límites

Desde un checkout limpio de esta rama y checkouts hermanos limpios en los pines indicados:

```sh
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest tests/evaluation/test_p3_contract_evidence.py
```

La validación con servicios se reproduce despachando `nightly.yml` con
`test_scope=p3-offer-checkout`, los dos refs exactos de la tabla y la rama del PR. Requiere
la credencial existente de lectura de repositorios hermanos; si falta, falla. No se permite
interpretar la omisión local de live como evidencia suficiente. En Windows, Pyright normal
reporta 25 referencias POSIX preexistentes; con plataforma Linux el análisis pasa. Ruff
format/lint y los controles offline de evidencia pasaron; la ejecución local relevante tuvo
10 passed, 25 skipped (24 live y 1 POSIX) y 2 subtests, sin iniciar servicios.

El contrato queda probado para cantidad uno, COP, un contexto fijo y reloj sintético bajo
estado controlado. No prueba reserva de stock, atomicidad entre Offer y ACP ordinario,
otros destinos/cantidades/monedas, aislamiento ante carga externa concurrente, flujo completo
del Agent, HNP, aprobación, pago ni comprensión conversacional.

El producto actual acepta una respuesta estructuralmente válida con ID de otra oferta.
El observador del test la clasifica `unknown`; **no** se implementó esa protección en el
Agent. La atribución de run/actor depende de credenciales backend, no de los IDs del catálogo.

Antes de implementar UX03, Yair debe aceptar: representación tipada A/B con límites separados;
política `true/false/unknown`; validación de ID/contexto/scope y custodia de observaciones;
refresco o uso del guarded create ya existente para cambios de revisión/digest; equivalencia
de calibración y oráculo independiente congelado. Esos cambios requieren otra unidad y sus
pruebas. Ninguna de estas propuestas constituye aprobación experimental.
