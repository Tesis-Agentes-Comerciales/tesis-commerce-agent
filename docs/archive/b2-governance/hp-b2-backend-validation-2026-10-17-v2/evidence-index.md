# Índice de evidencia y lectura independiente

Este candidato deriva del paquete inicial de 64 artefactos y del consolidado de 124. Se verificó cada manifiesto y la igualdad del conjunto de archivos, no solo el hash del manifiesto. [Proveniencia](evidence/package-provenance.json) y [catálogo de fuentes](evidence/source-catalog.json) fijan sus SHA-256 y los originales de los extractos. Los dos paquetes completos permanecen en custodia local, sin cambios.

| Archivo o conjunto | Uso | Naturaleza |
|---|---|---|
| evidence/cancellation-audit.json | Dos deltas, cluster, test, fases, referencias y ausencia de UUID conservados | Derivado; snapshots seleccionados del observador SQL READ ONLY y referencias al código congelado |
| merchant-retained-144.csv | Clasificación individual de retenidos y línea de función | Derivado por lectura estática; no ejecución de fixtures |
| merchant-inventory-393.csv | Unión de resultados y exclusiones, sin duplicar fases | Inventario previo conservado por contenido; nodeids sanitizados y hash del original |
| scenario-matrix.csv | 24 escenarios y grupo de ejecución compartido | Derivado de la matriz sellada; añade referencia candidata y aclara etiqueta D2 sintética |
| evidence/http-{deny,not-found,corrupt-history}.json | Resultados específicos I05/I06/I09/I10 y trazas/conteos | Evidencia de ejecuciones anteriores, sanitizada en rutas; no pruebas nuevas |
| evidence/prior/agent-*.json | 6 casos TCP HTTP/PG previos | Evidencia anterior, rutas normalizadas |
| evidence/prior/linked-*.json | 2 casos D17 reales, D2 ASGI y recuperación | Evidencia anterior; principal sintético |
| evidence/results.json; prior-results-extract.json | Conteos por fase, warnings, resultados enlazados | Resúmenes previos; no sumar escenarios como tests |
| evidence/effects-extract.json | Contadores por campaña y limpieza previamente verificada | Separar cancelaciones históricas de ceros actuales; sin captura global de host |
| evidence/environment.json | Intérpretes, dependencias, commits, PG, herramientas | Metadatos de la ejecución anterior, no instalación nueva |
| evidence/gates.json | Dictamen y gates previos preservados | Sin promoción a aprobado global |
| evidence/preparation-integrity.json | Revalidación de fuentes, Git y paquetes durante preparación | Auditoría estática nueva; no servicios, tests ni DB |

La normalización reemplaza rutas absolutas del workspace por marcadores y conserva identificadores sintéticos, digests y contadores. Un hash del original no se presenta como hash del JSON normalizado: el catálogo fija el original y hashes.sha256 fija cada byte entregado. No se incluyen claves HMAC privadas; sus huellas y MACs registrados no permiten verificarlos de nuevo sin claves históricas, que no se conservaron para reutilización.

Reproducción de resultados comerciales requiere autorización y entorno nuevo; no está incluida como acción de este paquete. La revisión estática puede verificar datos, hashes, relaciones y código congelado sin ejecutar aplicaciones. El SHA-256 prueba consistencia con las huellas registradas, no identidad de un firmante ni validez semántica automática.

Para revisión independiente: reconciliar 144+249=393; dos filas I05/I06→un caso; tres casos finales→cuatro IDs; D2 sintético; incidencia cancel en un test/dos sesiones; restricciones y gates sin aprobar conservados. Comprobar enlaces a código y los límites explícitos, además del manifiesto. Un escaneo por patrones no sustituye inspección humana del contenido antes de publicar.

## Hashes de nodeids sanitizados

En `merchant-inventory-393.csv` hay exactamente siete identificadores que contienen `[REDACTED]`. Sus valores `nodeid_sha256` son SHA-256 de los identificadores **originales, anteriores a la sanitización**, no de las cadenas visibles en el CSV. Por tanto, no pueden recalcularse desde los siete identificadores sanitizados; que difieran del SHA-256 de la cadena visible no constituye una discrepancia del manifiesto. No se restauran ni se publican los segmentos retirados para hacerlos recalculables. Los hashes originales se mantienen como referencias de procedencia, sin afirmar verificación independiente del preimage a partir de este paquete.

Para verificar el archivo entregado, usar su entrada en `hashes.sha256`: esa huella sí corresponde a los bytes íntegros del CSV sanitizado. Las siete filas pertenecen al inventario de 393; no aparecen en `merchant-retained-144.csv`. No se cambió ningún identificador ni digest de los inventarios en esta revisión.
