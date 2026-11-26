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

`slow` lo pone cada prueba viva que tarda minutos: hoy `test_live_07bb_guarded_d1.py` y una
regresión del Merchant en `test_live_06dc_creation_recovery.py`.

## Capas

| Capa | Qué verifica | Dónde | Cuándo | En local |
| --- | --- | --- | --- | --- |
| 1. Unitarias y contratos | Dominio, aplicación, gobernanza, persistencia, ACP/AP2 congelados, hashes de la evidencia histórica de gobernanza, frontera del repositorio | `tests/` sin `integration/` | Compuerta local | `uv run pytest` |
| 1. Autenticación | Registro, sesión y JWT verificable con JWKS contra el servidor real | `auth/test/` | Compuerta local | `cd auth && npm ci && npm test` |
| 2. Merchant + PostgreSQL | Persistencia y ACP HTTP del Merchant | Repositorio del Merchant | En el repositorio del Merchant | Ver su `docs/testing.md` |
| 3. UI | Lint, build, historias de Storybook con interacción y axe en Chromium, y pruebas unitarias de Vitest en Node (`src/**/*.test.ts`, dentro de `test-storybook`) | `ui/` | Lint y build en la compuerta; el resto al tocar `ui/` | `cd ui && npm ci && npm run lint && npm test && npm run build && npm run test-storybook` |
| 3. UI E2E | Recorridos de chat y de delegaciones en el navegador con Playwright, sobre el build y con backends simulados (las delegaciones reproducen secuencias reales de la API) | `ui/e2e/` | Al tocar `ui/` | `cd ui && npm run e2e` |
| 4. Sistema | Agent ↔ Merchant ↔ proveedor de pagos con PostgreSQL desechable; gobernanza con fallos inyectados, compras sin gobernanza y por ACP, compras delegadas con y sin gobernanza, y recorridos grabados de una persona en el navegador (compra sin gobernanza, compra gobernada y delegaciones, también en teléfono) | `tests/integration/` | A mano, con `RUN_LIVE_STACK=1` | [`tests/integration/README.md`](../tests/integration/README.md) |
| 5. Evaluación | Sin red ni modelo: `verify-evidence` sobre bases SQLite de corridas con el código del producto (con la persona presente y en compras delegadas), el arranque de `demo-stack`, el bloque `flow_trace`, los escenarios de la evaluación, el puerto LLM (sus dobles, el tope de gasto con su registro de uso, y el transporte de Anthropic y su Batch contra el SDK con un `MockTransport`), y los bytes de los resultados archivados | `tests/evaluation/` | Compuerta local, dentro de `pytest` | `uv run pytest -m evaluation` |

Las corridas reales de evaluación (modelo pagado) nunca corren en la compuerta: las lanza el dueño
a mano, con su tope de gasto. Los corredores de los pilotos de gobernanza de octubre salieron de
`main`: se reproducen desde la etiqueta `pilotos-octubre`, y sus datos y resultados siguen en
`evaluation/`.

Selecciones útiles:

```sh
uv run pytest -m "not evaluation"     # solo producto
uv run pytest -m evaluation           # solo evaluación (dry-run)
uv run pytest --cov --cov-report=term # con cobertura, como la compuerta
```

## Variables

| Variable | Uso |
| --- | --- |
| `RUN_LIVE_STACK` | `1` activa la capa 4. Si falta un requisito, la suite falla con la lista; no omite en silencio. |
| `MERCHANT_REPO`, `PAYMENT_REPO` | Checkouts del Merchant y del proveedor de pagos para la capa 4. |
| `RUN_LIVE_LLM` | Manual: pone Claude (`ANTHROPIC_API_KEY` exportada) detrás de la prueba conversacional y de la compra sin gobernanza en el navegador de la capa 4; los demás recorridos siempre usan el router guionado. Gasta dinero. |
| `HNP_LIVE` | Solo para los corredores de los pilotos de octubre, en la etiqueta `pilotos-octubre`: habilita sus llamadas reales. En `main` no activa nada, y las herramientas de evaluación se niegan a correr si lo ven, igual que con `RUN_LIVE_LLM`. |

No hay otras activaciones: las suites vivas anteriores se retiraron y su cobertura es ahora la
capa 4 con `RUN_LIVE_STACK`. La correspondencia prueba por
prueba está en las secciones de equivalencia de [`tests/integration/README.md`](../tests/integration/README.md).

## Compuerta local

No hay CI alojado: cada cambio pasa por `scripts/check.sh` antes de fusionarse. Necesita
`uv sync --frozen --all-groups` y `npm ci` en `auth/` y `ui/`, no usa servicios externos ni el
modelo, y tarda unos cuatro minutos. En orden:

1. `uv lock --check`: el lock coincide con `pyproject.toml`.
2. `ruff format --check`, `ruff check` y `pyright`.
3. `uv build`: falla si `commerce_evaluation` aparece dentro del wheel del producto.
4. `pytest --cov` (capas 1 y 5; la 4 se recoge y se omite) y el piso de cobertura del producto.
5. `npm test` en `auth/`.
6. `npm run lint` y `npm run build` en `ui/`.

Cada paso muestra los segundos transcurridos. Al tocar `ui/` se corren además
`npm run test-storybook` y `npm run e2e`, que necesitan el Chromium de Playwright
(`npx --no-install playwright install chromium`, la versión de `ui/package-lock.json`). La capa 4
corre a mano (ver [`tests/integration/README.md`](../tests/integration/README.md)); los videos y
las capturas por paso de sus recorridos quedan en `.local/browser-journeys/` de quien la corre.

## Cobertura

Se mide con `pytest-cov` sobre `commerce_agent` y `commerce_evaluation`, con ramas
(`[tool.coverage.*]` en `pyproject.toml`). El producto (`src/`) tiene **92.9 %** (líneas 95.0 %,
ramas 84.0 %).

| Paquete | Cobertura |
| --- | --- |
| `commerce_agent.domain` | 97.3 % |
| `commerce_agent.delegation` | 97.0 % |
| `commerce_agent.llm` | 96.7 % |
| `commerce_agent.trace` | 95.1 % |
| `commerce_agent.application` | 93.8 % |
| `commerce_agent.governance` | 92.4 % |
| `commerce_agent.persistence` | 92.4 % |
| `commerce_agent` (módulos raíz: API, runtime, grafo, aprobación, pagos) | 91.3 % |
| `commerce_agent.acp` | 89.4 % |
| `commerce_agent.webhooks` | 86.3 % |
| `commerce_evaluation` | 78.1 % |

La compuerta exige 85 % solo al producto (`coverage report --include="src/*" --fail-under=85`),
unos puntos por debajo de su cobertura: atrapa una caída real sin volver ruidosos los cambios
pequeños. `commerce_evaluation` se reporta pero no tiene piso. La capa 4 no cuenta para estas
cifras: sus servicios corren en otros procesos.

## Tiempos de referencia

En una Mac de desarrollo con Docker Desktop:

| Selección | Pruebas | Tiempo |
| --- | --- | --- |
| `scripts/check.sh` completo | Todo lo de la fila siguiente, más `auth/` y `ui/` | ~4 min |
| `uv run pytest --cov` (capas 1 y 5) | 4644: 4376 pasan y 268 se omiten (la capa 4) | ~3,5 min |

La capa 4 tiene 268 pruebas: 257 sin `slow`, 16 en `tests/integration/test_live_hnp.py` y 7
recorridos del navegador (`-k browser`). Tarda bastante más que la compuerta, y cuánto depende de
Docker y de la máquina. Los conteos salen de `uv run pytest --collect-only -q` con la selección de
cada fila.

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
