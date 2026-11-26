# HP/B2 backend: evidencia documental

**Dictamen: PARTIAL.** Corte experimental: 17 de octubre de 2026, America/Bogota. Este paquete documenta ejecuciones ya terminadas; su preparación no ejecuta pruebas, no inicia servicios y no abre bases de datos.

La evidencia respalda una demostración funcional acotada de preselección autoritativa → Q shadow → D1 aplicada → creación D17 o recuperación histórica → D2-HP backend con humano sintético. No certifica compra gobernada completa, UI humana real, HNP, pagos ni seguridad general. La conformidad histórica de ejecución sigue sin aprobarse por dos cancelaciones Merchant fuera del perímetro autorizado.

Documentos para revisión:

- [Auditoría técnica e incidencia](audit-report.md).
- [Interpretación de cobertura y gates](coverage-and-limitations.md).
- [144 tests retenidos](retained-tests.md), con [inventario individual](merchant-retained-144.csv).
- [Matriz de 24 escenarios](scenario-matrix.csv) e [inventario Merchant completo](merchant-inventory-393.csv).
- [Índice de evidencia](evidence-index.md) y [manifiesto SHA-256](hashes.sha256).

La matriz cuenta escenarios; no debe sumarse como número de tests. Merchant tiene 393 identificadores únicos: 249 ejecutados históricamente (uno con incidencia de autorización), 144 retenidos. Los tres casos HTTP finales cubren cuatro IDs; I05/I06 son el mismo caso. Los escenarios D2 usan ASGI e identidad sintética, aunque su fuente D17 sí usa HTTP TCP y PostgreSQL reales.

Paquete mínimo por selección explícita: Markdown, CSV y JSON. No contiene SQLite, pgdata, WAL, volcados operativos, claves, tokens de sesión, archivos .env ni ejecutores de operaciones comerciales. Incluye huellas de las evidencias originales y extractos sanitizados para revisión; no es un entorno autónomo de reproducción ni un certificado criptográfico de autenticidad externa. Los artefactos completos permanecen en custodia local.


## Revisión documental v2 — 18 de octubre de 2026

Esta versión aclara dos convenciones, sin cambiar inventarios, matriz, resultados, evidencias ni gates:

- En los siete nodeids de `merchant-inventory-393.csv` que contienen `[REDACTED]`, `nodeid_sha256` corresponde al identificador original, anterior a la sanitización. No puede recalcularse desde el identificador sanitizado entregado. El manifiesto `hashes.sha256` sí permite verificar los bytes del CSV entregado.
- Para las filas D2 (D01–D12), `commercial_posts=0` significa cero POST comerciales **adicionales iniciados por D2** durante su fase. No significa ausencia de los POST D17 anteriores que originaron la evidencia enlazada, ni un total cero de la campaña Agent–Merchant.

Se conserva `AUDIT-MERCHANT-CANCEL-01`, con sus dos ejecuciones y el test clasificado como incidencia de autorización; su gate histórico sigue sin aprobarse. Se conservan `PARTIAL` global e I10/D08/D09 parciales, junto con las limitaciones de ASGI e identidad sintética. No se ejecutaron nuevas pruebas, servicios o conexiones de base de datos, ni se modificaron fuentes, assertions, migraciones, dependencias, ramas o commits; tampoco se realizaron stage, commit, push, PR o escrituras Notion.

Trazabilidad del paquete original, preservado byte a byte:

- Archivo: `publication-candidate-2026-10-17.zip`.
- SHA-256 del ZIP original: `cf19824039c86207fb6c7f7c7a65ba7f7fa1940af9d0cdff63e43fff28cf12f7`.
- SHA-256 del manifiesto original: `672601c78d9a69061e4993059b32be73e6003d13afe164f51eaf70ab46c3b552`.

Los JSON de integridad y resultados incluidos siguen siendo registros de la preparación y ejecución anteriores; no se redatan ni se presentan como una nueva auditoría del workspace. La comprobación de v2 se limita a la integridad de ambos paquetes y a que solo cambiaron README, evidence-index y coverage-and-limitations, además del manifiesto regenerado. El candidato v2 conserva 38 archivos. Esta selección Git contiene 36 archivos: 35 entradas y su manifiesto.

## Procedencia de esta selección Git

Derivada del candidato v2, que permanece intacto en custodia local.

- SHA-256 ZIP v2: `5f8f72ac5370798d1eb1062370c6b7d8b7c63ca257b68036dda3dbc0b42f6956`.
- SHA-256 manifiesto v2: `682a398929ef9a9229c6afd050f56b107c0b47695ab4c700d14707896bf4c1e6`.

Se excluyen el plan local y las propuestas Notion. Los CSV y JSON conservan sus bytes. El README se adapta y se incorpora una regla de preservación de bytes limitada a este directorio. El manifiesto corresponde únicamente a esta selección Git. Los JSON de resultados e integridad siguen siendo registros históricos, no una nueva ejecución ni un cierre de gates.

## Corrección/actualización 2026-10-19

La [adenda del 19 de octubre](adenda-2026-10-19.md) hace tres correcciones:

- I05/I06 se apoyó en una política deny inyectada en tests. En el runtime de esa fecha enforce no podía activarse, así que no se verificó D1 enforce en el flujo real.
- La variante local de I10 no tenía test en el repositorio hasta el PR #15.
- La evidencia live se ejecutó una vez en Windows, con un worker de inyección de fallos que no está en ningún repositorio. No es reproducible desde `main`.

La adenda también registra los cambios posteriores de los PR #12–#15 del Agent y del PR #3 del Merchant, y el cierre con mitigación de `AUDIT-MERCHANT-CANCEL-01`. No cambian los archivos sellados, los inventarios, la matriz, los resultados ni los gates. El dictamen sigue **PARTIAL**. En esta revisión solo cambian este README y `hashes.sha256`, que se regenera y añade la adenda. La selección Git pasa a 37 archivos: 36 entradas y su manifiesto.
