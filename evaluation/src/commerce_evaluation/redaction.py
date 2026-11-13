"""Redaction of what the evaluation runners keep or print (docs/design/run-traceability.md §4.2).

Two passes over a text: every known secret value (as given, JSON-escaped and, for multi-line
keys, line by line), then the shapes of a secret whose value no runner holds: JWTs (the buyer's),
bearer tokens, PEM blocks, model API keys (`sk-…`, Anthropic's `sk-ant-…` and OpenAI's) and
PostgreSQL URLs. Only the secret is replaced, so an error message keeps what failed. It lives
apart from the runners so `demo_stack` and the pilots share it without importing each other.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ValidationError

REDACTED = "[redacted]"
# Lines of a multi-line secret (a PEM key) shorter than this are not secrets by themselves.
MIN_SECRET_LINE = 16
# A PEM block up to its END line or, cut short, to the end of the text.
PEM_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\Z)", re.DOTALL)
JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*")
MODEL_KEY = re.compile(r"\bsk-[\w-]{20,}")
DATABASE_URL = re.compile(r"postgres(?:ql)?(?:\+\w+)?://\S+")
# Only a token-like word after `Bearer`: prose such as "missing Bearer token" stays.
BEARER = re.compile(r"(Bearer\s+)[\w.~+/=-]{16,}")


def secret_forms(values: Iterable[str]) -> list[str]:
    """Every form a secret value may take in a text, longest first."""
    forms: set[str] = set()
    for value in values:
        if not value:
            continue
        forms.update({value, json.dumps(value)[1:-1]})
        forms.update(
            line
            for line in value.splitlines()
            if len(line) >= MIN_SECRET_LINE and not line.startswith("-----")
        )
    return sorted(forms, key=len, reverse=True)


def redact(text: str, values: Iterable[str] = (), *, marker: str = REDACTED) -> str:
    """The text without any of the secret values or any secret shape."""
    for form in secret_forms(values):
        text = text.replace(form, marker)
    for pattern in (PEM_BLOCK, JWT, MODEL_KEY, DATABASE_URL):
        text = pattern.sub(marker, text)
    return BEARER.sub(lambda match: match.group(1) + marker, text)


def relabel(text: str, paths: Mapping[str, str]) -> str:
    """The text with each local path replaced by its label, longest path first, so a directory
    inside another one keeps its own label."""
    for path in sorted(paths, key=len, reverse=True):
        text = text.replace(path, paths[path])
    return text


def _redacted(value: Any, values: list[str], paths: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return relabel(redact(value, values), paths)
    if isinstance(value, Mapping):
        return {key: _redacted(item, values, paths) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item, values, paths) for item in value]
    return value


def sweep_fields(
    row: Mapping[str, Any],
    names: Iterable[str],
    values: Iterable[str],
    *,
    paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The row with every string inside the named fields redacted and its local `paths` relabeled;
    other fields stay as they are."""
    secrets, labels = list(values), dict(paths or {})
    return dict(row) | {
        name: _redacted(row[name], secrets, labels) for name in names if name in row
    }


def error_text(error: BaseException) -> str:
    """`Type: message` for a row's error field. A Pydantic validation error names only where and
    why each input failed: its message quotes the input cut to a few characters at each end, a
    fragment of a secret that no sweep recognizes, by value or by shape."""
    if isinstance(error, ValidationError):
        failures = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['type']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        return f"{type(error).__name__}: {failures}"
    return f"{type(error).__name__}: {error}"


def secret_values(environment: Mapping[str, str], names: Iterable[str]) -> tuple[str, ...]:
    """The non-empty values that `environment` gives the secret variables `names`."""
    return tuple(value for name in names if (value := environment.get(name)))
