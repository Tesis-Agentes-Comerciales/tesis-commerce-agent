"""Canonical JSON of Merchant payloads, shared by `acp` and `governance` without a cycle.

It imports neither package: `acp.offer_snapshot` and the governance digests both need it, and
governance imports `acp`.
"""

import json

_SENSITIVE = {
    "authorization",
    "bearer",
    "token",
    "bearer_token",
    "access_token",
    "refresh_token",
    "session_token",
    "idempotency_key",
    "credentials",
    "credential",
    "payment_data",
    "checkout_jwt",
}


def canonical_original_response(value: object) -> bytes:
    """Exact Merchant c5bd811 profile, including safe integers and sensitive keys."""

    def check(item: object) -> None:
        if item is None or isinstance(item, (str, bool)):
            return
        if type(item) is int and abs(item) <= 9007199254740991:
            return
        if isinstance(item, list):
            for child in item:
                check(child)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            if any(key.lower() in _SENSITIVE for key in item):
                raise ValueError("Unsupported response fields")
            for child in item.values():
                check(child)
            return
        raise ValueError("Unsupported response value")

    check(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
