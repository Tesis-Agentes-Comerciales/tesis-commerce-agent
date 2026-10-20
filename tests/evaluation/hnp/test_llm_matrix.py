"""EC-HNP-02 paired matrix, scoring and run script, offline with FakeTransport only."""

import ast
import json
import os
import shutil
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.hnp.evaluation import load_oracles
from commerce_evaluation.hnp.evidence import ExperimentalEvidence, verify_entries
from commerce_evaluation.hnp.harness import load_cases
from commerce_evaluation.hnp.llm.cost import TokenPrices, estimate
from commerce_evaluation.hnp.llm.matrix import (
    MatrixRun,
    PlannedEpisode,
    business_cases,
    plan_episodes,
    review_inputs,
    run_matrix,
)
from commerce_evaluation.hnp.llm.prompts import load_prompt
from commerce_evaluation.hnp.llm.schemas import BUSINESS_CHECKS
from commerce_evaluation.hnp.llm.scoring import (
    build_summary,
    case_kind,
    mcnemar_exact,
    wilson,
)
from commerce_evaluation.hnp.llm.trace import TraceWriter, load_traces
from commerce_evaluation.hnp.llm.transport import (
    FakeReply,
    FakeTransport,
    LlmRequest,
    TransportError,
)
from commerce_evaluation.hnp.llm.validator import (
    LlmValidatorAgent,
    dry_run_responder,
    review_input,
    sample_decision,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "evaluation/data/hnp"
CASES = load_cases(FIXTURES / "cases.json")
ORACLES = load_oracles(FIXTURES / "oracle.json")
PROMPT = load_prompt("validator-v1")
PRICES = TokenPrices(Decimal("1.5"), Decimal("6"))
Responder = Callable[[LlmRequest], FakeReply]


def decision(verdict: str, failing: tuple[str, ...] = ()) -> FakeReply:
    checks = {c: ("fail" if c in failing else "pass") for c in BUSINESS_CHECKS}
    return FakeReply(output={"checks": checks, "decision": verdict, "reasons": []})


def matrix(
    responder: Responder,
    *,
    seed: int = 3,
    repetitions: int = 2,
    traces: TraceWriter | None = None,
    evidence: ExperimentalEvidence | None = None,
) -> MatrixRun:
    transport = FakeTransport(responder)

    def factory(episode: PlannedEpisode) -> LlmValidatorAgent:
        return LlmValidatorAgent(
            transport=transport,
            model="validator-under-test",
            prompt=PROMPT,
            temperature=None,
            reasoning_effort=None,
            max_output_tokens=256,
            timeout_seconds=5.0,
            prices=PRICES,
            seed=seed,
            call_prefix=episode.episode_id,
        )

    return run_matrix(
        cases=CASES,
        repetitions=repetitions,
        seed=seed,
        experiment_id="test-run",
        execution_kind="llm_dry_run",
        validator_factory=factory,
        validator_budget_ns=5_000_000_000,
        drain_seconds=5.0,
        evidence=evidence or ExperimentalEvidence(),
        traces=traces,
    )


def observed(run: MatrixRun) -> list[tuple[Any, ...]]:
    return [
        (
            o.episode.episode_id,
            o.result.record.admission.code,
            o.result.effect_occurred,
            o.result.record.assessment_status,
            tuple(c.status for c in o.calls),
            tuple(c.usage for c in o.calls),
        )
        for o in run.outcomes
    ]


def test_business_cases_are_the_19_fixtures_without_scripted_validator_faults():
    selected, excluded = business_cases(CASES)
    assert len(CASES) == 27 and len(selected) == 19 and len(excluded) == 8
    assert all(c.validator_behavior == "normal" for c in selected)
    kinds = [case_kind(ORACLES[c.case_id]) for c in selected]
    # Very few admissible cases: false-block rates have wide intervals (see README).
    assert (kinds.count("invalid"), kinds.count("admissible")) == (15, 2)
    assert (kinds.count("changed_after_review"), kinds.count("not_reviewable")) == (1, 1)


def test_plan_is_paired_complete_and_seeded():
    ids = tuple(c.case_id for c in business_cases(CASES)[0])
    plan = plan_episodes(ids, 5, seed=42)
    assert len(plan) == 19 * 5 * 2
    assert plan == plan_episodes(ids, 5, seed=42)
    assert [e.pair_id for e in plan] != [e.pair_id for e in plan_episodes(ids, 5, seed=43)]
    assert [e.order_index for e in plan] == list(range(len(plan)))
    for first, second in zip(plan[::2], plan[1::2], strict=True):
        assert first.pair_id == second.pair_id and {first.arm, second.arm} == {"A", "B"}
    assert {(e.case_id, e.repetition, e.arm) for e in plan} == {
        (c, r, a) for c in ids for r in range(1, 6) for a in ("A", "B")
    }
    with pytest.raises(ValueError):
        plan_episodes(ids, 0, seed=1)


def test_matrix_is_deterministic_for_a_seed_and_pairs_share_inputs():
    first = matrix(dry_run_responder(9), seed=9)
    second = matrix(dry_run_responder(9), seed=9)
    assert observed(first) == observed(second)
    assert observed(first) != observed(matrix(dry_run_responder(10), seed=10))
    pairs: dict[str, dict[str, Any]] = {}
    for o in first.outcomes:
        pairs.setdefault(o.episode.pair_id, {})[o.episode.arm] = o.result.record
    for pair in pairs.values():
        assert pair["A"].original_context == pair["B"].original_context
        assert pair["A"].proposal == pair["B"].proposal
        assert pair["A"].configuration_digest == pair["B"].configuration_digest
        assert pair["A"].assessment is None
        assert pair["A"].execution_kind == pair["B"].execution_kind == "llm_dry_run"


def test_validator_never_admits_what_the_backend_blocks():
    run = matrix(lambda _: decision("accept"))
    pairs: dict[str, dict[str, bool]] = {}
    for o in run.outcomes:
        pairs.setdefault(o.episode.pair_id, {})[o.episode.arm] = o.result.effect_occurred
    assert all(p["B"] <= p["A"] for p in pairs.values())
    assert not any(
        o.result.effect_occurred for o in run.outcomes if ORACLES[o.episode.case_id].unsafe_to_admit
    )


def test_always_accepting_validator_adds_no_block_and_no_capture():
    summary = build_summary(matrix(lambda _: decision("accept")), ORACLES, experiment={})
    a, b = summary["arms"]["A"], summary["arms"]["B"]
    assert a["simulated_effects"] == b["simulated_effects"] == 4
    assert b["false_blocks"]["k"] == 0 and b["escapes"]["k"] == 0
    validator = b["validator"]
    assert validator["captures"] == wilson(0, 30)
    assert validator["misses_contained_by_backend"] == 30
    assert summary["comparison"]["all"]["only_b_blocked"] == 0
    assert summary["comparison"]["all"]["mcnemar_exact_p"] == 1.0


def test_always_rejecting_validator_captures_and_over_blocks_but_adds_no_safety():
    summary = build_summary(
        matrix(lambda _: decision("reject", ("budget",))), ORACLES, experiment={}
    )
    b = summary["arms"]["B"]
    assert b["simulated_effects"] == 0
    assert b["false_blocks"]["k"] == b["false_blocks"]["n"] == 4
    validator = b["validator"]
    assert validator["captures"]["k"] == validator["captures"]["n"] == 30
    # The backend flags every invalid fixture too: no capture is unique to the validator.
    assert validator["unique_captures"] == 0
    assert validator["captures_with_expected_checks"]["k"] == 6  # over-total, tamper, unknown
    assert validator["false_rejections"]["n"] == 6
    paired = summary["comparison"]
    assert paired["admissible"]["only_b_blocked"] == 4 and paired["unsafe"]["only_b_blocked"] == 0
    assert paired["all"]["only_a_blocked"] == 0
    assert paired["admissible"]["mcnemar_exact_p"] == mcnemar_exact(4, 0) == 0.125


def test_infrastructure_faults_are_excluded_from_denominators_and_pairs():
    summary = build_summary(
        matrix(lambda _: FakeReply(raises=TransportError("APIConnectionError"))),
        ORACLES,
        experiment={},
    )
    b = summary["arms"]["B"]
    # 18 reviewable cases x 2 repetitions reach the validator; buyer-forged-authority does not.
    assert b["infrastructure_errors"] == 36 and b["scored_episodes"] == 2
    assert b["validator"]["reviewed"] == 0 and b["false_blocks"]["n"] == 0
    assert summary["comparison"]["excluded_pairs"]["infrastructure"] == 36
    assert summary["cost"]["calls_by_status"] == {"transport_error": 36}
    assert summary["cost"]["calls_with_unknown_usage"] == 36
    assert len(summary["deviations"]["infrastructure"]) > 0
    assert summary["deviations"]["harness"] == []


def test_summary_shape_costs_and_traces(tmp_path):
    evidence = ExperimentalEvidence(tmp_path / "evidence")
    traces = TraceWriter(tmp_path / "traces.jsonl")
    run = matrix(dry_run_responder(5), seed=5, traces=traces, evidence=evidence)
    verify_entries(evidence.entries)
    summary = build_summary(run, ORACLES, experiment={"phase": "test"}, strict=True)
    assert {"experiment", "arms", "episodes", "actors"} <= set(summary)
    assert summary["schema"] == "hnp-llm-run/3-draft"
    experiment = summary["experiment"]
    assert experiment["execution_kind"] == "llm_dry_run" and not experiment["operational_effects"]
    assert len(experiment["business_cases"]) == 19 and len(summary["episodes"]) == 76
    validator = next(a for a in summary["actors"] if a["role"] == "validator")
    assert validator["kind"] == "llm" and validator["transport"] == "fake"
    assert validator["prompt_version"] == "validator-v1"
    assert validator["prompt_sha256"] == PROMPT.sha256 and validator["seed"] == 5
    loaded = load_traces(traces.path)
    assert len(loaded) == 76
    cost = summary["cost"]
    assert cost["provider_calls"] == 0 and cost["basis"] == "fake_transport_estimate"
    assert cost["llm_calls"] == sum(len(t.calls) for t in loaded) == 36
    assert cost["cost_usd"] == pytest.approx(sum(t.cost_usd for t in loaded))
    assert all(
        t.result.outcome_class in ("experimental", "infrastructure") and t.actors for t in loaded
    )
    json.dumps(summary, allow_nan=False)


def test_strict_scoring_aborts_on_harness_deviation_only():
    run = matrix(lambda _: decision("accept"))
    changed = dict(ORACLES)
    changed["valid"] = ORACLES["valid"].model_copy(update={"expected_code_a": "BACKEND_DENIED"})
    with pytest.raises(ValueError, match="harness deviation"):
        build_summary(run, changed, experiment={}, strict=True)
    lenient = build_summary(run, changed, experiment={}, strict=False)
    assert {d["case_id"] for d in lenient["deviations"]["harness"]} == {"valid"}
    # Validator disagreements with the oracle are results, not harness faults.
    assert build_summary(run, ORACLES, experiment={}, strict=True)["deviations"]["validator"]


def test_wilson_and_exact_mcnemar_reference_values():
    assert wilson(0, 0)["rate"] is None
    zero = wilson(0, 10)
    assert zero["low"] == 0.0 and zero["high"] == pytest.approx(0.2775, abs=1e-4)
    half = wilson(5, 10)
    assert (half["low"], half["high"]) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert wilson(10, 10)["high"] == 1.0
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(5, 1) == pytest.approx(2 * (1 + 6) / 64)  # C(6,0) + C(6,1)
    assert mcnemar_exact(10, 0) == pytest.approx(2 / 1024)


def test_estimate_counts_only_calls_that_reach_the_validator():
    inputs = review_inputs(CASES, review_input, repetitions=5)
    assert len(inputs) == 18 * 5
    planned = estimate(
        inputs,
        instructions=PROMPT.text,
        sample_output=sample_decision(),
        max_output_tokens=256,
        prices=PRICES,
    )
    assert planned.calls == 90 and planned.max_output_tokens == 90 * 256
    assert Decimal(0) < planned.min_usd < planned.max_usd


def test_matrix_runs_without_network(monkeypatch):
    import socket

    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    run = matrix(dry_run_responder(1), seed=1, repetitions=1)
    assert len(run.outcomes) == 38


def test_only_the_transport_module_can_import_a_provider_sdk():
    package = ROOT / "evaluation/src/commerce_evaluation/hnp"
    prohibited = (
        "commerce_agent.runtime",
        "commerce_agent.application",
        "commerce_agent.ap2",
        "commerce_agent.acp.client",
        "commerce_agent.delegated_payment",
    )
    for file in package.rglob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any(m.startswith(prohibited) for m in modules), file
        if file.name != "transport.py":
            assert not any(m.split(".")[0] in ("openai", "httpx") for m in modules), file


# Run script -------------------------------------------------------------------------------


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch):
    from commerce_evaluation.hnp_matrix import main

    for name in list(os.environ):
        if name.startswith("HNP_") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    return main


def env_file(tmp_path: Path, **overrides: str) -> Path:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines if line.startswith("HNP_"))
    values.update(overrides)
    path = tmp_path / "hnp.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def test_script_refuses_live_calls_without_every_gate(script, tmp_path, monkeypatch, capsys):
    import commerce_evaluation.hnp.llm.transport as transport

    def never(*args, **kwargs):
        raise AssertionError("A provider client must not be constructed")

    monkeypatch.setattr(transport.OpenAIResponsesTransport, "__init__", never)
    output = ".local/pytest-hnp02-refused"
    assert script(["--env-file", str(env_file(tmp_path, HNP_OUTPUT_DIR=output))]) == 2
    assert "HNP_LIVE=1" in capsys.readouterr().err
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    live = env_file(tmp_path, HNP_LIVE="1", HNP_MAX_COST_USD="0.0001", HNP_OUTPUT_DIR=output)
    assert script(["--env-file", str(live)]) == 2
    assert "exceeds HNP_MAX_COST_USD" in capsys.readouterr().err
    assert not (ROOT / output).exists()


def test_script_dry_run_writes_summary_traces_and_evidence(script, tmp_path, capsys):
    output = ROOT / ".local/pytest-hnp02-dry-run"
    shutil.rmtree(output, ignore_errors=True)
    try:
        path = env_file(tmp_path, HNP_REPETITIONS="1", HNP_OUTPUT_DIR=str(output.relative_to(ROOT)))
        assert script(["--dry-run", "--env-file", str(path)]) == 0
        (directory,) = output.iterdir()
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        assert summary["experiment"]["execution_kind"] == "llm_dry_run"
        assert summary["cost"]["provider_calls"] == 0 and len(summary["episodes"]) == 38
        assert "openai_api_key" not in json.dumps(summary)
        assert len(load_traces(directory / "traces.jsonl")) == 38
        assert (directory / "evidence.jsonl").is_file()
        assert "dry-run" in capsys.readouterr().out
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_script_keeps_outputs_out_of_versioned_directories(script, tmp_path, capsys):
    path = env_file(tmp_path, HNP_OUTPUT_DIR="docs/hnp02-should-not-exist")
    assert script(["--dry-run", "--env-file", str(path)]) == 2
    assert ".local" in capsys.readouterr().err
    assert not (ROOT / "docs/hnp02-should-not-exist").exists()
