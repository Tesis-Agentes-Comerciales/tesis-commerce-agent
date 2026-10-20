# Monitor HP/HNP de solo lectura — MON-02A/B

**PARTIAL.** Base organizacional `83510f8dd5fbe5026d909df2b4949802b8b2aa1a`; rama `codex/hnp-ui-readonly-monitor`. Reproducción histórica, sin autoridad comercial, inferencias LLM ni eventos en vivo.

## Reproducir en Windows PowerShell 5.1

Requiere Git, Node.js 24 (probado con 24.15.0), npm y Python 3.12 para los 23 tests de artefactos. No se necesitan credenciales, servicios comerciales ni bases de datos. Ejecutar desde la raíz del clon:

```powershell
git clone --branch codex/hnp-ui-readonly-monitor --single-branch https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent.git
Set-Location tesis-commerce-agent
Push-Location ui
npm.cmd ci --ignore-scripts --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw 'Instalacion UI fallida' }
$env:VITE_GOVERNANCE_MONITOR='true'
npm.cmd run build
if ($LASTEXITCODE -ne 0) { throw 'Build fallido' }
Pop-Location
node scripts/prepare_governance_snapshot.mjs --demo-fixtures-only
if ($LASTEXITCODE -ne 0) { throw 'Fixture rechazada' }
powershell.exe -NoProfile -File scripts/governance_snapshot_demo.ps1 -Action Start
# Servidor estatico separado: solo reenvia /api/auth; el resto de /api/ responde 503.
node ui/node_modules/vite/bin/vite.js preview ui --config ui/governance-preview.config.mjs
```

Abrir **http://127.0.0.1:5182/governance** e iniciar sesión: el monitor comparte la puerta de Better Auth de la app, así que requiere el servicio `auth/` (puerto 4210 o `AUTH_DEV_UPSTREAM`); sin sesión solo aparece el formulario de acceso. Sin `VITE_GOVERNANCE_MONITOR=true` en el build, el monitor no existe y la ruta muestra la app. El modo inicial **Reproducción offline** funciona incluso sin iniciar el servicio de snapshots. **Snapshot backend local** lee exclusivamente la fixture histórica mediante un GET local. No equivale a observabilidad de un backend comercial.

Para detener: Ctrl+C en la terminal de la UI y, desde otra terminal en la raíz del clon:

```powershell
powershell.exe -NoProfile -File scripts/governance_snapshot_demo.ps1 -Action Stop
```

Stop comprueba la identidad del proceso registrado; no detiene todos los procesos Node. Los puertos 5182/5183 (o los configurados, ver abajo) deben estar libres. No usar NODE_ENV=production: el servicio lo rechaza. La preparación genera únicamente una caché descartable en `.local`; no vuelve a ejecutar los escenarios.

## Cuatro recorridos

| Recorrido | Fuente incluida | Alcance |
|---|---|---|
| HP creación observada | sources/agent-received.json | Un POST D17 histórico; creación y recuperación found. Veredicto textual D1 ausente: no se infiere de IDs/digests ni del POST, y la etiqueta no afirma «permitido» (solo un allow D1 aplicado podría). |
| HP bloqueado D1 | sources/http-deny.json | Q allow shadow, D1 deny enforce, cero POST y efectos; creación/recuperación no aplicables. |
| HNP accept erróneo contenido | sources/hnp-false-accept-selected.json y ledger | HNP-01 scripted offline; accept/budget pass guionado frente a BACKEND_DENIED/budget, 13000 > 12500, sin efecto ficticio. No ejecutó PDP D1. |
| HP respuesta perdida | sources/agent-lost.json | Timeout inicial, creación incierta, recuperación found posterior; un POST histórico, sin reenvío. |

Se separan ejecución, creación, recuperación y efecto observado; ausencia de dato no significa éxito. El adaptador rechaza contradicciones, incluido deny con efecto o confirmación sin inicio. El exportador emite directamente `governance-monitor/3`: bloqueo y no inicio quedan como creación/recuperación no aplicables sin traducción en el navegador, y la UI solo acepta v3. Los cuatro escenarios conservan exactamente su significado de v2; v3 solo añade `experiments`. Los JSON originales de `sources/` no cambian; un test Python comprueba que reproducen byte a byte el bundle incluido. El validador HNP y el backend se muestran separadamente.

## Experimento HNP-02 (dry-run)

Un quinto botón, en una navegación aparte («Experimentos HNP-02»), muestra una corrida HNP-02 Fase 1: brazo A con payload guionado y controles backend frente a brazo B con lo mismo más un validador LLM, sobre un sink ficticio en memoria. No es un escenario: no tiene creación, recuperación ni badge de confirmación. La vista abre siempre con «Experimento en entorno ficticio, sink ficticio, no es operación real.» y, salvo que el transporte declarado sea `provider`, con «Cifras de transporte simulado, no miden ningún modelo.». Muestra actores, métricas por brazo con IC de Wilson al 95 %, tablas pareadas con McNemar exacto, costo y latencia, desviaciones y, por caso, agregados de todas las repeticiones más la repetición 1 de ambos brazos con las declaraciones del validador frente a los controles backend.

| Campo | dry-run incluido | corrida con proveedor |
|---|---|---|
| `executionKind` | `llm validator dry-run` | `llm validator provider` |
| `transport` / transporte del validador | `fake` / `fake` | `provider` / `openai-responses` |
| Base de costo | `fake_transport_estimate` | `provider_reported_usage` |

Cualquier otra combinación, tipo o transporte se rechaza. La regla HNP de los escenarios (solo `scripted offline` con validador) no cambia: los tipos LLM solo se aceptan como experimentos, con sus propias invariantes (A sin validador, B nunca admite lo que A bloqueó, B ⊆ A en efectos, Wilson coherente con k/n, pares que suman). La UI, el exportador y [governance-monitor-3.schema.json](governance-monitor-3.schema.json) (JSON Schema 2020-12) comparten vocabularios; los tests de Python y Node comprueban que esquema y parsers aceptan y rechazan lo mismo.

La corrida incluida es `hnp02-dry_run-20261020T044329Z-s20261022`: FakeTransport, `.env.example`, semilla 20261022 y `HNP_REPETITIONS=2` (19 casos × 2 repeticiones × 2 brazos = 76 episodios; 36 llamadas simuladas, 0 al proveedor; USD 0,0234 estimados). Sus `summary.json` y `traces.jsonl` están en `sources/hnp02/<run>/`; `evidence.jsonl` no se versiona. La proyección no copia razones del modelo, prompts, settings ni entorno: solo códigos, estados y conteos, y rechaza la corrida entera ante cadenas con forma de secreto, claves duplicadas o números no finitos. Para generar y proyectar otra corrida sin llamadas reales:

```bash
env -u OPENAI_API_KEY HNP_REPETITIONS=2 uv run python scripts/run_hnp_llm_matrix.py --dry-run --env-file .env.example
# copiar summary.json y traces.jsonl de .local/.../<run> a docs/governance-monitor/sources/hnp02/<run>/
uv run python scripts/export_governance_monitor.py --versioned-sources --hnp02-run docs/governance-monitor/sources/hnp02/<run>
uv run python scripts/export_governance_monitor.py --versioned-sources --hnp02-run docs/governance-monitor/sources/hnp02/<run> --verify-only
```

El exportador nunca copia corridas por su cuenta: versionar una es una decisión explícita. Las corridas con proveedor se proyectan igual desde `.local`, pero el snapshot de demostración solo admite experimentos dry-run.

## GET de demostración

CLI separado, `127.0.0.1:5183` por defecto, lista fija con un único ID `hp-hnp-demo-v1`; máximo 65536 bytes, contrato `governance-demo-snapshot/1`, hash fijado, validación de redacción y consistencia antes de servir. Rechaza rutas desconocidas, traversal, query strings, métodos distintos de GET, credenciales entrantes, Host y Origin ajenos. No importa runtime, SDKs ni bases de datos. La CLI requiere `--demo-fixtures-only`; no se habilita en producción.

Los valores locales viven solo en `ui/src/features/governance-monitor/demo-config.ts` y cada uno se cambia por entorno: `VITE_GOVERNANCE_SNAPSHOT_PORT` (5183; lo leen el build de la UI y el servidor, así que debe coincidir en ambos), `GOVERNANCE_SNAPSHOT_ORIGINS` (orígenes CORS loopback separados por comas; por defecto `http://127.0.0.1:5182,http://127.0.0.1:5179`) y `GOVERNANCE_DEMO_DIR` (`.local/mon-02b-2026-10-18`, relativo a la raíz). Un puerto u origen inválido, o un origen no loopback, detiene el arranque. El host 127.0.0.1 no es configurable. `governance_snapshot_demo.ps1` obtiene esos valores con `--print-config`. El hash del snapshot tampoco es una variable: es el ancla de integridad que la UI compara con los bytes servidos, vive en `snapshot-pin.ts` y `npm run test` lo contrasta con la preparación determinista; el hash del bundle fuente se toma de `provenance.json`.

**El GET no tiene autenticación real** (la pantalla sí exige sesión): solo fixtures sanitizadas, nunca evidencia de usuarios autenticados. Loopback y CORS no sustituyen autenticación ni protegen frente a un proceso local hostil. Los patrones de redacción tampoco son una garantía general para nuevos datos: la lista fija y los hashes limitan esta demo a los artefactos revisados.

La UI valida también hash, tamaño y contrato. Distingue leído, desconectado, obsoleto e inconsistente; no cambia a offline automáticamente ni mezcla campos. Un error tras una lectura válida conserva el último snapshot completo marcado obsoleto; una inconsistencia oculta toda la evidencia de esa fuente. El retorno offline requiere selección explícita. La hora de lectura del navegador no es la hora del evento.

## Verificación segura: 110 tests distintos

```powershell
npm.cmd --prefix ui run test
if ($LASTEXITCODE -ne 0) { throw 'Tests Node fallidos' }
$env:PYTHONDONTWRITEBYTECODE='1'
python -m unittest discover -s tests/governance_monitor -v
if ($LASTEXITCODE -ne 0) { throw 'Tests de artefactos fallidos' }
npm.cmd --prefix ui run lint
if ($LASTEXITCODE -ne 0) { throw 'Lint fallido' }
npm.cmd --prefix ui run build
if ($LASTEXITCODE -ne 0) { throw 'Build fallido' }
```

33 adaptador/render/copy + 13 experimento HNP-02 y esquema compartido + 28 snapshots/configuración + 9 routing hermético con y sin flag + 4 puerta de sesión + 23 artefactos (8 HP/HNP-01 + 15 HNP-02) = 110. `npm run test` prepara la fixture en memoria desde la evidencia versionada (sin `.local`) y el CI lo ejecuta junto a lint y build; los 23 tests Python entran en `uv run pytest`. Con Python sin dependencias, los tres que validan con `jsonschema` se omiten; el test Node equivalente usa un verificador del subconjunto de palabras clave del esquema que rechaza cualquier otra. Los 28 incluyen transporte HTTP real limitado al servidor de fixtures de prueba y casos con dobles; no constituyen integración Agent–Merchant. Los POST negativos prueban 405 en ese servidor, no son POST comerciales. No ejecutar suites comerciales opt-in para esta demo. [RESULTS.md](RESULTS.md) detalla resultados y límites.

## Fuentes y trazabilidad

[provenance.json](provenance.json) fija seis hashes históricos, 54 resultados HNP contrastados en la exportación cerrada y 65 entradas de ledger. El test publicado comprueba el registro seleccionado contra esas 65 entradas; no se adjudica nuevamente los otros 53 resultados. La extracción seleccionada no tiene el hash del results.json original. Los digests de ReviewRecord y de entrada de ledger tienen dominios distintos; no son firmas ni consentimiento humano.

HP procede del candidato documental v2 cerrado, evidencia `prior/agent-received.json`, `prior/agent-lost.json` y `http-deny.json`, ejecutada sobre Agent `83510f8dd5fbe5026d909df2b4949802b8b2aa1a`. HNP procede del programa [HNP-01 a633a04](https://github.com/Maldinho18/tesis-commerce-agent/commit/a633a043337286406f9a3e859809556ce46bcba1), condición B / validator-false-accept, sink ficticio y roles programados. No se portó ni ejecutó el harness original.

El bundle v3 incluido tiene el SHA-256 que registra `provenance.json` (`projection_sha256`, hoy `dc07e2ea9e2155665f33d8dc76b253b20b1b2727ba18b36b4ebad15748cfe5e4`, junto a los hashes de la corrida HNP-02 en `hnp02Runs`); el snapshot determinista tiene SHA-256 `40ae9fb0f38d50322eb984c725c6532fe588acfe3f7124c1d16151be6c78e132`. El ID sigue siendo `hp-hnp-demo-v1`: una caché `.local` preparada con el bundle v2 hace fallar la preparación con `EXISTING_FIXTURE_NOT_OVERWRITTEN`; borrarla y volver a preparar. Los atributos Git limitados a los archivos del monitor conservan sus bytes (incluido JSON/JSONL) al clonar en Windows, sin cambiar reglas de otros componentes. [PUBLICATION-SHA256SUMS](PUBLICATION-SHA256SUMS) cubre los archivos publicados salvo el propio manifiesto.

El exportador Python se conserva para auditoría opcional con `--hp-root <directorio-evidence> --hnp-root <directorio-hnp> [--hnp02-run <dir>] --verify-only`. Requiere originales fijados, no necesarios para ejecutar la demo; sin ellos, `--versioned-sources` y `tests/governance_monitor` reproducen el bundle desde las copias de `sources/`. No regenerar ni modificar la evidencia para adaptar resultados. No se incluyen DB, logs, ZIP, capturas, credenciales ni cachés.

## Integración pendiente

`/governance` y `/governance/` existen solo en builds con `VITE_GOVERNANCE_MONITOR=true` (el CI comprueba que el build por defecto no los incluya). Se seleccionan antes de importar App y pasan por la misma puerta de sesión Better Auth (`ui/src/components/session-gate.tsx`), sin cargar el runtime comercial ni SSE; las demás rutas conservan App. No se modifican SSE, D17, D2 ni políticas. El preview de demostración reenvía solo `/api/auth` y bloquea las demás API; no valida funcionalmente una compra.

Conservar **PARTIAL**, I10/D08/D09 parciales y **AUDIT-MERCHANT-CANCEL-01**: dos ejecuciones de cancelación conservadas y gate histórico sin aprobar. ASGI, identidad sintética y las limitaciones de recuperación histórica no se resuelven con esta UI. En evidencias D2, commercial_posts=0 significa cero POST adicionales iniciados por D2, no ausencia de POST D17 previos. Los siete hashes de nodeids redactados corresponden a originales y no se recalculan desde sus identificadores sanitizados.

[HANDOFF-YAIR.md](HANDOFF-YAIR.md) y [CHANGELOG.md](CHANGELOG.md) separan lo implementado de MON-02C (observabilidad autenticada) y HNP-02 con proveedor (dos LLM independientes), ambos pendientes y no autorizados aquí; el monitor solo proyecta el dry-run versionado.
