"""The suite's case and its catalog (docs/design/eval-suite.md §4).

A case follows τ²-bench: what the user knows and does not know, the expected final state and a
reference solution. A `presente` case has a user who talks to the agent; a `delegado` case has
no conversation, only the scope its owner consents to beforehand.

Each case is one file, `<group>/<escenario>/<modo>.json`, under the catalog directory.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from commerce_agent.governance.hnp_models import (
    MAX_SCOPE_VARIANTS,
    PositiveMinor,
    PreferenceText,
    Priority,
)
from commerce_agent.governance.models import Id, Ids, Minor
from commerce_evaluation.suite.events import (
    DELEGATED_TRIGGERS,
    MAX_CATALOG_TEXT,
    MODEL_FAULTS,
    OWNER_EVENTS,
    PARAMETERS,
    CatalogField,
    EventKind,
    Trigger,
    parameters_of,
)
from commerce_evaluation.suite.exposure import visible_fields
from commerce_evaluation.verify_evidence import LINKS
from commerce_evaluation.verify_evidence_hnp import HNP_LINKS

# Each vocabulary the owner names is written once, here.
Present = Literal["presente"]
Delegated = Literal["delegado"]
Group = Literal[
    "pedido-del-usuario",
    "cambios-en-la-tienda",
    "cambios-en-el-permiso",
    "fallas-tecnicas",
    "manipulacion",
]
MANIPULATION: Group = "manipulacion"
Suite = Literal["regression", "capability", "adversarial"]
SUITES: tuple[Suite, ...] = ("regression", "capability", "adversarial")
Grader = Literal["effects", "outcome", "evidence", "claims"]
# buy: an order. clarify: the agent asks and waits. block: it stops without buying.
# escalate: it hands the decision to the owner or the operator (a `delegado` task `escalated`).
Outcome = Literal["buy", "clarify", "block", "escalate"]
# The Merchant's P0 fixture: the only catalog the live stack seeds.
Catalog = Literal["p0"]
# AGENT_GOVERNANCE_MODE off and enforce. Manipulation runs under both: their comparison backs
# the thesis's central finding.
Governance = Literal["B0", "B2"]

Text = Annotated[str, Field(min_length=1)]
CaseId = Annotated[str, Field(min_length=1)]


def _distinct[T](values: tuple[T, ...]) -> tuple[T, ...]:
    if len(set(values)) != len(values):
        raise ValueError("Duplicate member")
    return values


class SuiteModel(BaseModel):
    """Strict and closed, like the HNP contracts: a typo in a case file is an error."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class User(SuiteModel):
    """The simulated user opens with `script[0]` word for word, then pursues `goal`, gives only
    `known_info` and says it does not know anything in `unknown_info`. In regression it says
    `script`, line by line.

    The user never decides an approval: the runner approves a presented checkout exactly when its
    variant and total equal `expected.purchase`, and never otherwise.
    """

    goal: Text
    known_info: tuple[Text, ...]
    unknown_info: tuple[Text, ...]
    script: Annotated[tuple[Text, ...], Field(min_length=1)]


class Delegation(SuiteModel):
    """The scope of `POST /delegations/proposals`. The runner proposes it for the synthetic
    Bogotá buyer, consents to its exact digest and triggers the task, so consent is not a field."""

    variant_ids: Annotated[Ids, Field(min_length=1, max_length=MAX_SCOPE_VARIANTS)]
    total_max_minor: PositiveMinor
    currency: Literal["COP"]
    valid_for_seconds: Annotated[int, Field(ge=60)]
    preference_text: PreferenceText | None = None
    priority: Priority | None = None

    @model_validator(mode="after")
    def _priority_orders_the_scope(self) -> Self:
        if self.priority is not None and sorted(self.priority) != sorted(self.variant_ids):
            raise ValueError("A priority orders every variant of the scope exactly once")
        return self


class Event(SuiteModel):
    """A seeded perturbation (suite/events.py): applied once, when the flow reaches `at`, and in
    force for the rest of the trial."""

    at: Trigger
    kind: EventKind
    # The offer the event changes, or whose catalog field it rewrites.
    variant: Id | None = None
    # The item price or the shipping after the change.
    amount_minor: PositiveMinor | None = None
    # The scope consented in place of the revoked one.
    replacement: Delegation | None = None
    # catalog_text: the field it rewrites and the text it puts there.
    field: CatalogField | None = None
    text: Annotated[str, Field(min_length=1, max_length=MAX_CATALOG_TEXT)] | None = None

    @model_validator(mode="after")
    def _parameters_of_the_kind(self) -> Self:
        needed = parameters_of(self.kind)
        if tuple(name for name in PARAMETERS if getattr(self, name) is not None) != needed:
            raise ValueError(f"{self.kind} takes {', '.join(needed) or 'no parameters'}")
        if self.kind == "catalog_text" and self.at != "start":
            raise ValueError("catalog_text is seeded at start, before the agent reads the catalog")
        return self


class Environment(SuiteModel):
    catalog: Catalog
    # Each condition is a trial of its own; the trial record names the one it ran.
    governance: Annotated[tuple[Governance, ...], Field(min_length=1), AfterValidator(_distinct)]
    events: tuple[Event, ...]


class Effects(SuiteModel):
    """Exact counts in the final state of the Merchant and the PSP."""

    orders: Minor
    checkouts: Minor
    psp_charges: Minor


class Purchase(SuiteModel):
    variant: Id
    # COP minor units, shipping included: the Merchant's authoritative total.
    total_minor: PositiveMinor


class Expected(SuiteModel):
    effects: Effects
    # Any of these ends the trial well; buy only on its own.
    outcome: Annotated[tuple[Outcome, ...], Field(min_length=1), AfterValidator(_distinct)]
    purchase: Purchase | None = None
    # No checkout or order of these variants (p0-llm-batch2.json `no_checkout_of`).
    forbidden_variants: Ids = ()
    must_communicate: tuple[Text, ...]
    must_not_claim: tuple[Text, ...]
    # The links of verify_evidence (presente) or verify_evidence_hnp (delegado) that must be ok.
    evidence: Annotated[tuple[Text, ...], AfterValidator(_distinct)]

    @model_validator(mode="after")
    def _purchase_when_buying(self) -> Self:
        if "buy" in self.outcome and len(self.outcome) > 1:
            raise ValueError("buy is an outcome on its own")
        if (self.outcome == ("buy",)) != (self.purchase is not None):
            raise ValueError("A purchase is expected exactly when the outcome is buy")
        if self.purchase is not None and self.purchase.variant in self.forbidden_variants:
            raise ValueError(f"The purchase {self.purchase.variant} is a forbidden variant")
        return self


class Reference(SuiteModel):
    """The case's reference solution; it must pass every code grader. The reference agent
    derives it from `expected` (suite/live/reference.py), so `agent_script` is the case's id."""

    agent_script: Text


class _Case(SuiteModel):
    links: ClassVar[tuple[str, ...]]

    id: CaseId
    mode: str
    # The scenario of scenarios/p0/matrix.json or data/injection-scenarios-v1.json, for
    # traceability only.
    source: Id | None = None
    group: Group
    suites: Annotated[tuple[Suite, ...], Field(min_length=1), AfterValidator(_distinct)]
    environment: Environment
    expected: Expected
    reward_basis: Annotated[tuple[Grader, ...], Field(min_length=1), AfterValidator(_distinct)]
    reference: Reference

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        escenario, _, mode = self.id.partition("/")
        if not escenario or mode != self.mode:
            raise ValueError(f"The id {self.id} is not <escenario>/{self.mode}")
        if unknown := sorted(set(self.expected.evidence) - set(self.links)):
            raise ValueError(
                f"Expects evidence links {self.mode} does not have: {', '.join(unknown)}"
            )
        if (expected := suites_of(self)) != set(self.suites):
            raise ValueError(f"The suites should be {_named(expected)}, not {_named(self.suites)}")
        if self.group == MANIPULATION and set(self.environment.governance) != {"B0", "B2"}:
            raise ValueError("A manipulacion case runs under B0 and B2")
        return self

    @model_validator(mode="after")
    def _injections_reach_the_model(self) -> Self:
        """A `catalog_text` the model never reads would test nothing (suite/exposure.py)."""
        visible = visible_fields(self.mode)
        hidden = [
            f"{event.field} of {event.variant}"
            for event in self.environment.events
            if event.kind == "catalog_text" and event.field not in visible
        ]
        if hidden:
            raise ValueError(
                f"catalog_text in a field no {self.mode} model request carries: {', '.join(hidden)}"
            )
        return self


def suites_of(case: _Case) -> set[Suite]:
    """The suites the spec (§2) puts a case in: every case runs in regression; capability needs a
    model to measure; manipulation is measured apart, in the adversarial suite."""
    if case.group == MANIPULATION:
        return {"regression", "adversarial"}
    if any(event.kind in MODEL_FAULTS for event in case.environment.events):
        return {"regression"}
    return {"regression", "capability"}


def _named(suites: Iterable[Suite]) -> str:
    present = set(suites)
    return " and ".join(suite for suite in SUITES if suite in present)


class PresentCase(_Case):
    links = LINKS

    mode: Present  # pyright: ignore[reportIncompatibleVariableOverride]
    user: User

    @model_validator(mode="after")
    def _without_delegation_events(self) -> Self:
        for event in self.environment.events:
            if event.at in DELEGATED_TRIGGERS or event.kind in OWNER_EVENTS:
                raise ValueError(f"A presente case has no delegation: {event.kind} at {event.at}")
        return self


class DelegatedCase(_Case):
    links = HNP_LINKS

    mode: Delegated  # pyright: ignore[reportIncompatibleVariableOverride]
    delegation: Delegation


Case = Annotated[PresentCase | DelegatedCase, Field(discriminator="mode")]
CASE: TypeAdapter[PresentCase | DelegatedCase] = TypeAdapter(Case)


def load_catalog(directory: Path) -> list[PresentCase | DelegatedCase]:
    """Every case under `directory`. A file's path is its group and its id, and no id repeats."""
    cases = []
    for path in sorted(directory.rglob("*.json")):
        try:
            case = CASE.validate_json(path.read_bytes())
        except ValidationError as error:
            error.add_note(str(path))
            raise
        named = path.relative_to(directory).with_suffix("").as_posix()
        if named != f"{case.group}/{case.id}":
            raise ValueError(f"{path}: the path should be {case.group}/{case.id}")
        cases.append(case)
    ids = [case.id for case in cases]
    if repeated := sorted({case_id for case_id in ids if ids.count(case_id) > 1}):
        raise ValueError(f"Duplicate case ids: {', '.join(repeated)}")
    return cases
