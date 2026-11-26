#!/usr/bin/env bash
# Local gate: the checks every change must pass before it is merged. No network service, no model.
# Requires `uv sync --frozen --all-groups` and `npm ci` in auth/ and ui/.
set -euo pipefail

cd "$(dirname "$0")/.."

# Each step shows the seconds elapsed since the gate started.
step() {
  printf '\n==> [%ss] %s\n' "$SECONDS" "$*"
}

step "uv lock --check"
uv lock --check

step "ruff format --check"
uv run ruff format --check .

step "ruff check"
uv run ruff check .

step "pyright"
uv run pyright

step "uv build: the wheel carries only commerce_agent"
dist="$(mktemp -d)"
trap 'rm -rf "$dist"' EXIT
uv build --wheel --quiet --out-dir "$dist"
wheel="$(uv run python -m zipfile -l "$dist"/*.whl)"
if grep -q "commerce_evaluation/" <<<"$wheel"; then
  echo "commerce_evaluation se coló en el wheel del producto" >&2
  exit 1
fi

step "pytest with coverage"
uv run pytest -q --cov --cov-report=

# The product's floor, a few points under its coverage: it catches a real drop without making
# small changes noisy. The evaluation package is reported but has no floor.
step "product coverage >= 85 %"
product="$(uv run coverage report --include='src/*' --format=total --fail-under=85)"
printf 'producto %s %%, total %s %%\n' "$product" "$(uv run coverage report --format=total)"

step "auth: npm test"
(cd auth && npm test)

step "ui: npm run lint && npm run build"
(cd ui && npm run lint && npm run build)

printf '\nCompuerta local en verde en %s s.\n' "$SECONDS"
