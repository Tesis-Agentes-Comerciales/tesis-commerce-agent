# Imagen de despliegue del buyer agent. uv resuelve el lock congelado y uvicorn sirve la API.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Feed estático exportado desde el merchant; `AGENT_FEED_PATH` apunta aquí.
COPY deploy ./deploy

ENV PATH="/app/.venv/bin:$PATH"
CMD ["sh", "-c", "uvicorn commerce_agent.api:app_factory --factory --host 0.0.0.0 --port ${PORT:-8080}"]
