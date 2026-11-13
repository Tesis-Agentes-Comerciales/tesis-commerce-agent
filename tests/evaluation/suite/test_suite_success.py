"""Strict success over the reward basis, and every grader on the seed cases (§2)."""

import pytest

from commerce_evaluation.paths import DATA
from commerce_evaluation.suite.cases import load_catalog
from commerce_evaluation.suite.graders.grade import Grade, Verdict
from commerce_evaluation.suite.graders.success import grade, strict_success
from tests.evaluation.suite.test_suite_catalog import SEED_IDS
from tests.evaluation.suite.trials import case, record

BASIS = case()  # effects, outcome, evidence and claims


def grades(*verdicts: Verdict) -> list[Grade]:
    """One grade per verdict, for effects, outcome, evidence and claims in that order."""
    names = ("effects", "outcome", "evidence", "claims")
    return [
        Grade(grader=name, verdict=verdict, reason="x")
        for name, verdict in zip(names, verdicts, strict=False)
    ]


@pytest.mark.parametrize(
    ("verdicts", "success"),
    [
        (("pass", "pass", "pass", "pass"), "pass"),
        (("pass", "fail", "pass", "unknown"), "fail"),
        (("pass", "pass", "pass", "unknown"), "unknown"),
    ],
    ids=["all-pass", "any-fail", "unknown-is-not-a-pass"],
)
def test_strict_success(verdicts: tuple[Verdict, ...], success: Verdict) -> None:
    assert strict_success(BASIS, grades(*verdicts)) == success


def test_only_the_reward_basis_counts() -> None:
    narrow = case(evidence=[]).model_copy(update={"reward_basis": ("effects", "outcome")})

    assert strict_success(narrow, grades("pass", "pass", "fail", "unknown")) == "pass"


def test_every_grader_of_the_basis_must_grade_once() -> None:
    with pytest.raises(ValueError, match="No grade from claims"):
        strict_success(BASIS, grades("pass", "pass", "pass"))
    with pytest.raises(ValueError, match="Two effects grades"):
        strict_success(BASIS, [*grades("pass", "pass", "pass", "pass"), grades("fail")[0]])


def test_the_seed_cases_grade_a_trial_that_does_what_they_expect() -> None:
    """A trial that ends as each seed expects passes effects and outcome; evidence needs its
    chain, and claims waits for the judge on the seeds' criteria. No seed expects a purchase; the
    rest of the catalog does not fit this one ending."""
    catalog = {case.id: case for case in load_catalog(DATA / "suite" / "cases")}
    for seed in (catalog[seed_id] for seed_id in SEED_IDS):
        final = {"action": "respond"} if seed.mode == "presente" else {"task_state": "failed"}
        verdicts = {given.grader: given.verdict for given in grade(seed, record(seed, final=final))}

        assert verdicts["effects"] == verdicts["outcome"] == "pass", seed.id
        assert verdicts["claims"] == "unknown", seed.id


def test_a_record_of_another_case_is_refused() -> None:
    with pytest.raises(ValueError, match="was paired with"):
        grade(case("delegado"), record(case()))
