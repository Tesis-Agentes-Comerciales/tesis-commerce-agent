"""Publishable Playwright traces: preserve UI evidence, remove session credentials."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

REDACTED = "[REDACTED]"
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
BEARER = re.compile(r"(?i)Bearer\s+[^\s\"<>]+")
SENSITIVE = re.compile(
    r"(?i)^(authorization|cookie|set-cookie|cookies|password|access_token|refresh_token|"
    r"token|checkout_receipt_jwt|payment_receipt_jwt|checkout_jwt|client_secret)$"
)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return BEARER.sub(REDACTED, JWT.sub(REDACTED, value))
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: REDACTED if SENSITIVE.fullmatch(key) else redact(item) for key, item in value.items()
    }
    # HAR headers use name/value pairs; Playwright records password fills in action params.
    if SENSITIVE.fullmatch(str(value.get("name", ""))):
        result["value"] = REDACTED
    params = value.get("params")
    if isinstance(params, dict) and re.search(
        r"(?i)password|contrase", str(params.get("selector", ""))
    ):
        result["params"] = {
            key: REDACTED if key in {"value", "text"} else item
            for key, item in result["params"].items()
        }
    return result


def sanitize_trace(raw: Path, target: Path) -> dict[str, str | int]:
    """Raw stays local until replaced; every textual resource is sanitized, not just headers."""
    changed = 0
    with ZipFile(raw) as source, ZipFile(target, "w", ZIP_DEFLATED) as output:
        for entry in source.infolist():
            data = source.read(entry)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                output.writestr(entry, data)
                continue
            lines = []
            for line in text.splitlines(keepends=True):
                try:
                    lines.append(json.dumps(redact(json.loads(line)), ensure_ascii=False) + "\n")
                except (json.JSONDecodeError, TypeError):
                    lines.append(redact(line))
            sanitized = "".join(lines).encode("utf-8")
            changed += sanitized != data
            output.writestr(entry, sanitized)
    return {
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "redacted_resources": changed,
    }
