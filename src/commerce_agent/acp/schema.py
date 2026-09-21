import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from referencing import Registry, Resource


class ACPContractError(ValueError):
    def __init__(self, definition: str) -> None:
        super().__init__(f"Payload violates ACP 2026-04-17 {definition}")
        self.definition = definition


@lru_cache
def _bundle() -> tuple[dict[str, Any], Registry[Any]]:
    resource = files("commerce_agent.vendor.acp").joinpath(
        "2026-04-17/schema.agentic_checkout.json"
    )
    bundle: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
    registry = Registry().with_resource(str(bundle["$id"]), Resource.from_contents(bundle))
    return bundle, registry


@lru_cache
def validator(definition: str) -> Draft202012Validator:
    bundle, registry = _bundle()
    if definition not in bundle["$defs"]:
        raise ValueError(f"Unknown ACP definition: {definition}")
    return Draft202012Validator(
        {"$ref": f"{bundle['$id']}#/$defs/{definition}"},
        registry=registry,
        format_checker=FormatChecker(),
    )


def validate_acp(definition: str, value: object) -> None:
    try:
        validator(definition).validate(value)
    except JsonSchemaValidationError as error:
        raise ACPContractError(definition) from error
