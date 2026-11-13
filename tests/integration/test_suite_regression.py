"""The suite's regression on the live stack: every case of the catalog, under each of its
governance conditions, with the reference agent and the scripted user (eval-suite.md §2, §4).

No model is configured and no LLM is called. Each trial runs through `LiveExecutor`, as
`eval-suite run --suite regression` does, and must be a strict success on everything code
decides: every grader of its reward basis passes, under B0 as under B2 (the evidence grader sets
B0's governance links aside itself, §4). Only what the agent says that code cannot square is
left, for the judge: a claims `unknown` whose reason starts with `FOR_THE_JUDGE`.

A case that fails here is inconsistent with the product or the stack, since its reference ends
where it expects. Each record is written to the run directory,
`records/<case>-<governance>.json`.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from commerce_evaluation.paths import DATA
from commerce_evaluation.stack.environment import missing_requirements
from commerce_evaluation.suite.cases import Case, load_catalog
from commerce_evaluation.suite.executor import RunSettings
from commerce_evaluation.suite.graders.claims import FOR_THE_JUDGE
from commerce_evaluation.suite.graders.grade import Grade
from commerce_evaluation.suite.graders.success import grade
from commerce_evaluation.suite.live_executor import LiveExecutor

CATALOG = load_catalog(DATA / "suite" / "cases")
TRIALS = [
    pytest.param(case, governance, id=f"{case.id}-{governance}")
    for case in CATALOG
    for governance in case.environment.governance
]


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("suite-regression")


@pytest.fixture(scope="module")
def executor(run_dir: Path) -> Iterator[LiveExecutor]:
    if problems := missing_requirements():
        pytest.fail("RUN_LIVE_STACK=1 also requires " + "; ".join(problems), pytrace=False)
    settings = RunSettings("suite-regression", run_dir, "regression", None, "low", None)
    with LiveExecutor(settings, None) as started:
        yield started


@pytest.mark.parametrize(("case", "governance"), TRIALS)
def test_reference_passes_the_code_graders(executor, run_dir, case: Case, governance):
    record = executor.run(case, governance, 1, 1000)
    records = run_dir / "records"
    records.mkdir(exist_ok=True)
    name = re.sub(r"[^a-z0-9-]+", "-", f"{case.id}-{governance}".lower())
    (records / f"{name}.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    assert record.infra_failure is None, record.infra_failure
    assert record.agent.model == "reference" and record.usage.usd == 0
    # Strict success on what code decides: no fail, and no unknown but the judge's.
    left = [
        f"{given.grader} {given.verdict}: {given.reason}"
        for given in grade(case, record)
        if given.verdict != "pass" and not for_the_judge(given)
    ]
    assert not left, left


def for_the_judge(given: Grade) -> bool:
    return (
        given.grader == "claims"
        and given.verdict == "unknown"
        and given.reason.startswith(FOR_THE_JUDGE)
    )
