#!/usr/bin/env bash
# Local gate: the checks every change must pass before it is merged. No network service, no model.
# Requires `uv sync --frozen --all-groups` and `npm ci` in auth/ and ui/.
set -euo pipefail

cd "$(dirname "$0")/.."

step() {
  printf '\n==> %s\n' "$*"
}

step "uv lock --check"
uv lock --check

step "ruff format --check"
uv run ruff format --check .

step "ruff check"
uv run ruff check .

step "pyright"
uv run pyright

step "pytest"
uv run pytest -q

step "auth: npm test"
(cd auth && npm test)

step "ui: npm run lint && npm run build"
(cd ui && npm run lint && npm run build)

printf '\nCompuerta local en verde.\n'
