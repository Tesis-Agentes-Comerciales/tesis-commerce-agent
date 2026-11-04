"""Publishable Playwright traces: preserve UI evidence, remove session credentials."""

import hashlib
import json
import re
from contextlib import suppress
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from commerce_evaluation.redaction import redact as redact_text

REDACTED = "[REDACTED]"
SENSITIVE = re.compile(
    r"(?i)^(authorization|cookie|set-cookie|cookies|password|access_token|refresh_token|"
    r"token|checkout_receipt_jwt|payment_receipt_jwt|checkout_jwt|client_secret)$"
)


def masked(value: Any) -> Any:
    """Keep HAR container types so the sanitized trace remains readable by Playwright."""
    if isinstance(value, list):
        return [masked(item) for item in value]
    if isinstance(value, dict):
        return {key: masked(item) for key, item in value.items()}
    return REDACTED if isinstance(value, str) else value


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value, marker=REDACTED)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: masked(item) if SENSITIVE.fullmatch(key) else redact(item)
        for key, item in value.items()
    }
    # HAR headers use name/value pairs; Playwright records password fills in action params.
    if SENSITIVE.fullmatch(str(value.get("name", ""))):
        result["value"] = REDACTED
    params = value.get("params")
    if isinstance(params, dict) and re.search(
        r"(?i)password|contrase", str(params.get("selector", ""))
    ):
        result["params"] = {
            key: REDACTED if key in {"value", "text"} else redact(item)
            for key, item in params.items()
        }
    return result


def credentials(value: Any) -> set[str]:
    """Also scrub copies in snapshots and encoded bodies, not only the original field."""
    found: set[str] = set()
    if isinstance(value, list):
        for item in value:
            found.update(credentials(item))
    if not isinstance(value, dict):
        return found
    for key, item in value.items():
        if SENSITIVE.fullmatch(key) and isinstance(item, str) and item:
            found.add(item)
        if key.lower() == "cookies" and isinstance(item, list):
            found.update(
                cookie["value"]
                for cookie in item
                if isinstance(cookie, dict) and isinstance(cookie.get("value"), str)
            )
        found.update(credentials(item))
    if SENSITIVE.fullmatch(str(value.get("name", ""))) and isinstance(value.get("value"), str):
        found.add(value["value"])
    params = value.get("params")
    if isinstance(params, dict) and re.search(
        r"(?i)password|contrase", str(params.get("selector", ""))
    ):
        found.update(str(params[key]) for key in ("value", "text") if params.get(key))
    return found


def sanitize_trace(raw: Path, target: Path) -> dict[str, str | int]:
    """Raw stays local until replaced; every textual resource is sanitized, not just headers."""
    changed = 0
    with ZipFile(raw) as source, ZipFile(target, "w", ZIP_DEFLATED) as output:
        secrets: set[str] = set()
        for entry in source.infolist():
            try:
                for line in source.read(entry).decode("utf-8").splitlines():
                    with suppress(json.JSONDecodeError):
                        secrets.update(credentials(json.loads(line)))
            except UnicodeDecodeError:
                pass
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
            text = redact_text("".join(lines), secrets, marker=REDACTED)
            sanitized = text.encode("utf-8")
            changed += sanitized != data
            output.writestr(entry, sanitized)
    return {
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "redacted_resources": changed,
    }
