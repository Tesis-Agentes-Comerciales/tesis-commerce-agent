# Tesis Commerce Agent

Buyer Agent experimental para la tesis. Este repositorio consume un ecommerce externo mediante
ACP 2026-04-17; no contiene catálogo, checkout, inventario ni tablas privadas del merchant.

## Estado actual

La primera base incluye:

- configuración segura del endpoint merchant;
- modelos estrictos para discovery y operaciones P0 de checkout;
- cliente HTTP ACP para discovery, create, get, update y cancel;
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

## Próximos incrementos

1. validar respuestas contra los bundles ACP congelados;
2. ingerir el Product Feed estático;
3. conectar el workflow de intención, búsqueda y selección;
4. añadir la superficie determinista de confirmación humana;
5. habilitar `/complete` únicamente detrás de esa aprobación;
6. verificar y deduplicar webhooks de orden;
7. ejecutar pruebas end-to-end contra `tesis-commerce-merchant`.

