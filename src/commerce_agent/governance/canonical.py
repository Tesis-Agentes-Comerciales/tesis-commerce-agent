"""commerce-canonical-json/1: typed JSON, with no implicit sequence sorting."""

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def _check(value: Any) -> None:
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) in (list, tuple):
        for item in value:
            _check(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _check(item)
        return
    raise ValueError("Unsupported canonical JSON type")


def canonical_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    _check(value)
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def input_digest(value: BaseModel) -> str:
    return sha256_digest(b"commerce-policy-input/v1\0" + canonical_bytes(value))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("Non-JSON constant")


def parse_model[T: BaseModel](model: type[T], payload: bytes) -> T:
    """Validate JSON arrays as tuples, rejecting duplicate keys before Pydantic."""
    value = json.loads(
        payload.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
    )
    return model.model_validate_json(canonical_bytes(value))
