# Tesis Commerce Agent

## Scope

This repository contains only the buyer agent. The merchant is an external ACP service and must
never be imported as a Python package or accessed through its database.

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

