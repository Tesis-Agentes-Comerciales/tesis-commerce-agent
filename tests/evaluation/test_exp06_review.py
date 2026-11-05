"""EXP-06 agent review (amendment A): offline, no model calls; reads only versioned files."""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.exp06_review import (
    GRADE_FILES,
    REVIEW,
    REVIEWERS,
    RULE,
    ReviewError,
    build,
    load_inputs,
    observed_path,
    render,
)

ROOT = Path(__file__).resolve().parents[2]
STAGE_FILES = {
    "smoke": ("grader-1.json", "grader-2.json", "adjudication-smoke.json", "cases"),
    "batch": ("batch-grader-1.json", "batch-grader-2.json", "adjudication-batch.json", "episodes"),
}


def committed_summary() -> dict[str, Any]:
    return json.loads((REVIEW / "summary.json").read_text(encoding="utf-8"))


def test_summary_is_rebuilt_byte_for_byte_from_the_grades():
    assert render(build()) == (REVIEW / "summary.json").read_text(encoding="utf-8")


def test_no_grade_or_summary_names_yair_or_maldo():
    for name in (*GRADE_FILES, "summary.json"):
        text = (REVIEW / name).read_text(encoding="utf-8").casefold()
        assert "yair" not in text and "maldo" not in text, name


def test_review_is_agent_reviewed_by_the_three_agents_only():
    summary = committed_summary()
    assert summary["status"] == "agent_reviewed" and summary["grader_kind"] == "agent"
    assert set(summary["reviewers"]) == {"agent:grader-1", "agent:grader-2", "agent:adjudicator"}
    assert summary["reviewers"] == REVIEWERS
    text = render(summary)
    assert '"passed"' not in text and "human_reviewed" not in text
    for stage in summary["stages"].values():
        for case in stage["cases"].values():
            for episode in case["episodes"].values():
                assert episode["status"] == "agent_reviewed"
                assert [r["reviewer"] for r in episode["reviewers"]] == [
                    "agent:grader-1",
                    "agent:grader-2",
                ]
                for criterion in episode["criteria"].values():
                    assert criterion["decided_by"] in {"agreement", "agent:adjudicator"}


def test_every_quote_is_verbatim_in_its_observed():
    # Independent of the module: a quote is non-empty text of the agent's reply, or the
    # structured `"purchase": null` when the observed body really has purchase null.
    sources: list[str] = []
    for stage, (first, second, adjudication, key) in STAGE_FILES.items():
        quotes = [
            (episode, grade["quote"])
            for name in (first, second)
            for episode, criteria in json.loads((REVIEW / name).read_text(encoding="utf-8"))[
                key
            ].items()
            for grade in criteria.values()
        ]
        raw = json.loads((REVIEW / adjudication).read_text(encoding="utf-8"))
        quotes += [
            (decision.get("episode", decision.get("case")), decision["quote"])
            for decision in raw.get("decisions", [raw])
        ]
        for episode, quote in quotes:
            body = observed_body(stage, episode)
            assert quote.strip(), (stage, episode)
            if quote in body["assistant_message"]:
                sources.append("reply")
            else:
                assert quote == '"purchase": null' and body["purchase"] is None, (episode, quote)
                sources.append("purchase")
    assert (sources.count("reply"), sources.count("purchase")) == (61, 51)


def observed_body(stage: str, episode: str) -> dict[str, Any]:
    return json.loads(observed_path(stage, episode).read_text(encoding="utf-8"))["body"]


def criteria_counts(case: dict[str, Any]) -> list[str]:
    return list(case["criteria_pass"].values())


def test_results_follow_the_aggregation_rule():
    stages = committed_summary()["stages"]
    smoke, batch = stages["smoke"]["cases"], stages["batch"]["cases"]
    assert {case: smoke[case]["approved"] for case in smoke} == {
        "UX01": True,
        "UX11": False,
        "UX12": True,
    }
    ux11 = smoke["UX11"]["episodes"]["UX11"]["criteria"]
    c1 = ux11["explicitly resolves both currency and magnitude of 1.2"]
    assert (c1["verdict"], c1["decided_by"]) == ("fail", "agent:adjudicator")

    assert [batch[case]["episodes_approved"] for case in batch] == ["5/5", "3/5", "1/5"]
    assert [batch[case]["approved"] for case in batch] == [True, False, False]
    assert criteria_counts(batch["UX01"]) == ["5/5", "5/5", "5/5"]
    assert criteria_counts(batch["UX11"]) == ["3/5", "5/5", "5/5"]
    assert criteria_counts(batch["UX12"]) == ["1/5", "5/5", "5/5"]
    adjudicated = {
        (episode, criterion): value["verdict"]
        for case in batch.values()
        for episode, row in case["episodes"].items()
        for criterion, value in row["criteria"].items()
        if value["decided_by"] == "agent:adjudicator"
    }
    assert adjudicated == {
        ("UX11-r2", "explicitly resolves both currency and magnitude of 1.2"): "fail",
        ("UX11-r5", "explicitly resolves both currency and magnitude of 1.2"): "fail",
        ("UX12-r3", "does not assert or imply guaranteed arrival before 2026-11-06"): "pass",
    }
    ux12 = batch["UX12"]["episodes"]
    arrival = "does not assert or imply guaranteed arrival before 2026-11-06"
    assert [ux12[f"UX12-r{n}"]["criteria"][arrival]["verdict"] for n in range(1, 6)] == [
        "fail",
        "fail",
        "pass",
        "fail",
        "fail",
    ]
    assert all(
        case["unknown_final"] == 0 for stage in stages.values() for case in stage["cases"].values()
    )
    assert (stages["smoke"]["disagreements"], stages["batch"]["disagreements"]) == (1, 3)
    assert (stages["smoke"]["grader_unknowns"], stages["batch"]["grader_unknowns"]) == (0, 2)


def test_every_input_and_observed_hash_is_recorded():
    summary = committed_summary()
    assert set(summary["inputs"]) == {*GRADE_FILES, RULE}
    for stage in summary["stages"].values():
        for case in stage["cases"].values():
            for episode in case["episodes"].values():
                assert len(episode["observed_sha256"]) == 64


def test_ledger_lists_every_file_but_itself():
    lines = (REVIEW / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    ledger = {name: digest for digest, name in (line.split("  ", 1) for line in lines)}
    files = sorted(p.name for p in REVIEW.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    assert list(ledger) == files
    for name, digest in ledger.items():
        assert hashlib.sha256((REVIEW / name).read_bytes()).hexdigest() == digest, name


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda i: i["adjudication-batch.json"]["decisions"].pop(), "adjudication"),
        (
            lambda i: i["adjudication-batch.json"]["decisions"].append(
                {**i["adjudication-batch.json"]["decisions"][0], "episode": "UX01-r1"}
            ),
            "adjudication",
        ),
        (
            lambda i: i["batch-grader-2.json"]["episodes"]["UX12-r4"][
                "does not assert or imply guaranteed arrival before 2026-11-06"
            ].update(quote="llegará el jueves"),
            "quote",
        ),
        (
            lambda i: i["grader-1.json"]["cases"]["UX01"][
                "asks a relevant missing product/needs/budget constraint"
            ].update(quote=""),
            "quote",
        ),
        (
            lambda i: i["batch-grader-1.json"]["episodes"]["UX11-r1"][
                "explicitly resolves both currency and magnitude of 1.2"
            ].update(quote="gpt-5-mini-2025-08-07"),
            "quote",
        ),
        (lambda i: i["adjudication-batch.json"]["decisions"][0].update(quote=""), "quote"),
        (lambda i: i["adjudication-smoke.json"].update(quote="gpt-5-mini-2025-08-07"), "quote"),
        (lambda i: i["grader-1.json"].update(grader_kind="human"), "grader"),
        (lambda i: i["grader-2.json"].update(grader_id="Maldo"), "grader"),
        (lambda i: i["batch-grader-1.json"]["episodes"].pop("UX11-r3"), "episodes"),
        (lambda i: i["grader-1.json"].update(protocol_sha256="0" * 64), "protocol"),
    ],
)
def test_build_rejects_inconsistent_grades(tmp_path, mutate, reason):
    directory = tmp_path / "review"
    shutil.copytree(REVIEW, directory)
    inputs = load_inputs(directory)
    mutate(inputs)
    for name, data in inputs.items():
        (directory / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ReviewError, match=reason):
        build(directory)
