# Estrategia de pruebas

Qué se prueba en el Agent, dónde vive cada prueba, cuándo corre y cómo ejecutarla en local. El
Merchant tiene su propio `docs/testing.md` con las capas 1 y 2 de su lado.

## Regla de ubicación

| Carpeta | Contiene |
| --- | --- |
| `src/` | El producto (`commerce_agent`). Es lo único que entra al wheel. |
| `evaluation/` | Los experimentos (`commerce_evaluation`, miembro del workspace de uv). |
| `tests/` | Las pruebas de ambos. El soporte compartido entre pruebas va en `tests/support/`. |
| `auth/test/`, `ui/` | Las pruebas del servicio de autenticación y de la UI, junto a su código. |

La carpeta decide la capa. `tests/conftest.py` marca por ruta, así que una prueba nueva queda
clasificada por dónde se crea, sin `pytestmark` ni `skipif` propios:

| Ruta | Marker | Efecto |
| --- | --- | --- |
| `tests/integration/` | `live` | Se omite salvo con `RUN_LIVE_STACK=1`. |
| `tests/evaluation/` | `evaluation` | Corre siempre, sin red ni modelo. |
| El resto de `tests/` | — | Unitarias y de contrato. |

`slow` lo pone cada módulo vivo que tarda minutos (hoy 07B-B y la regresión del Merchant en
06D-C).

## Capas

| Capa | Qué verifica | Dónde | Cuándo | En local |
| --- | --- | --- | --- | --- |
| 1. Unitarias y contratos | Dominio, aplicación, gobernanza, persistencia, ACP/AP2 congelados, hashes de la evidencia histórica de gobernanza, frontera del repositorio | `tests/` sin `integration/` | Cada PR (`python-tests`) | `uv run pytest` |
| 1. Autenticación | Registro, sesión y JWT verificable con JWKS contra el servidor real | `auth/test/` | Cada PR (`auth`) | `cd auth && npm ci && npm test` |
| 2. Merchant + PostgreSQL | Persistencia, D17, ACP HTTP del Merchant | Repositorio del Merchant | Cada PR del Merchant | Ver su `docs/testing.md` |
| 3. UI | Lint, build, historias de Storybook con interacción y axe en Chromium | `ui/` | Cada PR (`ui`) | `cd ui && npm ci && npm run lint && npm run build && npm run test-storybook` |
| 4. Sistema | Agent ↔ Merchant ↔ proveedor de pagos con PostgreSQL desechable; gobernanza con fallos inyectados, compras B0 y ACP, compra B0 desde el navegador | `tests/integration/` | Cada noche y a pedido (`nightly.yml`) | [`tests/integration/README.md`](../tests/integration/README.md) |
| 3. UI E2E | Recorridos de chat en el navegador con Playwright, sobre el build y con backends simulados | `ui/e2e/` | Cada PR (`ui`) | `cd ui && npm run e2e` |
| 5. Evaluación | Experimento HNP y piloto de gobernanza sin red ni modelo (dry-run): forma de resultados, costos, trazas, guardas de gasto | `tests/evaluation/` y entrypoints | Cada PR, dentro de `python-tests` | `uv run pytest -m evaluation`, `uv run hnp-matrix --dry-run --env-file .env.example`, `uv run governance-pilot --dry-run` |

Las corridas reales de evaluación (`HNP_LIVE=1`, modelo pagado) nunca corren en CI: se lanzan a
mano con los entrypoints de `commerce_evaluation` y sus guardas de costo.

Selecciones útiles:

```sh
uv run pytest -m "not evaluation"     # solo producto
uv run pytest -m evaluation           # solo evaluación (dry-run)
uv run pytest --cov --cov-report=term # con cobertura, como el CI
```

## Variables

| Variable | Uso |
| --- | --- |
| `RUN_LIVE_STACK` | `1` activa la capa 4. Si falta un requisito, la suite falla con la lista; no omite en silencio. |
| `MERCHANT_REPO`, `PAYMENT_REPO` | Checkouts del Merchant y del proveedor de pagos para la capa 4. |
| `RUN_LIVE_LLM` | Manual: pone el modelo de OpenAI del entorno detrás de las pruebas conversacional y de navegador de la capa 4. Gasta dinero. |
| `HNP_LIVE` | Manual: habilita llamadas reales en la evaluación HNP; además exige tope de costo. |

No hay otras activaciones: las del E2E conjunto de septiembre y las de las suites live por ticket
se retiraron y su cobertura es ahora la capa 4 con `RUN_LIVE_STACK`. La correspondencia prueba por
prueba está en las secciones de equivalencia de [`tests/integration/README.md`](../tests/integration/README.md).

## Workflows

`ci.yml` corre en cada PR y en `main`:

- `python-lint`: `ruff format --check`, `ruff check`, `pyright` y `uv build`; falla si
  `commerce_evaluation` aparece dentro del wheel.
- `python-tests`: `uv run pytest --cov` (capas 1 y 5; la 4 se recoge y se omite), el umbral de
  cobertura del producto, los dry-run de `hnp-matrix` y `governance-pilot` (sin red; escriben en
  `.local/`) y un resumen de cobertura en la página del job.
- `auth`: `npm ci` y `npm test` en `auth/`.
- `ui`: `npm ci`, lint, build, `build-storybook`, `test-storybook` y `npm run e2e` en Chromium.
  Playwright sale de `ui/package-lock.json` (`npx --no-install`), nunca de una versión sin fijar.

`nightly.yml` corre a las 06:23 UTC y con `workflow_dispatch`, donde se puede elegir la rama o
commit del Merchant (`merchant_ref`) y del proveedor (`payment_ref`). Su job `live-stack` clona
el Merchant y el proveedor, instala los tres proyectos, `auth/`, `ui/` y el Chromium de
Playwright, corre `tests/integration` completo y sube la evidencia (JSONL, logs, captura del
navegador y JUnit) como artefacto por 14 días.

El Merchant y el proveedor son privados: `live-stack` necesita el secreto
`MERCHANT_REPO_TOKEN`, un token de solo lectura (`contents: read`) sobre
`Tesis-Agentes-Comerciales/tesis-commerce-merchant` y
`Tesis-Agentes-Comerciales/tesis-payment-provider-sandbox`. Sin él, el job se omite con un
aviso visible en la ejecución.

## Cobertura

Se mide con `pytest-cov` sobre `commerce_agent` y `commerce_evaluation`, con ramas
(`[tool.coverage.*]` en `pyproject.toml`). Línea base del 2026-10-20: el producto (`src/`)
tiene **89.1 %** (líneas 92.2 %, ramas 77.4 %).

| Paquete | Cobertura |
| --- | --- |
| `commerce_agent.domain` | 96.1 % |
| `commerce_agent.governance` | 91.9 % |
| `commerce_agent.persistence` | 90.5 % |
| `commerce_agent.application` | 88.8 % |
| `commerce_agent.llm` | 87.1 % |
| `commerce_agent.webhooks` | 86.3 % |
| `commerce_agent` (módulos raíz: API, runtime, grafo, aprobación, pagos) | 85.5 % |
| `commerce_agent.acp` | 83.8 % |
| `commerce_evaluation` | 75.6 % (el experimento HNP sobre 90 %; los runners del piloto y de conversación casi sin cubrir) |

El umbral del CI es 85 % y aplica solo al producto (`coverage report --include="src/*"`), cuatro
puntos por debajo de la línea base: atrapa una caída real sin volver ruidosos los PR pequeños.
`commerce_evaluation` se reporta en el resumen del job pero no tiene umbral. La capa 4 no cuenta
para estas cifras: sus servicios corren en otros procesos.

## Tiempos de referencia

En macOS con OrbStack, 2026-10-20:

| Selección | Pruebas | Tiempo |
| --- | --- | --- |
| `uv run pytest` (capas 1 y 5) | ~1620: ~1560 pasan y 66 se omiten | ~1 min |
| `tests/integration` completa | 66 | ~12 min |
| `tests/integration -m "not slow"` | 55 | ~2 min |

Los conteos salen de `uv run pytest --collect-only -q` con la selección de cada fila.

## Al escribir una prueba

- Escribir solo en `tmp_path` o en el directorio de la ejecución, nunca en el checkout.
- No comparar contra la fecha o la hora reales sin tolerar el cambio de día; preferir relojes
  inyectados. No medir tiempos con umbrales absolutos.
- Fijar hashes o bytes solo de contratos congelados (`tests/contracts/`); para documentos
  generados, comparar el contenido que importa.
- Una espera por bloqueo debe fallar de inmediato en la prueba (timeout corto o inyectado), no
  agotar el timeout de producción.
- Lo que necesite servicios reales va en `tests/integration/` y usa el arnés de ese directorio;
  no crear otra activación por variable de entorno.
