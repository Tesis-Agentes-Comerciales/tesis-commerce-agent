# Evidencia histórica de gobernanza HP/HNP

**PARTIAL.** Esta carpeta guarda la evidencia de gobernanza HP y HNP que la tesis produjo entre el
17 y el 20 de octubre de 2026. Son artefactos cerrados: no se regeneran, no se editan y no
representan una compra gobernada completa.

El monitor de solo lectura `/governance` que la mostraba se retiró el 20 de octubre de 2026. Lo
reemplazará la traza de compras, propuesta en
[`docs/design/trace-panel.md`](../design/trace-panel.md), que mostrará los controles de cada compra
real del usuario. El visor, el exportador, la proyección `evidence.json` y su esquema siguen en el
historial de Git (`git log --diff-filter=D -- ui/src/features/governance-monitor`).

## Fuentes

| Archivo en `sources/` | Contenido |
| --- | --- |
| `agent-received.json` | HP, creación observada. Un POST D17 histórico, con creación y recuperación `found`. No trae veredicto D1 textual, así que no prueba un «permitido». |
| `agent-lost.json` | HP, respuesta perdida. Timeout inicial, creación incierta y recuperación `found` posterior, con un solo POST y sin reenvío. |
| `http-deny.json` | HP, bloqueo D1. Q permite en `shadow`, D1 deniega en `enforce`; cero POST y cero efectos. |
| `hnp-evidence.jsonl` | Ledger completo de HNP-01: 65 entradas encadenadas por digest (54 registros de revisión y 11 recibos de efecto simulado). Es el `evidence.jsonl` original con otro nombre. |
| `hnp-false-accept-selected.json` | El resultado HNP-01 de la condición B, `validator-false-accept`, extraído del `results.json` original. El validador acepta y da por bueno el presupuesto, pero el backend deniega (`BACKEND_DENIED`, 13000 > 12500) y no hay efecto. |
| `hnp02/hnp02-dry_run-20261020T044329Z-s20261022/` | Corrida HNP-02 fase 1 en dry-run (`summary.json` y `traces.jsonl`): brazo A con payload guionado y controles backend, brazo B con lo mismo más un validador LLM, sobre un sink ficticio. FakeTransport, semilla 20261022, 19 casos × 2 repeticiones × 2 brazos = 76 episodios, 36 llamadas simuladas y 0 al proveedor. |

Las tres fuentes HP vienen del candidato documental v2 cerrado
([hp-b2-backend-validation-2026-10-17-v2](../archive/b2-governance/hp-b2-backend-validation-2026-10-17-v2/)),
ejecutado sobre Agent `83510f8dd5fbe5026d909df2b4949802b8b2aa1a`. Las de HNP-01 vienen del
programa [HNP-01 a633a04](https://github.com/Maldinho18/tesis-commerce-agent/commit/a633a043337286406f9a3e859809556ce46bcba1).

Junto a `sources/` quedan:

- [hnp-01-inventory.md](hnp-01-inventory.md) y [hnp-shared-imports.json](hnp-shared-imports.json):
  el inventario de HNP-01 del 18 de octubre y la comparación de hashes de los módulos que compartía
  con `main`;
- [CHANGELOG.md](CHANGELOG.md): la bitácora de cada corte del monitor.

## Hashes

[provenance.json](provenance.json) fija los SHA-256:

- `sourcePins`: las tres fuentes HP, el ledger (`evidence.jsonl`) y el `results.json` y el
  `summary.json` originales de HNP-01, que no se versionan;
- `hnp02Runs`: `summary.json` y `traces.jsonl` de la corrida HNP-02;
- `projection_sha256`: la proyección `evidence.json` del visor retirado. Se comprueba desde el
  historial con:

  ```sh
  git show 082cb45:ui/src/features/governance-monitor/evidence.json | shasum -a 256
  ```

`tests/contracts/test_governance_evidence_hashes.py` comprueba en cada `uv run pytest` que cada
archivo de `sources/` coincide con su hash. También fija el SHA-256 de `provenance.json`, así que
no se puede cambiar una fuente y su hash a la vez. `hnp-false-accept-selected.json` no tiene hash
propio: el test lo liga al ledger fijado por el digest de su registro. El docstring del test
detalla qué campos quedan fuera.

Algunas fuentes conservan su CRLF original. Por eso el `.gitattributes` de la raíz marca
`sources/**` como `-text`. Los hashes prueban consistencia de bytes, no firma del productor ni
consentimiento humano.

## Límites

- **PARTIAL.** Siguen abiertos I10, D08 y D09, las limitaciones ASGI y de identidad sintética, las
  de recuperación histórica y **AUDIT-MERCHANT-CANCEL-01**: dos ejecuciones de cancelación
  conservadas y el gate histórico sin aprobar. Esta evidencia no los cierra.
- **HNP-01 es guionado.** Corre offline con roles programados y un sink ficticio. No ejecuta el PDP
  D1, no conecta negocio y no representa dos LLM independientes.
- **HNP-02 es dry-run.** Las cifras salen de FakeTransport y no miden ningún modelo. Una corrida con
  proveedor requiere oráculos preregistrados, roles independientes y tope de costo; ver
  [evaluation/src/commerce_evaluation/hnp/llm/README.md](../../evaluation/src/commerce_evaluation/hnp/llm/README.md).

## Historia

- **17 de octubre de 2026:** se cierra la evidencia HP del candidato documental v2 sobre Agent
  `83510f8`.
- **18 de octubre:** MON-02A/B en la rama `codex/hnp-ui-readonly-monitor`. Es un monitor de
  reproducción con cuatro recorridos (creación observada, bloqueo D1, accept erróneo contenido y
  respuesta perdida) y un GET local de fixtures. Se validó en Windows PowerShell 5.1 con Node
  24.15.0, con 69 tests. Ese día también se hizo el inventario de HNP-01.
- **19 de octubre:** se publica con un manifiesto `PUBLICATION-SHA256SUMS` y atributos `-text` por
  directorio y se integra en #14. Antes de la fusión, `/governance` queda detrás de
  `VITE_GOVERNANCE_MONITOR=true` y de la sesión de Better Auth.
- **20 de octubre:** el contrato `governance-monitor/3` añade la corrida HNP-02 en dry-run (#18) y
  llega a 110 tests (87 de Node y 23 de Python). El lanzador pasa a Node y se retira el manifiesto,
  porque Git ya fija los bytes y `provenance.json` guarda los hashes. Ese mismo día se retira el
  visor: las fuentes, `provenance.json`, `hnp-01-inventory.md` y `hnp-shared-imports.json` quedan
  sin cambios, este README se reescribe y la bitácora suma la entrada del retiro.
