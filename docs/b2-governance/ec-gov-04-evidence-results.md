# EC-GOV-04 — Ledger D1 de evidencia

Base: `aef131cf6ace32a621d20ad5e5555bb7b19539ee`, rama
`governance/ec-gov-04-decision-evidence`, inicialmente limpia. Sin commit/push/merge.
Dictamen final: **READY_FOR_REVIEW**, limitado al ledger D1 aislado.

## Arquitectura y contratos

Se añaden modelos estrictos/inmutables para las tres variantes de `DecisionEvidence`
del contrato v1: `decision_record`, `input_rejected` y `application_failure`.
El registro completo conserva la entrada canónica UTF-8, la decisión, referencias,
aplicación y replayability. Run/trace/owner se extraen de la entrada para no inventar
campos de wire ajenos al contrato. Rechazos y fallos operativos no conservan raw input
ni inventan una decisión/digest evaluable.

`EvidenceScope` es un registro interno de contexto resuelto por backend, separado
del input del motor: run/trace/owner, fase/modos, versión de contexto, política y refs
autorizadas del episodio. No autentica propietarios ni demuestra por sí solo la
procedencia de un ID opaco. Su resolución contra fuentes operativas sigue pendiente.
No hay endpoint para aceptar evidencia ni texto arbitrario del cliente.

`GovernanceEvidenceStore` usa una conexión SQLite proporcionada por el llamador.
`SqliteGovernanceEvidenceStore` no abre conexiones, no confirma ni revierte la
transacción, no altera estado comercial y no ejecuta su inicialización al importar.
El llamador habilita FK antes de empezar y controla BEGIN/COMMIT/ROLLBACK.

Se eligió un adaptador separado frente a ampliar la inicialización automática de
`SqliteAgentStore`: permite probar el ledger en la misma base sin modificar ese store
ni crear tablas de gobernanza en bases comerciales por un arranque B0.
El inicializador nuevo usa DDL transaccional aditivo, evita `executescript` y rechaza
versiones/esquemas incompatibles; no migra bases operativas existentes.

## Tablas y relaciones

| Tabla | Contenido / integridad |
| --- | --- |
| `governance_schema` | Marcador singleton de versión 1; sin migración silenciosa |
| `governance_episodes` | PK run_id; contexto estricto y snapshots no sensibles de artefactos Python/política |
| `governance_evidence` | PK evidence_id; FK run_id al contexto; UNIQUE(run_id, sequence); decision_id único para decision_record; cuerpo canónico y digest SHA-256 del cuerpo |

La evidencia tiene proyecciones SQL de fase, modo, versión de política, estado esperado,
decision_id, input/digest y outcome; razones, reglas y aplicación están en el cuerpo
versionado. Se comprueban el digest de entrada con el dominio EC-GOV-02 y el SHA-256
del cuerpo. Los artefactos contienen el perfil JSON y las cuatro fuentes exactas
del motor de referencia; sus digests se verifican y el código archivado nunca se ejecuta.

El FK apunta al registro de contexto de gobernanza, no a `purchases`. Esto permite
registrar rechazos sin inventar una fila comercial. El futuro backend deberá validar
la compra/owner al registrar el contexto. No se añade una segunda fuente de verdad
comercial ni se declara resuelta la autenticación de episodio.

## Escritura, replay y atomicidad

1. Registrar contexto/artefactos mediante configuración backend explícita.
2. Fuera de la transacción de escritura, `prepare_decision` valida el registro y
   reproduce el resultado con los artefactos verificados de la referencia Python.
   Admite únicamente esos bytes exactos en la instancia del store; no es una capability
   comercial ni un permiso persistido. Reabrir el store requiere preparar de nuevo.
3. Dentro de una transacción controlada por aplicación, `record_decision` revalida
   tipos, contexto, referencias, versión esperada y preparación; después inserta.
   No invoca el evaluador dentro de esa transacción.
4. Para `transition_committed`, exigir referencia/versiones coherentes y un
   `LocalTransitionVerifier` backend que consulte la transición local real sobre esa
   misma conexión. No basta aportar el nombre de una transición.
5. Si falla una operación local posterior, el llamador revierte ambas escrituras.

No se insertó este flujo en servicios comerciales. Los métodos actuales del store B0
abren sus propias conexiones/transacciones; no se debe asumir que puedan combinarse
con este puerto sin una futura composición UoW explícita. No se declara atomicidad
con merchant/proveedor ni con efectos externos.

La secuencia es contigua desde 1 por run y las versiones esperadas no retroceden.
SQL protege de UPDATE/DELETE y de INSERT OR REPLACE con triggers, incluso con
recursive_triggers desactivado. Repetir evidencia idéntica es idempotente; reutilizar
identidad con cuerpo diferente, secuencia ocupada o decision_id de otra compra falla.
Un fallo de aplicación conserva la decisión original y añade otro registro; si falló
la escritura de la decisión, no se inventa su padre para registrar el fallo operativo.

Lectura revalida cuerpo, digests, referencias y proyecciones. Replay compara outcome,
razones y rule_results, conserva el snapshot de entrada y exige código instalado con
la misma identidad que el archivo recuperable. Sin esa versión, falla de forma explícita;
no ejecuta snapshots Python históricos ni usa OPA como supuesto motor disponible.

## Privacidad y alcance de garantías

El parser rechaza campos extra, payloads no canónicos, tipos coercibles, versiones
desconocidas y resultados incoherentes. La validación de instancias ocurre antes de
serializar: así un modelo construido con bool en un entero no se convierte en 1.
Los diagnósticos solo permiten rutas de campos conocidas del esquema; no guardan
nombres arbitrarios inyectados ni valores rechazados. Errores públicos usan únicamente
el catálogo GOV existente, sin copiar errores SQL ni payloads sensibles.

Esto no es un detector general de secretos ocultos en IDs sintácticamente válidos:
IDs y referencias tienen que venir del backend. D30 sigue pendiente en retención,
controles de acceso, protección operativa, exportación y auditoría de datos reales.
Append-only de interfaz/SQL no protege frente a un administrador capaz de alterar
el esquema o el archivo de base de datos. D32 no tiene límites ni protección DoS medidos.

C09 se prueba con SQLite real y conexiones concurrentes. C10 se prueba con marcadores
sintéticos y rechazo sin persistencia del payload. C11 cubre rollback y recuperación
locales con una tabla de transición sintética; sigue pendiente la integración de
enforce y la pérdida de observación shadow en compras. No se presentan estos tests
como observación de checkout, pagos, firma o efectos comerciales.

## Validación y preservación

Validación real en Windows, Python 3.12.6 y SQLite 3.45.3. No se ejecutó Linux.
Se usaron las dependencias existentes, `UV_NO_SYNC=true`, `UV_OFFLINE=true`,
`RUFF_NO_CACHE=true` y temporales bajo `.local/gov04/`. E2E/proveedores reales
deshabilitados; sin sincronización ni modificación de dependencias.

| Control final | Resultado |
| --- | --- |
| `uv run pytest tests/governance/test_evidence.py` | **92 passed in 4.05s**, código 0, sin omisiones ni advertencias |
| `uv run ruff format --check .` | `120 files already formatted`, código 0 |
| `uv run ruff check .` | `All checks passed!`, código 0 |
| `uv run pyright` | `0 errors, 0 warnings, 0 informations`, código 0 |
| `uv run pytest` | **423 passed, 3 skipped, 1 warning in 34.52s**, código 0 |
| `git diff --check` y revisión de los archivos nuevos en índice temporal | Sin errores; el índice real permanece vacío |

Los dos comandos Pytest añadieron `--junitxml=.local/gov04/unit-final-v3.xml` y
`--junitxml=.local/gov04/full-final-v3.xml`, respectivamente, con
`PYTEST_ADDOPTS='-p no:cacheprovider --basetemp=C:/ANDES/Tesis/tesis-commerce-agent/.local/gov04/unit-final-v3'`
o `full-final-v3`. Las omisiones son los tres E2E conocidos que requieren servicios
externos. La advertencia es la deprecación de `anyio.abc.BlockingPortal` en Starlette.

| Matriz de tests nuevos, sin duplicar casos | Aprobados |
| --- | --- |
| Escritura, lectura, reapertura y replay D1 HP/HNP como metadatos | 8 |
| C09: append-only, unicidad, secuencias/versiones, concurrencia y FK SQL | 20 |
| C11/perímetro local: transacción, preparación, transición, fallos y recuperación | 18 |
| Vínculos, artefactos, contratos estrictos y C10/privacidad | 42 |
| Compatibilidad de bases/row factory, esquema incompatible y lectura corrupta | 4 |
| **Total** | **92** |

Incidencias corregidas durante desarrollo: validar tras serializar permitía perder
el tipo bool de una instancia construida; ahora se valida antes. Una prueba de FK
reutilizaba un decision_id de otro run y activaba correctamente la guarda de ámbito
antes de alcanzar el FK: se corrigió el fixture para aislar el FK y se mantuvo la
aserción exacta `FOREIGN KEY`. Los XML intermedios con fallo se conservaron en
`.local/gov04/`; no se eliminó ni omitió ninguna prueba para conseguir el resultado final.

Los 278 archivos previamente versionados se compararon por SHA-256 crudo antes y
después: todos idénticos. Incluye B0/históricos, fuentes comerciales, SQLite existente,
ACP/AP2, contratos publicados, `.gitattributes` y `uv.lock`.
La identidad Python tampoco cambió:
`sha256:b1b5286deaf7741804bb0efda0017f1162d87cfc2de0efdacae545ab38457a14`.
No se abrió ninguna base operativa existente: la compatibilidad se probó creando
una base temporal con el `SqliteAgentStore` real, conservando esquema y filas comerciales.
Sin B0, migraciones operativas, E2E, servicios externos ni cambios en otros repositorios.

Archivos nuevos: `governance/evidence.py`, `governance/evidence_artifacts.py`,
`governance/evidence_ports.py`, `persistence/governance_sqlite.py` bajo
`src/commerce_agent/`; `tests/governance/test_evidence.py` y este informe.
No se modificó ningún archivo que ya estuviera versionado en la base.

## Estado de decisiones

| Decisión | Implementado aquí | Pendiente |
| --- | --- | --- |
| D16 | Correlación D1, contexto backend explícito y detección de referencias de otra compra | Resolver identidad/ownership real y referencias operativas al integrar |
| D18 | Tablas aditivas, PK/FK/unique/índices, compatibilidad en base temporal con el store existente | Migración operativa revisada, snapshots/intentos y lifecycle completo |
| D23 | Puerto de conexión compartida, preparación fuera de SQL, rollback/commit locales probados | UoW/CAS comercial, carreras en compras, outbox y recuperación de efectos externos |
| D30 | Modelos cerrados, entrada mínima replayable, artefactos verificables y diagnósticos seguros | Retención, ACL, protección operativa/exportación y validación con datos reales |

Sin selección de OPA, H01, D17, D2/D3 o implementación de autoridad HNP.
HP/HNP solo se conservan como metadatos D1. Ningún modo se activó en compras.
