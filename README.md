# Tesis Commerce Agent

Buyer Agent experimental para la tesis. Este repositorio consume un ecommerce externo mediante
ACP 2026-04-17; no contiene catálogo, checkout, inventario ni tablas privadas del merchant.

## Estado actual

La primera base incluye:

- configuración segura del endpoint merchant;
- modelos estrictos para discovery y operaciones P0 de checkout;
- cliente HTTP ACP para discovery, create, get, update y cancel;
- lector del Product Feed estático con validación ACP completa y límites de recursos;
- proyección de variantes a candidatos de búsqueda, marcando precio y disponibilidad como observados;
- normalización de errores del perfil P0 actual;
- puertos neutrales para que el workflow no dependa de HTTP ni del merchant;
- estado explícito que reserva la confirmación humana antes de completar la compra.

`/complete` está deliberadamente ausente hasta implementar y probar la barrera de aprobación
humana. El modelo nunca recibe el Bearer, claves de idempotencia, tokens de pago o autoridad.

## Desarrollo

```sh
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
uv build
```

Para configurar el merchant local:

```sh
cp -n .env.example .env
```

El Bearer sintético debe emitirse desde el CLI local del merchant y mantenerse fuera de Git, logs,
prompts y artefactos.

## Product Feed P0

El merchant exporta un snapshot completo compuesto por `metadata.json` y `products.jsonl`. El
agente lo carga desde un directorio configurado por el backend:

```python
from pathlib import Path

from commerce_agent.acp import load_product_feed

feed = load_product_feed(Path("artifacts/feed/p0"))
for candidate in feed.available_candidates:
    print(candidate.variant_id, candidate.observed_unit_amount_minor)
```

El loader valida cada documento contra `schema.feed.json`, limita tamaño y número de registros, y
rechaza IDs duplicados. Los precios y la disponibilidad del feed son observaciones para búsqueda y
ranking; el checkout del merchant vuelve a calcular los términos autoritativos.

## Próximos incrementos

1. validar respuestas de checkout contra los bundles ACP congelados;
2. conectar el workflow de intención, búsqueda y selección al feed;
3. añadir la superficie determinista de confirmación humana;
4. habilitar `/complete` únicamente detrás de esa aprobación;
5. verificar y deduplicar webhooks de orden;
6. ejecutar pruebas end-to-end contra `tesis-commerce-merchant`.
