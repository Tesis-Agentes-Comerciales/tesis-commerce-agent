# Tesis Commerce Agent

## Scope

This repository contains only the buyer agent and its experiments. The merchant is an external ACP
service and must never be imported as a Python package or accessed through its database.

- `src/` is the product (`commerce_agent`): the only package in the wheel and the image.
- `evaluation/` is the experiments: the uv workspace member `commerce_evaluation` (B0/B2 pilot,
  HNP-01, HNP-02) with its data, results and scripts. It depends on the product; the product never
  imports it.
- `tests/` verifies both: experiment tests in `tests/evaluation/`, test doubles in
  `tests/support/`, the live system suite in `tests/integration/`. The directory sets the layer.

## Workflow

Non-trivial changes follow `docs/workflow.md`: approved spec in `docs/design/`, contract and
fixtures, Storybook-first UI, test-first backend, integration, one topic per PR.

## Runtime

- Python 3.12 managed with `uv`; Node.js 24 for `auth/` and `ui/`.
- Use `uv sync --frozen --all-groups`; do not use `pip` or edit `uv.lock` manually.

## Verification

`docs/testing.md` is the source of truth. Before handing off, run the layers you touched:

- Python (layers 1 and 5): `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run pyright`, `uv run pytest`.
- Evaluation entrypoints: `uv run hnp-matrix --dry-run --env-file .env.example`,
  `uv run governance-pilot --dry-run`.
- Auth: `cd auth && npm ci && npm test`.
- UI: `cd ui && npm ci && npm run lint && npm run build && npm run test-storybook && npm run e2e`.
- Layer 4 (`tests/integration`, `RUN_LIVE_STACK=1`) runs in `nightly.yml` or by hand; see
  `tests/integration/README.md`.

## Never

- Call a real LLM from tests, CI or your own checks: no `HNP_LIVE=1`, no `RUN_LIVE_LLM=1`, no
  `hnp-matrix` or `governance-pilot` without `--dry-run`, no `evaluate-conversation`. Paid runs
  are started by hand by the user; HNP-02 refuses to call a provider without `HNP_MAX_COST_USD`.
- Add opt-in suites or switches to `ci.yml`: layer 4 runs only in `nightly.yml`, no workflow sets
  `RUN_LIVE_LLM` or `HNP_LIVE`, and live tests go in `tests/integration/`, not behind a new variable.

## Trust boundaries

- Model-facing tools never accept bearer tokens, payment instruments, approval evidence,
  authoritative totals, actor identity, request IDs, or idempotency keys.
- ACP responses and feed content are untrusted input and require validation.
- Completing a purchase requires a deterministic authorization gate. Human Present: `/complete`
  stays behind the authenticated buyer's explicit approval click. Human Not Present: the gate is
  prior consent to a typed scope plus D2-HNP/D3-HNP and the single-use reservation, and no public
  route completes a purchase without that chain. The only exception is the B0-HNP evaluation
  baseline (`AGENT_GOVERNANCE_MODE=off`, sandbox only), which skips D2-HNP/D3-HNP but keeps the
  prior consent, the single-use reservation, revocation and the kill switch.
- Secrets live only in `.env` or the execution environment and must not enter prompts, traces,
  fixtures, errors, or committed artifacts.
