# Tesis Commerce Agent

## Scope

This repository contains only the buyer agent and its experiments. The merchant is an external ACP
service and must never be imported as a Python package or accessed through its database.

- `src/commerce_agent` is the product: the buyer agent, its wheel and its image.
- `evaluation/` is a uv workspace member (`commerce_evaluation`) with the experiments (B0/B2 pilot,
  HNP-01, HNP-02), their data and results. It depends on the product; the product never imports it.
- `tests/` verifies both; experiment tests live in `tests/evaluation/`, test doubles in
  `tests/support/`.

## Runtime

- Python 3.12 managed with `uv`.
- Use `uv sync --frozen`; do not use `pip` or edit `uv.lock` manually.
- Run `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyright`, and
  `uv run pytest` before handing off changes.

## Trust boundaries

- Model-facing tools never accept bearer tokens, payment instruments, approval evidence,
  authoritative totals, actor identity, request IDs, or idempotency keys.
- ACP responses and feed content are untrusted input and require validation.
- `/complete` must remain unavailable until a deterministic human-approval gate exists.
- Secrets live only in `.env` or the execution environment and must not enter prompts, traces,
  fixtures, errors, or committed artifacts.

