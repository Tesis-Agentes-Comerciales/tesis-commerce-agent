# Monitor HP/HNP de solo lectura — MON-02A/B

**PARTIAL.** Reproducción de artefactos históricos cerrados, sin autoridad comercial, inferencias LLM ni eventos en vivo. Valida un monitor de reproducción y un GET local de fixtures; no una compra gobernada completa.

## Ejecutar la demo

Requiere Node.js 24 o posterior y npm; el lanzador solo usa Node, sin shell ni comandos del sistema. No necesita credenciales, servicios comerciales ni bases de datos. Desde la raíz del clon:

```sh
npm ci --prefix ui
npm ci --prefix auth
cd ui && npm run governance:demo
```

`node scripts/governance_demo.mjs` desde la raíz es equivalente. El lanzador:

1. prepara la fixture con `scripts/prepare_governance_snapshot.mjs --demo-fixtures-only` en `.local/governance-demo/fixtures`. La preparación es determinista y no vuelve a ejecutar escenarios. Nunca sobrescribe una fixture distinta: si una caché de un bundle anterior falla con `EXISTING_FIXTURE_NOT_OVERWRITTEN`, se borra ese directorio y se vuelve a lanzar;
2. construye la UI con `VITE_GOVERNANCE_MONITOR=true` en `.local/governance-demo/ui-dist`, de modo que `ui/dist` sigue siendo el build por defecto, sin monitor;
3. levanta tres procesos en loopback:

| Proceso | URL | Notas |
|---|---|---|
| Preview de auditoría (`ui/governance-preview.config.mjs`) | http://127.0.0.1:5182/governance | Reenvía solo `/api/auth`; el resto de `/api/` responde 503 |
| Servidor de snapshots (`scripts/governance_snapshot_server.mjs`) | http://127.0.0.1:5183/snapshots/hp-hnp-demo-v1 | GET de la fixture fijada por hash |
| Better Auth (`auth/`) | http://127.0.0.1:4210 | Secreto efímero y base `.local/governance-demo/auth.db` nueva en cada arranque |

Cuando los tres responden, imprime las URL y el hash del snapshot. Ctrl+C los detiene todos; si uno termina, el lanzador detiene los demás. Un puerto ocupado detiene el arranque. Con `AUTH_DEV_UPSTREAM` definido no se levanta Better Auth y el preview reenvía `/api/auth` a ese origen.

En http://127.0.0.1:5182/governance se crea una cuenta sintética (correo y contraseña cualesquiera, sin verificación). El monitor comparte la puerta de sesión de Better Auth de la app (`ui/src/components/session-gate.tsx`): sin sesión solo aparece el formulario de acceso. El modo inicial es **Reproducción offline**; **Snapshot backend local** lee la fixture histórica mediante el GET. No equivale a observabilidad de un backend comercial. No usar `NODE_ENV=production`: el lanzador y el servidor lo rechazan.

## Cuatro recorridos

| Recorrido | Fuente incluida | Alcance |
|---|---|---|
| HP creación observada | sources/agent-received.json | Un POST D17 histórico; creación y recuperación found. Veredicto textual D1 ausente: no se infiere de IDs/digests ni del POST, y la etiqueta no afirma «permitido» (solo un allow D1 aplicado podría). |
| HP bloqueado D1 | sources/http-deny.json | Q allow shadow, D1 deny enforce, cero POST y efectos; creación/recuperación no aplicables. |
| HNP accept erróneo contenido | sources/hnp-false-accept-selected.json y ledger | HNP-01 scripted offline; accept/budget pass guionado frente a BACKEND_DENIED/budget, 13000 > 12500, sin efecto ficticio. No ejecutó PDP D1. |
| HP respuesta perdida | sources/agent-lost.json | Timeout inicial, creación incierta, recuperación found posterior; un POST histórico, sin reenvío. |

Se separan ejecución, creación, recuperación y efecto observado; ausencia de dato no significa éxito. El adaptador rechaza contradicciones, incluido deny con efecto o confirmación sin inicio, y no reconstruye campos ausentes a partir de IDs o digests. El exportador emite directamente `governance-monitor/3`: bloqueo y no inicio quedan como creación/recuperación no aplicables sin traducción en el navegador, y la UI solo acepta v3. Los cuatro escenarios conservan exactamente su significado de v2; v3 solo añade `experiments`. Los JSON originales de `sources/` no cambian; un test Python comprueba que reproducen byte a byte el bundle incluido. El validador HNP y el backend se muestran separadamente.

MON-02A corrigió tres errores de presentación: un badge de confirmado antes de comenzar, la confirmación de creación/recuperación atribuida a un bloqueo y el budget pass del validador presentado como control backend aprobado. La fuente HNP contiene realmente ese pass erróneo y el deny backend por 13000 > 12500; se conservan ambos. HNP-01 no ejecuta PDP D1, no conecta negocio y no representa dos LLM independientes.

## Experimento HNP-02 (dry-run)

Un quinto botón, en una navegación aparte («Experimentos HNP-02»), muestra una corrida HNP-02 Fase 1: brazo A con payload guionado y controles backend frente a brazo B con lo mismo más un validador LLM, sobre un sink ficticio en memoria. No es un escenario: no tiene creación, recuperación ni badge de confirmación. La vista abre siempre con «Experimento en entorno ficticio, sink ficticio, no es operación real.» y, salvo que el transporte declarado sea `provider`, con «Cifras de transporte simulado, no miden ningún modelo.». Muestra actores, métricas por brazo con IC de Wilson al 95 %, tablas pareadas con McNemar exacto, costo y latencia, desviaciones y, por caso, agregados de todas las repeticiones más la repetición 1 de ambos brazos con las declaraciones del validador frente a los controles backend.

| Campo | dry-run incluido | corrida con proveedor |
|---|---|---|
| `executionKind` | `llm validator dry-run` | `llm validator provider` |
| `transport` / transporte del validador | `fake` / `fake` | `provider` / `openai-responses` |
| Base de costo | `fake_transport_estimate` | `provider_reported_usage` |

Cualquier otra combinación, tipo o transporte se rechaza. La regla HNP de los escenarios (solo `scripted offline` con validador) no cambia: los tipos LLM solo se aceptan como experimentos, con sus propias invariantes (A sin validador, B nunca admite lo que A bloqueó, B ⊆ A en efectos, Wilson coherente con k/n, pares que suman). La UI, el exportador y [governance-monitor-3.schema.json](governance-monitor-3.schema.json) (JSON Schema 2020-12) comparten vocabularios; los tests de Python y Node comprueban que esquema y parsers aceptan y rechazan lo mismo.

La corrida incluida es `hnp02-dry_run-20261020T044329Z-s20261022`: FakeTransport, `.env.example`, semilla 20261022 y `HNP_REPETITIONS=2` (19 casos × 2 repeticiones × 2 brazos = 76 episodios; 36 llamadas simuladas, 0 al proveedor; USD 0,0234 estimados). Sus `summary.json` y `traces.jsonl` están en `sources/hnp02/<run>/`; `evidence.jsonl` no se versiona. La proyección no copia razones del modelo, prompts, settings ni entorno: solo códigos, estados y conteos, y rechaza la corrida entera ante cadenas con forma de secreto, claves duplicadas o números no finitos. Para generar y proyectar otra corrida sin llamadas reales:

```sh
env -u OPENAI_API_KEY HNP_REPETITIONS=2 uv run python scripts/run_hnp_llm_matrix.py --dry-run --env-file .env.example
# copiar summary.json y traces.jsonl de .local/.../<run> a docs/governance-monitor/sources/hnp02/<run>/
uv run python scripts/export_governance_monitor.py --versioned-sources --hnp02-run docs/governance-monitor/sources/hnp02/<run>
uv run python scripts/export_governance_monitor.py --versioned-sources --hnp02-run docs/governance-monitor/sources/hnp02/<run> --verify-only
```

El exportador nunca copia corridas por su cuenta: versionar una es una decisión explícita. Las corridas con proveedor se proyectan igual desde `.local`, pero el snapshot de demostración solo admite experimentos dry-run.

## GET de demostración

CLI separado, `127.0.0.1:5183` por defecto, lista fija con un único ID `hp-hnp-demo-v1`; máximo 65536 bytes, contrato `governance-demo-snapshot/1`, hash fijado, validación de redacción y consistencia antes de servir. Rechaza rutas desconocidas, traversal, query strings, métodos distintos de GET, credenciales entrantes, Host y Origin ajenos. No importa runtime, SDKs ni bases de datos. La CLI requiere `--demo-fixtures-only`; no se habilita en producción.

Los valores locales viven solo en `ui/src/features/governance-monitor/demo-config.ts` y cada uno se cambia por entorno: `VITE_GOVERNANCE_SNAPSHOT_PORT` (5183; lo leen el build de la UI y el servidor, así que debe coincidir en ambos), `GOVERNANCE_SNAPSHOT_ORIGINS` (orígenes CORS loopback separados por comas; por defecto `http://127.0.0.1:5182,http://127.0.0.1:5179`) y `GOVERNANCE_DEMO_DIR` (`.local/governance-demo`, relativo a la raíz). El lanzador los toma de ahí y los pasa al build y al servidor. Un puerto u origen inválido, o un origen no loopback, detiene el arranque. El host 127.0.0.1 no es configurable. El hash del snapshot tampoco es una variable: es el ancla de integridad que la UI compara con los bytes servidos, vive en `snapshot-pin.ts` y `npm run test` lo contrasta con la preparación determinista; el hash del bundle fuente se toma de `provenance.json`.

**El GET no tiene autenticación real** (la pantalla sí exige sesión): solo fixtures sanitizadas, nunca evidencia de usuarios autenticados. Loopback y CORS no sustituyen autenticación ni protegen frente a un proceso local hostil. Los patrones de redacción tampoco son una garantía general para nuevos datos: la lista fija y los hashes limitan esta demo a los artefactos revisados.

La UI valida también hash, tamaño y contrato. Distingue leído, desconectado, obsoleto e inconsistente; no cambia a offline automáticamente ni mezcla campos. Un error tras una lectura válida conserva el último snapshot completo marcado obsoleto; una inconsistencia oculta toda la evidencia de esa fuente. El retorno offline requiere selección explícita. La hora de lectura del navegador no es la hora del evento.

## Verificación: 110 tests distintos

```sh
cd ui && npm run lint && npm run build && npm run test
uv run pytest tests/governance_monitor
```

33 adaptador/render/copy + 13 experimento HNP-02 y esquema compartido + 28 snapshots/configuración + 9 routing hermético con y sin flag + 4 puerta de sesión = 87 tests Node, más 23 de artefactos Python (8 HP/HNP-01 + 15 HNP-02) = 110. `npm run test` prepara la fixture en memoria desde la evidencia versionada (sin `.local`) y el CI lo ejecuta junto a lint y build, además de comprobar que el build por defecto no incluya el monitor; los tests Python entran en `uv run pytest`. El test Node del esquema usa un verificador del subconjunto de palabras clave que rechaza cualquier otra. Los 28 de snapshots incluyen transporte HTTP real limitado al servidor de fixtures de prueba y casos con dobles; no constituyen integración Agent–Merchant. Los POST negativos prueban 405 en ese servidor, no son POST comerciales. No ejecutar suites comerciales opt-in para esta demo.

## Fuentes y trazabilidad

[provenance.json](provenance.json) fija seis hashes históricos, 54 resultados HNP contrastados en la exportación cerrada y 65 entradas de ledger. El test publicado comprueba el registro seleccionado contra esas 65 entradas; no se adjudica nuevamente los otros 53 resultados. La extracción seleccionada no tiene el hash del results.json original. Los digests de ReviewRecord y de entrada de ledger tienen dominios distintos; no son firmas ni consentimiento humano.

HP procede del candidato documental v2 cerrado, evidencia `prior/agent-received.json`, `prior/agent-lost.json` y `http-deny.json`, ejecutada sobre Agent `83510f8dd5fbe5026d909df2b4949802b8b2aa1a`. HNP procede del programa [HNP-01 a633a04](https://github.com/Maldinho18/tesis-commerce-agent/commit/a633a043337286406f9a3e859809556ce46bcba1), condición B / validator-false-accept, sink ficticio y roles programados. No se portó ni ejecutó el harness original.

El bundle v3 incluido tiene el SHA-256 que registra `provenance.json` (`projection_sha256`, hoy `dc07e2ea9e2155665f33d8dc76b253b20b1b2727ba18b36b4ebad15748cfe5e4`, junto a los hashes de la corrida HNP-02 en `hnp02Runs`); el snapshot determinista tiene SHA-256 `40ae9fb0f38d50322eb984c725c6532fe588acfe3f7124c1d16151be6c78e132`. El `.gitattributes` de la raíz protege solo los bytes con hash: `sources/` (copias de evidencia sellada, algunas en CRLF) se versiona sin conversión y `evidence.json` se mantiene en LF en cualquier plataforma. Los hashes prueban consistencia de bytes, no firma del productor ni consentimiento humano.

El exportador Python se conserva para auditoría opcional con `--hp-root <directorio-evidence> --hnp-root <directorio-hnp> [--hnp02-run <dir>] --verify-only`. Requiere originales fijados, no necesarios para ejecutar la demo; sin ellos, `--versioned-sources` y `tests/governance_monitor` reproducen el bundle desde las copias de `sources/`. No regenerar ni modificar la evidencia para adaptar resultados. No se incluyen DB, logs, ZIP, capturas, credenciales ni cachés.

## Integración y límites

`/governance` y `/governance/` existen solo en builds con `VITE_GOVERNANCE_MONITOR=true`. Se seleccionan en `ui/src/main.tsx` antes de importar App y pasan por la misma puerta de sesión Better Auth, sin cargar el runtime comercial ni SSE; las demás rutas conservan App. No se modifican SSE, D17, D2 ni políticas. El preview de demostración reenvía solo `/api/auth` y bloquea las demás API; no valida funcionalmente una compra. Los cambios al punto de entrada `ui/src/main.tsx` deben coordinarse con el resto de la UI.

Se mantienen **PARTIAL**, I10/D08/D09 parciales, las limitaciones ASGI y de identidad sintética, las de recuperación histórica y **AUDIT-MERCHANT-CANCEL-01**: dos ejecuciones de cancelación conservadas y gate histórico sin aprobar. La UI no agrega evidencia para cerrar esos gates. En evidencias D2, commercial_posts=0 significa cero POST adicionales iniciados por D2, no ausencia de POST D17 previos. Los siete hashes de nodeids redactados corresponden a originales y no se recalculan desde sus identificadores sanitizados.

## Pendiente

Ninguno de estos incrementos está implementado ni autorizado aquí; el monitor solo proyecta el dry-run versionado.

- **MON-02C (lectura autenticada):** requiere diseño separado con autenticación real, ownership por ejecución y operación, permisos de lectura, un contrato de eventos con cursores, redacción en el servidor y pruebas de ausencia de mutación. No conectar el SSE comercial por conveniencia.
- **HNP-02 con proveedor (dos LLM independientes):** necesita oráculos preregistrados, roles y proveedores independientes, control de costos y credenciales por rol, aislamiento del sink y prohibición verificable de checkout y pagos. Se prepara una propuesta antes de ejecutar proveedores.

## Historia

- MON-02A/B se desarrolló en la rama `codex/hnp-ui-readonly-monitor` sobre Agent `83510f8` y se validó en Windows PowerShell 5.1 con Node 24.15.0: 69 tests distintos en el corte MON-02B. El primer intento de los tests HTTP chocó con EACCES del sandbox en sockets loopback y se repitió con acceso al transporte local; los 20 requests registrados apuntaron solo al listener de fixtures de prueba. La revisión en navegador del preview confirmó `/governance` offline y la lectura del snapshot, sin enviar formularios.
- La publicación del 19 de octubre de 2026 añadió un manifiesto `PUBLICATION-SHA256SUMS` y atributos `-text` por directorio para que su verificación sobreviviera a clones en Windows, junto con un lanzador PowerShell y la caché `.local/mon-02b-2026-10-18`. Se integró en #14 y el contrato v3 con HNP-02 en #18.
- El 20 de octubre de 2026 el lanzador pasó a Node (`npm run governance:demo`), con Better Auth incluido y la caché en `.local/governance-demo`. Se retiraron el manifiesto y los atributos por directorio: Git ya fija los bytes versionados, y `provenance.json` conserva los hashes que importan.
- [CHANGELOG.md](CHANGELOG.md) detalla cada corte. [hnp-01-inventory.md](hnp-01-inventory.md) es el inventario del 18 de octubre, anterior a la integración de HNP-01 (#16); sus conteos de tests son de ese corte.
