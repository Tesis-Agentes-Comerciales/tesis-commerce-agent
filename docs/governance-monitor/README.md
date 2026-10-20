# Monitor HP/HNP de solo lectura — MON-02A/B

**PARTIAL.** Base organizacional `83510f8dd5fbe5026d909df2b4949802b8b2aa1a`; rama `codex/hnp-ui-readonly-monitor`. Reproducción histórica, sin autoridad comercial, inferencias LLM ni eventos en vivo.

## Reproducir en Windows PowerShell 5.1

Requiere Git, Node.js 24 (probado con 24.15.0), npm y Python 3.12 para los cinco tests de artefactos. No se necesitan credenciales, servicios comerciales ni bases de datos. Ejecutar desde la raíz del clon:

```powershell
git clone --branch codex/hnp-ui-readonly-monitor --single-branch https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent.git
Set-Location tesis-commerce-agent
Push-Location ui
npm.cmd ci --ignore-scripts --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw 'Instalacion UI fallida' }
npm.cmd run build
if ($LASTEXITCODE -ne 0) { throw 'Build fallido' }
Pop-Location
node scripts/prepare_governance_snapshot.mjs --demo-fixtures-only
if ($LASTEXITCODE -ne 0) { throw 'Fixture rechazada' }
powershell.exe -NoProfile -File scripts/governance_snapshot_demo.ps1 -Action Start
# Servidor estatico separado: bloquea /api/, sin proxy comercial.
node ui/node_modules/vite/bin/vite.js preview ui --config ui/governance-preview.config.mjs
```

Abrir **http://127.0.0.1:5182/governance**. El modo inicial **Reproducción offline** funciona incluso sin iniciar el servicio de snapshots. **Snapshot backend local** lee exclusivamente la fixture histórica mediante un GET local. No equivale a observabilidad de un backend comercial.

Para detener: Ctrl+C en la terminal de la UI y, desde otra terminal en la raíz del clon:

```powershell
powershell.exe -NoProfile -File scripts/governance_snapshot_demo.ps1 -Action Stop
```

Stop comprueba la identidad del proceso registrado; no detiene todos los procesos Node. Los puertos 5182/5183 deben estar libres. No usar NODE_ENV=production: el servicio lo rechaza. La preparación genera únicamente una caché descartable en `.local`; no vuelve a ejecutar los escenarios.

## Cuatro recorridos

| Recorrido | Fuente incluida | Alcance |
|---|---|---|
| HP creación observada | sources/agent-received.json | Un POST D17 histórico; creación y recuperación found. Veredicto textual D1 ausente: no se infiere de IDs/digests ni del POST, y la etiqueta no afirma «permitido» (solo un allow D1 aplicado podría). |
| HP bloqueado D1 | sources/http-deny.json | Q allow shadow, D1 deny enforce, cero POST y efectos; creación/recuperación no aplicables. |
| HNP accept erróneo contenido | sources/hnp-false-accept-selected.json y ledger | HNP-01 scripted offline; accept/budget pass guionado frente a BACKEND_DENIED/budget, 13000 > 12500, sin efecto ficticio. No ejecutó PDP D1. |
| HP respuesta perdida | sources/agent-lost.json | Timeout inicial, creación incierta, recuperación found posterior; un POST histórico, sin reenvío. |

Se separan ejecución, creación, recuperación y efecto observado; ausencia de dato no significa éxito. El adaptador rechaza contradicciones, incluido deny con efecto o confirmación sin inicio. El exportador emite directamente `governance-monitor/2`: bloqueo y no inicio quedan como creación/recuperación no aplicables sin traducción en el navegador, y la UI solo acepta v2. Los JSON originales de `sources/` no cambian; un test Python comprueba que reproducen byte a byte el bundle incluido. El validador HNP y el backend se muestran separadamente.

## GET de demostración

CLI separado, `127.0.0.1:5183`, lista fija con un único ID `hp-hnp-demo-v1`; máximo 65536 bytes, contrato `governance-demo-snapshot/1`, hash fijado, validación de redacción y consistencia antes de servir. Rechaza rutas desconocidas, traversal, query strings, métodos distintos de GET, credenciales entrantes, Host y Origin ajenos. No importa runtime, SDKs ni bases de datos. La CLI requiere `--demo-fixtures-only`; no se habilita en producción.

**No tiene autenticación real**: solo fixtures sanitizadas, nunca evidencia de usuarios autenticados. Loopback y CORS no sustituyen autenticación ni protegen frente a un proceso local hostil. Los patrones de redacción tampoco son una garantía general para nuevos datos: la lista fija y los hashes limitan esta demo a los artefactos revisados.

La UI valida también hash, tamaño y contrato. Distingue leído, desconectado, obsoleto e inconsistente; no cambia a offline automáticamente ni mezcla campos. Un error tras una lectura válida conserva el último snapshot completo marcado obsoleto; una inconsistencia oculta toda la evidencia de esa fuente. El retorno offline requiere selección explícita. La hora de lectura del navegador no es la hora del evento.

## Verificación segura: 69 tests distintos

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

30 adaptador/render + 26 snapshots + 8 routing hermético + 5 artefactos = 69. `npm run test` prepara la fixture en memoria desde la evidencia versionada (sin `.local`) y el CI lo ejecuta junto a lint y build; los cinco tests Python entran en `uv run pytest`. Los 26 incluyen transporte HTTP real limitado al servidor de fixtures de prueba y casos con dobles; no constituyen integración Agent–Merchant. Los POST negativos prueban 405 en ese servidor, no son POST comerciales. No ejecutar suites comerciales opt-in para esta demo. [RESULTS.md](RESULTS.md) detalla resultados y límites.

## Fuentes y trazabilidad

[provenance.json](provenance.json) fija seis hashes históricos, 54 resultados HNP contrastados en la exportación cerrada y 65 entradas de ledger. El test publicado comprueba el registro seleccionado contra esas 65 entradas; no se adjudica nuevamente los otros 53 resultados. La extracción seleccionada no tiene el hash del results.json original. Los digests de ReviewRecord y de entrada de ledger tienen dominios distintos; no son firmas ni consentimiento humano.

HP procede del candidato documental v2 cerrado, evidencia `prior/agent-received.json`, `prior/agent-lost.json` y `http-deny.json`, ejecutada sobre Agent `83510f8dd5fbe5026d909df2b4949802b8b2aa1a`. HNP procede del programa [HNP-01 a633a04](https://github.com/Maldinho18/tesis-commerce-agent/commit/a633a043337286406f9a3e859809556ce46bcba1), condición B / validator-false-accept, sink ficticio y roles programados. No se portó ni ejecutó el harness original.

El bundle v2 incluido tiene el SHA-256 que registra `provenance.json` (`projection_sha256`, hoy `4f0a53771e814cd37fc583fd6c9adb14dccb95cbfdca41684ec863da76a99490`); el snapshot determinista tiene SHA-256 `b51798061cf2258d20d51d00c05addc697656549188fdf7c3e5c6a502b90097e`. Los atributos Git limitados a los archivos del monitor conservan sus bytes (incluido JSON/JSONL) al clonar en Windows, sin cambiar reglas de otros componentes. [PUBLICATION-SHA256SUMS](PUBLICATION-SHA256SUMS) cubre los archivos publicados salvo el propio manifiesto.

El exportador Python se conserva para auditoría opcional con `--hp-root <directorio-evidence> --hnp-root <directorio-hnp> --verify-only`. Requiere originales fijados, no necesarios para ejecutar la demo; sin ellos, `tests/governance_monitor` reproduce el bundle desde las copias de `sources/`. No regenerar ni modificar la evidencia para adaptar resultados. No se incluyen DB, logs, ZIP, capturas, credenciales ni cachés.

## Integración pendiente

`/governance` y `/governance/` se seleccionan antes de importar App; las demás rutas conservan App. No se modifican autorización, SSE, D17, D2 ni políticas. El preview de demostración bloquea las API y permite revisar la pantalla normal sin iniciar sus servicios; no valida funcionalmente una compra ni una sesión humana real.

Conservar **PARTIAL**, I10/D08/D09 parciales y **AUDIT-MERCHANT-CANCEL-01**: dos ejecuciones de cancelación conservadas y gate histórico sin aprobar. ASGI, identidad sintética y las limitaciones de recuperación histórica no se resuelven con esta UI. En evidencias D2, commercial_posts=0 significa cero POST adicionales iniciados por D2, no ausencia de POST D17 previos. Los siete hashes de nodeids redactados corresponden a originales y no se recalculan desde sus identificadores sanitizados.

[HANDOFF-YAIR.md](HANDOFF-YAIR.md) y [CHANGELOG.md](CHANGELOG.md) separan lo implementado de MON-02C (observabilidad autenticada) y HNP-02 (dos LLM independientes), ambos pendientes y no autorizados aquí.
