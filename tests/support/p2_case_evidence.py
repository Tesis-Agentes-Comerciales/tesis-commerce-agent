"""Record a live case only after its exercise and verified cleanup have finished."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class InsufficientFixtureData(RuntimeError):
    def __init__(self, role: str, matching_variants: int):
        super().__init__("insufficient_fixture_data")
        self.role = role
        self.matching_variants = matching_variants


@contextmanager
def verified_case(
    record: Callable[..., None], scenario: str, facts: dict[str, Any]
) -> Iterator[None]:
    try:
        yield
        if scenario in {"P2-PROV-02", "P2-PROV-03"} and facts.get("cleanup_verified") is not True:
            raise AssertionError("Cleanup must be independently verified before success")
    except BaseException as error:
        failure: dict[str, Any] = {
            **facts,
            "status": "failed",
            "failure_kind": "execution",
            "error_type": type(error).__name__,
        }
        if isinstance(error, InsufficientFixtureData):
            failure.update(
                status="blocked",
                failure_kind="data",
                reason="insufficient_fixture_data",
                role=error.role,
                matching_variants=error.matching_variants,
            )
        record(scenario, **failure)
        raise
    else:
        record(scenario, **{**facts, "status": "passed"})
