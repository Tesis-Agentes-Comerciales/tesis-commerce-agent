"""EXP-06 agent review (amendment A): rebuild ``summary.json`` from the versioned grades.

Offline and deterministic. It reads the six grade files and the aggregation rule in
``evaluation/results/exp06-review/`` and the ``observed`` evidence of the smoke and the batch,
whose hashes it checks against their ``SHA256SUMS``. It calls no model.

    uv run python -m commerce_evaluation.exp06_review            # rewrite summary.json
    uv run python -m commerce_evaluation.exp06_review --check    # fail if it is stale

Aggregation (``aggregation-rule.md``): a criterion keeps the graders' verdict when both agree and
the adjudicator's when they differ; an episode is approved when its three criteria are ``pass``;
a case is approved when all its episodes in the stage are; ``unknown`` never counts as ``pass``.
"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from commerce_evaluation.exp06 import ROOT, protocol, protocol_hash
from commerce_evaluation.exp06_execution import CASES, ORIGINAL_HASH, schedule

REVIEW = ROOT / "evaluation/results/exp06-review"
EVIDENCE = {
    "smoke": "evaluation/results/exp06-smoke/evidence",
    "batch": "evaluation/results/exp06-batch/evidence",
}
# Per stage: grader-1 file, grader-2 file, adjudication file, key of the graded episodes.
STAGES = {
    "smoke": ("grader-1.json", "grader-2.json", "adjudication-smoke.json", "cases"),
    "batch": ("batch-grader-1.json", "batch-grader-2.json", "adjudication-batch.json", "episodes"),
}
GRADE_FILES = tuple(name for files in STAGES.values() for name in files[:3])
RULE = "aggregation-rule.md"
GRADERS = ("agent:grader-1", "agent:grader-2")
ADJUDICATOR = "agent:adjudicator"
REVIEWERS = {
    "agent:grader-1": {"role": "grader", "model": "Claude Opus", "model_snapshot": None},
    "agent:grader-2": {"role": "grader", "model": "Claude Sonnet", "model_snapshot": None},
    ADJUDICATOR: {"role": "adjudicator", "model": "Claude Opus", "model_snapshot": None},
}
VERDICTS = {"pass", "fail", "unknown"}
# The only quote allowed outside the agent's reply: the structured field the effect criteria cite.
PURCHASE_NULL = '"purchase": null'


class ReviewError(ValueError):
    pass


def require(condition: object, reason: str) -> None:
    if not condition:
        raise ReviewError(reason)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_inputs(directory: Path = REVIEW) -> dict[str, Any]:
    return {
        name: json.loads((directory / name).read_text(encoding="utf-8")) for name in GRADE_FILES
    }


def observed_path(stage: str, episode: str) -> Path:
    return ROOT / EVIDENCE[stage] / f"{episode}-observed.json"


def observed(stage: str, episode: str, ledger: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Hash of an episode's evidence and the response body the quotes are checked against."""
    path = observed_path(stage, episode)
    data = path.read_bytes()
    digest = sha256(data)
    require(ledger.get(path.name) == digest, f"observed hash differs from its ledger: {episode}")
    return digest, json.loads(data)["body"]


def quoted(quote: object, body: dict[str, Any]) -> bool:
    """Non-empty text of the agent's reply, or `"purchase": null` when the body says so."""
    if quote == PURCHASE_NULL:
        return "purchase" in body and body["purchase"] is None
    return isinstance(quote, str) and bool(quote.strip()) and quote in body["assistant_message"]


def evidence_ledger(stage: str) -> dict[str, str]:
    lines = (ROOT / EVIDENCE[stage] / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    return {name: digest for digest, name in (line.split("  ", 1) for line in lines if line)}


def grades(raw: dict[str, Any], reviewer: str, key: str, episodes: list[str]) -> dict[str, Any]:
    require(
        raw.get("grader_kind") == "agent" and f"agent:{raw.get('grader_id')}" == reviewer,
        f"unexpected grader for {reviewer}",
    )
    require(raw.get("protocol_sha256") == ORIGINAL_HASH == protocol_hash(), "protocol hash")
    require(set(raw.get(key, {})) == set(episodes), f"grader episodes differ: {reviewer}")
    return raw[key]


def adjudications(raw: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    require(
        raw.get("grader_kind") == "agent" and raw.get("role") == "adjudicator",
        "unexpected grader for the adjudication",
    )
    decided: dict[tuple[str, str], dict[str, Any]] = {}
    for decision in raw.get("decisions", [raw]):
        key = (decision.get("episode", decision.get("case")), decision["criterion"])
        require(key not in decided, f"duplicate adjudication: {key}")
        decided[key] = decision
    return decided


def count(hits: int, total: int) -> str:
    return f"{hits}/{total}"


def stage_summary(stage: str, inputs: dict[str, Any]) -> dict[str, Any]:
    first, second, adjudication, key = STAGES[stage]
    order = schedule(stage)
    episodes = [episode for episode, _ in order]
    graded = {
        GRADERS[0]: grades(inputs[first], GRADERS[0], key, episodes),
        GRADERS[1]: grades(inputs[second], GRADERS[1], key, episodes),
    }
    decided, used = adjudications(inputs[adjudication]), set()
    ledger = evidence_ledger(stage)
    rows: dict[str, dict[str, Any]] = {case: {} for case in CASES}
    for episode, case in order:
        rubric = protocol()["cases"][case]["rubric"]
        digest, body = observed(stage, episode, ledger)
        reviewers = []
        for reviewer in GRADERS:
            criteria = graded[reviewer][episode]
            require(list(criteria) == rubric, f"criteria differ from the rubric: {episode}")
            for grade in criteria.values():
                require(
                    grade["verdict"] in VERDICTS, f"verdict outside pass/fail/unknown: {episode}"
                )
                require(
                    quoted(grade["quote"], body), f"quote not in the reply: {reviewer}/{episode}"
                )
            reviewers.append(
                {
                    "reviewer": reviewer,
                    "criteria": {
                        criterion: {"verdict": grade["verdict"], "quote": grade["quote"]}
                        for criterion, grade in criteria.items()
                    },
                }
            )
        final: dict[str, dict[str, Any]] = {}
        for criterion in rubric:
            first_verdict, second_verdict = (
                graded[r][episode][criterion]["verdict"] for r in GRADERS
            )
            decision = decided.get((episode, criterion))
            if first_verdict == second_verdict:
                require(decision is None, f"adjudication without a disagreement: {episode}")
                final[criterion] = {"verdict": first_verdict, "decided_by": "agreement"}
                continue
            if decision is None:
                raise ReviewError(f"missing adjudication: {episode}/{criterion}")
            require(
                decision["verdict"] in VERDICTS and quoted(decision["quote"], body),
                f"adjudication verdict or quote not in the reply: {episode}",
            )
            used.add((episode, criterion))
            final[criterion] = {
                "verdict": decision["verdict"],
                "decided_by": ADJUDICATOR,
                "quote": decision["quote"],
                "borderline": decision.get("borderline"),
            }
        rows[case][episode] = {
            "status": "agent_reviewed",
            "approved": all(value["verdict"] == "pass" for value in final.values()),
            "observed": f"{EVIDENCE[stage]}/{episode}-observed.json",
            "observed_sha256": digest,
            "criteria": final,
            "reviewers": reviewers,
        }
    require(used == set(decided), "adjudication without a disagreement or outside the rubric")
    cases = {}
    for case, row in rows.items():
        total = len(row)
        approved = sum(episode["approved"] for episode in row.values())
        cases[case] = {
            "approved": approved == total,
            "episodes_approved": count(approved, total),
            "criteria_pass": {
                criterion: count(
                    sum(e["criteria"][criterion]["verdict"] == "pass" for e in row.values()), total
                )
                for criterion in protocol()["cases"][case]["rubric"]
            },
            "unknown_final": sum(
                value["verdict"] == "unknown"
                for e in row.values()
                for value in e["criteria"].values()
            ),
            "episodes": row,
        }
    return {
        "evidence": EVIDENCE[stage],
        "grades": [first, second],
        "adjudication": adjudication,
        "episodes": len(order),
        "grader_unknowns": sum(
            grade["verdict"] == "unknown"
            for reviewer in GRADERS
            for criteria in graded[reviewer].values()
            for grade in criteria.values()
        ),
        "disagreements": len(used),
        "cases": cases,
    }


def build(directory: Path = REVIEW) -> dict[str, Any]:
    inputs = load_inputs(directory)
    return {
        "experiment": "EXP-06",
        "amendment": "amendment-A-agent-review.md",
        "status": "agent_reviewed",
        "grader_kind": "agent",
        "protocol": {"path": "evaluation/scenarios/exp06-hp.json", "sha256": protocol_hash()},
        "aggregation_rule": RULE,
        "reviewers": REVIEWERS,
        "inputs": {name: sha256((directory / name).read_bytes()) for name in (*GRADE_FILES, RULE)},
        "generated_by": "uv run python -m commerce_evaluation.exp06_review",
        "stages": {stage: stage_summary(stage, inputs) for stage in STAGES},
    }


def render(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Rebuild the EXP-06 agent review summary.")
    parser.add_argument("--check", action="store_true", help="fail if summary.json is stale")
    args = parser.parse_args(argv)
    text, target = render(build()), REVIEW / "summary.json"
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            raise SystemExit("summary.json is stale: rebuild it")
        return
    target.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
