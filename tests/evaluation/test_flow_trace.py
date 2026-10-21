"""The `flow_trace` block of an HNP pilot row (run-traceability.md §4.1, §4.2, T3).

Each scenario is one real HNP run: the product's runtime, executor, D1/D2/D3-HNP and dispatch over
one SQLite file, with the merchant and the PSP as doubles and, for the `llm` selector, the real SDK
over a scripted `httpx.MockTransport` (tests/support/model_server.py). No request leaves the
process. The block is then read from the stopped run's databases alone.
"""

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from commerce_agent.config import Settings
from commerce_agent.delegation.contract import DelegationView
from commerce_agent.delegation.service import DelegationConsentRequest, DelegationProposalRequest
from commerce_agent.runtime import AgentRuntime
from commerce_evaluation.flow_trace import SCHEMA, FlowTrace, build_flow_trace, with_flow_trace
from commerce_evaluation.hnp_pilot import load_pilot
from commerce_evaluation.hnp_pilot_episode import EpisodeRun, observe, token_occurrences
from commerce_evaluation.hnp_pilot_live import finished_episode
from commerce_evaluation.hnp_pilot_scoring import CostMeter, score_episode
from commerce_evaluation.redaction import REDACTED
from commerce_evaluation.run_artifacts import RunArtifacts
from tests.delegation.test_hnp_runtime import OWNER, governed_hnp, runtime
from tests.evaluation.test_hnp_pilot_episode import FakeMerchant
from tests.support.hnp_dispatch import HNP_ORDER_ID, HnpMerchant, Psp
from tests.support.model_server import ModelServer, shortlisted_choice, structured
from tests.test_delegation_api import FEED, MERCHANT, hnp_settings, proposal_body

MANIFEST, MATRIX = load_pilot()
SCENARIOS = {scenario["id"]: scenario for scenario in MATRIX["scenarios"]}
API_KEY = "sk-test-flow-trace-0123456789abcdef"
REASON = "Elijo SON-01 porque es el más barato."
JWT = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJidXllciJ9.c2lnbmF0dXJl"
BEARER = "Bearer " + "b" * 40
PEM = (
    "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg\n"
    "-----END PRIVATE KEY-----"
)


def llm(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "agent_hnp_selector": "llm",
            "openai_api_key": SecretStr(API_KEY),
            "openai_model": "test-model",
        }
    )


def consent(built: AgentRuntime, **changes: Any) -> DelegationView:
    assert built.delegations is not None
    proposal = built.delegations.propose(
        OWNER, DelegationProposalRequest.model_validate_json(json.dumps(proposal_body(**changes)))
    )
    view, created = built.delegations.consent(
        OWNER,
        DelegationConsentRequest(
            proposal_id=proposal.proposal_id, scope_digest=proposal.scope_digest
        ),
    )
    assert created
    return view


def order_webhook(built: AgentRuntime, database: Path) -> None:
    """The merchant's `order_create`, as the webhook endpoint stores it."""
    with closing(sqlite3.connect(database)) as connection:
        (checkout_id,) = connection.execute("SELECT checkout_id FROM purchases").fetchone()
    order = {"id": HNP_ORDER_ID, "checkout_session_id": checkout_id, "status": "confirmed"}
    event = {"type": "order_create", "data": {"type": "order", **order}}
    body = json.dumps(event, sort_keys=True, separators=(",", ":"))
    assert built.store.record_webhook(
        request_id="whd_1",
        event_type="order_create",
        order_id=HNP_ORDER_ID,
        payload_sha256=hashlib.sha256(body.encode()).hexdigest(),
        payload=event,
    )


def run(
    directory: Path,
    settings: Callable[[Path], Settings],
    *answers: Callable[[dict[str, Any]], Any],
    webhook: bool = False,
    revoke: bool = False,
    **changes: Any,
) -> Path:
    server = ModelServer(*answers)
    with pytest.MonkeyPatch.context() as patch:
        server.install(patch)
        built = runtime(settings(directory), HnpMerchant(MERCHANT), Psp())
        try:
            assert built.hnp_executor is not None and built.delegations is not None
            view = consent(built, **changes)
            if revoke:
                built.delegations.revoke(OWNER, view.grant_id)
            built.hnp_executor.run(view.task.task_id)
            if webhook:
                order_webhook(built, directory / "agent.db")
        finally:
            built.close()
    with closing(sqlite3.connect(directory / "agent.db")) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return directory / "agent.db"


RUNS: dict[str, Callable[[Path], Path]] = {
    "b2_llm": lambda directory: run(
        directory,
        lambda path: llm(governed_hnp(path)),
        structured(lambda body: shortlisted_choice(body) | {"assistant_message": REASON}),
        webhook=True,
    ),
    "b2_invalid_output": lambda directory: run(
        directory,
        lambda path: llm(governed_hnp(path)),
        structured(lambda body: {"action": "purchase", "assistant_message": "?"}),
    ),
    "b2_budget_deny": lambda directory: run(directory, governed_hnp, total_max_minor=71_999),
    "b0": lambda directory: run(directory, hnp_settings),
    "revoked": lambda directory: run(directory, governed_hnp, revoke=True),
}


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Callable[[str], Path]]:
    cache: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name not in cache:
            cache[name] = RUNS[name](tmp_path_factory.mktemp(name))
        return cache[name]

    yield get


@pytest.fixture
def run_dir(built: Callable[[str], Path], tmp_path: Path) -> Callable[[str], Path]:
    """A copy of the run's directory as the pilot leaves it: agent.db and the feed it read."""

    def copy(name: str) -> Path:
        directory = tmp_path / name
        directory.mkdir()
        shutil.copyfile(built(name), directory / "agent.db")
        shutil.copytree(FEED, directory / "feed")
        return directory

    return copy


def trace_of(directory: Path, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    return build_flow_trace(
        directory / "agent.db", directory / "agent.graph.db", secrets, feed=directory / "feed"
    )


def execute(directory: Path, statement: str, *values: Any) -> None:
    with closing(sqlite3.connect(directory / "agent.db")) as connection, connection:
        connection.execute(statement, values)


def model_request(directory: Path, *, outcome: str, error_kind: str | None, output: Any) -> None:
    execute(
        directory,
        "INSERT INTO model_requests (run_id, stage, request_index, started_at, latency_ms, "
        "outcome, error_kind, model, output_json) VALUES (NULL, 'model.route', 1, "
        "'2026-10-21T15:00:00Z', 1.0, ?, ?, 'test-model', ?)",
        outcome,
        error_kind,
        json.dumps(output),
    )


def test_a_b2_hnp_purchase_is_explained_from_its_databases(
    run_dir: Callable[[str], Path],
) -> None:
    trace = trace_of(run_dir("b2_llm"))

    assert FlowTrace.model_validate(trace).schema_version == SCHEMA == "flow-trace/1"
    [run_id] = trace["run_ids"]
    delegation = trace["delegation"]
    assert (delegation["task_state"], delegation["grant_state"]) == ("purchased", "consumed")
    assert delegation["scope"]["variant_ids"] == ["SON-01"]
    assert delegation["scope"]["total_max_minor"] == 150_000
    assert delegation["preference_text"] == "Solo si llega esta semana."
    assert delegation["intent"]["hard"]["max_total_amount_minor"] == 150_000
    # What the model saw and what it answered, reason included, from `model_requests`.
    assert delegation["shortlist_source"] == "model_request"
    assert [item["variant_id"] for item in delegation["shortlist"]] == ["SON-01"]
    assert delegation["shortlist"][0]["display_amount"] == "COP 720"
    assert delegation["choice"] == {
        "selector": "llm",
        "selected_variant_id": "SON-01",
        "message": REASON,
    }
    assert [(item["phase"], item["outcome"]) for item in trace["decisions"]] == [
        ("D1", "allow"),
        ("D2", "allow"),
        ("D3", "allow"),
    ]
    d1 = trace["decisions"][0]
    assert [rule["policy_id"] for rule in d1["rule_results"]] == ["POL-01", "POL-02", "POL-11"]
    assert d1["input"]["selected_unit_minor"] == 72_000
    assert d1["input"]["intent"]["max_total_minor"] == 150_000
    # A reference keeps its id; `input_digest` binds the whole canonical input.
    assert d1["input"]["feed_snapshot_ref"].startswith("source:")
    assert d1["input_digest"].startswith("sha256:")
    assert [item["stage"] for item in trace["freshness"]] == ["close"]
    assert trace["checkout"]["purchase_stage"] == "completed"
    assert trace["reservation"]["state"] == "consumed"
    assert trace["dispatch"]["state"] == "completed"
    assert trace["order"]["order_id"] == HNP_ORDER_ID
    assert [hook["event_type"] for hook in trace["webhooks"]] == ["order_create"]
    [request] = trace["model_requests"]
    assert (request["run_id"], request["stage"], request["outcome"]) == (
        run_id,
        "model.hnp_select",
        "ok",
    )
    assert (request["input_tokens"], request["reasoning_tokens"], request["retries"]) == (
        1825,
        640,
        0,
    )
    assert request["without_tokens"] is False
    assert request["output"]["structured"]["assistant_message"] == REASON
    assert trace["errors"] == [] and trace["unavailable"] == []


def test_the_block_never_carries_a_token_mandate_key_or_the_buyer_s_details(
    run_dir: Callable[[str], Path],
) -> None:
    directory = run_dir("b2_llm")
    text = json.dumps(trace_of(directory), ensure_ascii=False)

    with closing(sqlite3.connect(directory / "agent.db")) as connection:
        forbidden = [
            value
            for query in (
                "SELECT token FROM delegation_mandates",
                "SELECT checkout_receipt_jwt FROM completion_results",
                "SELECT payment_receipt_jwt FROM completion_results",
                "SELECT idempotency_key FROM operation_keys",
                "SELECT idempotency_key FROM d1_checkout_operations",
            )
            for (value,) in connection.execute(query)
            if value
        ]
        (details,) = connection.execute("SELECT checkout_details_json FROM delegation_grants")
    assert len(forbidden) >= 6
    buyer = ["buyer@example.test", "Buyer Test", "+573000000000", "Calle 100 # 10-20", "110111"]
    assert all(item in details[0] for item in buyer)
    for value in (*forbidden, *buyer):
        assert str(value) not in text
    assert "eyJ" not in text


def test_secrets_seeded_in_the_database_end_redacted(run_dir: Callable[[str], Path]) -> None:
    directory = run_dir("b2_llm")
    secret = "invocation-secret-" + "x" * 24
    leaked = f"{JWT} {BEARER} {PEM} {secret}"
    model_request(
        directory,
        outcome="ok",
        error_kind=None,
        output={"structured": {"assistant_message": leaked}, "function_calls": []},
    )
    model_request(directory, outcome="error", error_kind=f"Error {secret}", output=None)

    trace = trace_of(directory, (secret,))

    text = json.dumps(trace, ensure_ascii=False)
    for value in (JWT, "b" * 40, "BEGIN PRIVATE KEY", "MIGHAgEAMBMG", secret):
        assert value not in text
    message = trace["model_requests"][1]["output"]["structured"]["assistant_message"]
    assert message.count(REDACTED) == 4
    assert {"stage": "model.route", "code": f"Error {REDACTED}", "source": "model_requests"} in (
        trace["errors"]
    )


def test_a_secret_the_sweep_cannot_reach_fails_closed(run_dir: Callable[[str], Path]) -> None:
    directory = run_dir("b2_llm")
    secret = "invocation-secret-" + "y" * 24
    # A key is not a value: the sweep leaves it, so the whole block is refused.
    model_request(directory, outcome="ok", error_kind=None, output={"structured": {secret: 1}})

    with pytest.raises(ValueError, match="secret"):
        trace_of(directory, (secret,))
    row = with_flow_trace({"observed": {}}, directory, (secret,))
    assert row["flow_trace"] is None
    assert secret not in json.dumps(row)
    assert row["measurement_errors"]["flow_trace"].startswith("FlowTraceError: ")


def test_a_block_that_breaks_its_schema_keeps_no_fragment_of_a_secret(
    run_dir: Callable[[str], Path],
) -> None:
    """A validation error quotes its input cut to a few characters at each end, which neither the
    value nor the shape sweep recognizes: the cause names where and why, never the input."""
    directory = run_dir("b2_llm")
    d1_key = "9f3c" + "7a1e5b2d" * 7 + "c4e6"
    execute(
        directory,
        "UPDATE delegation_grants SET scope_json = json_set(scope_json, '$.scope', ?)",
        f"pre {d1_key} mid {JWT} post",
    )

    row = with_flow_trace({"observed": {}}, directory, (d1_key,))

    text = json.dumps(row)
    for secret in (d1_key, JWT):
        assert not any(secret[start : start + 8] in text for start in range(len(secret) - 7))
    assert row["flow_trace"] is None
    assert row["measurement_errors"] == {
        "flow_trace": "ValidationError: delegation.scope: dict_type"
    }


def test_without_model_requests_the_field_is_null_and_named(
    run_dir: Callable[[str], Path],
) -> None:
    """An older agent has no `model_requests`: the shortlist is recomputed from the feed as the
    executor computes it, and the stage timing still tells a model chose."""
    directory = run_dir("b2_llm")
    execute(directory, "DROP TABLE model_requests")

    trace = trace_of(directory)

    assert trace["model_requests"] is None and trace["unavailable"] == ["model_requests"]
    delegation = trace["delegation"]
    assert delegation["shortlist_source"] == "recomputed"
    assert [item["variant_id"] for item in delegation["shortlist"]] == ["SON-01"]
    assert delegation["choice"] == {
        "selector": "llm",
        "selected_variant_id": "SON-01",
        "message": None,
    }


def test_a_request_without_tokens_is_kept_and_marked(run_dir: Callable[[str], Path]) -> None:
    """An invalid structured output fails the call: its row has no usage, and it stays."""
    trace = trace_of(run_dir("b2_invalid_output"))

    [request] = trace["model_requests"]
    assert (request["outcome"], request["error_kind"]) == ("error", "ValidationError")
    assert (request["input_tokens"], request["output_tokens"]) == (None, None)
    assert request["without_tokens"] is True
    assert trace["delegation"]["shortlist_source"] == "model_request"
    assert trace["delegation"]["choice"]["selected_variant_id"] is None
    assert trace["errors"] == [
        {"stage": "hnp.task", "code": "selection_not_eligible", "source": "hnp_tasks"},
        {"stage": "model.hnp_select", "code": None, "source": "stage_timings"},
        {"stage": "model.hnp_select", "code": "ValidationError", "source": "model_requests"},
    ]


def test_a_d1_budget_deny_shows_the_input_and_the_rule_that_failed(
    run_dir: Callable[[str], Path],
) -> None:
    trace = trace_of(run_dir("b2_budget_deny"))

    [d1] = trace["decisions"]
    assert (d1["phase"], d1["outcome"], d1["reason_codes"]) == (
        "D1",
        "deny",
        ["BUDGET_NOT_SATISFIABLE"],
    )
    assert {rule["policy_id"]: rule["result"] for rule in d1["rule_results"]}["POL-11"] == "fail"
    assert (d1["input"]["selected_unit_minor"], d1["input"]["intent"]["max_total_minor"]) == (
        72_000,
        71_999,
    )
    assert trace["delegation"]["choice"] == {
        "selector": "no_model_call",
        "selected_variant_id": "SON-01",
        "message": None,
    }
    assert {"stage": "hnp.task", "code": "budget_not_satisfiable", "source": "hnp_tasks"} in (
        trace["errors"]
    )
    assert trace["dispatch"] is None and trace["order"] is None


def test_b0_hnp_recomputes_the_deterministic_shortlist(run_dir: Callable[[str], Path]) -> None:
    trace = trace_of(run_dir("b0"))

    delegation = trace["delegation"]
    assert delegation["shortlist_source"] == "recomputed"
    assert delegation["shortlist"][0] | {"matched_preferences": []} == {
        "variant_id": "SON-01",
        "product_title": delegation["shortlist"][0]["product_title"],
        "variant_title": delegation["shortlist"][0]["variant_title"],
        "display_amount": "COP 720",
        "preference_score": delegation["shortlist"][0]["preference_score"],
        "matched_preferences": [],
    }
    assert delegation["choice"]["selector"] == "no_model_call"
    assert delegation["choice"]["selected_variant_id"] == "SON-01"
    assert trace["model_requests"] == [] and trace["errors"] == []
    assert trace["order"]["order_id"] == HNP_ORDER_ID
    # B0-HNP has no governance: D1 never ran.
    assert not trace["decisions"]


def test_a_task_never_claimed_has_no_shortlist(run_dir: Callable[[str], Path]) -> None:
    """Revoked before the trigger: the executor never computed a shortlist, so none is shown."""
    trace = trace_of(run_dir("revoked"))

    assert trace["run_ids"] == []
    delegation = trace["delegation"]
    assert (delegation["task_state"], delegation["grant_state"]) == ("canceled", "revoked")
    assert (delegation["shortlist_source"], delegation["shortlist"]) == (None, None)
    assert delegation["choice"]["selected_variant_id"] is None
    assert trace["errors"] == [
        {"stage": "hnp.task", "code": "revoked_by_owner", "source": "hnp_tasks"}
    ]
    assert "feed" not in trace["unavailable"]


def test_the_block_changes_no_measure_of_the_row(
    run_dir: Callable[[str], Path], tmp_path: Path
) -> None:
    """`observed`, C8 and `score_episode` are the same before and after the block, which only adds
    a key and never writes to the run's files."""
    directory = run_dir("b2_llm")
    psp = tmp_path / "payments.db"
    with closing(sqlite3.connect(psp)) as connection, connection:
        connection.execute("CREATE TABLE delegations (checkout_id TEXT, result_json TEXT)")
    with closing(sqlite3.connect(directory / "agent.db")) as connection:
        grant_id, task_id = connection.execute("SELECT grant_id, task_id FROM hnp_tasks").fetchone()
    episode = EpisodeRun(
        merchant_run_id="run-1",
        grant_id=grant_id,
        task_id=task_id,
        trigger=[{"status": 202, "code": None, "label": "202"}],
        task_view={"task_id": task_id, "state": "purchased", "outcome_code": None},
        delegation_view={"escalation": None},
        token_probe="never-written",
    )

    def measured() -> tuple[dict[str, Any], dict[str, Any]]:
        observed, errors = observe(
            "B2-HNP",
            episode,
            run_dir=directory,
            psp_database=psp,
            merchant=FakeMerchant(),
            verifier=None,
        )
        cost = CostMeter.from_manifest(MANIFEST).charge(observed["model_usage"])
        score = score_episode(SCENARIOS["HN09"], "B2-HNP", observed, cost)
        return {"observed": observed, "measurement_errors": errors, "cost": cost, **score}, {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.glob("agent*"))
        }

    row, files = measured()
    traced = with_flow_trace(row, directory, ())

    assert traced["flow_trace"] is not None
    assert {key: value for key, value in traced.items() if key != "flow_trace"} == row
    assert measured() == (row, files)
    assert token_occurrences(directory, episode.token_probe) == 0
    rescored = score_episode(SCENARIOS["HN09"], "B2-HNP", traced["observed"], traced["cost"])
    assert rescored == {key: row[key] for key in rescored}


def test_a_block_that_cannot_be_built_is_null_and_the_row_stands(tmp_path: Path) -> None:
    row = {"scenario_id": "HN01", "metrics": {"oracle_match": 1}, "measurement_errors": {"x": "y"}}
    (tmp_path / "agent.db").write_bytes(b"not a database")

    traced = with_flow_trace(row, tmp_path, ())

    assert traced["flow_trace"] is None
    assert traced["measurement_errors"]["x"] == "y"
    assert traced["measurement_errors"]["flow_trace"] == "DatabaseError: file is not a database"
    assert {key: traced[key] for key in ("scenario_id", "metrics")} == {
        "scenario_id": "HN01",
        "metrics": {"oracle_match": 1},
    }


def test_a_row_whose_agent_never_started_has_no_block_and_no_failed_reading(
    tmp_path: Path,
) -> None:
    """A seed failure leaves no agent.db: the summary must not count it as a failed builder."""
    row = {"scenario_id": "HN01", "harness_error": "RuntimeError: seed failed"}

    traced = with_flow_trace(row, tmp_path / "never-started", ())

    assert traced == row | {"flow_trace": None}


def test_the_runner_writes_the_block_its_kept_artifact_gives_again(
    run_dir: Callable[[str], Path], tmp_path: Path
) -> None:
    directory = run_dir("b2_llm")
    kept = RunArtifacts(
        root=tmp_path / "runs", invocation_id="hnp-pilot-test", runner="hnp-pilot", provenance={}
    )
    row = {"scenario_id": "HN09", "condition": "B2-HNP", "observed": {"task": {}}}

    finished = finished_episode(kept, directory, row)

    assert finished["flow_trace"]["schema_version"] == SCHEMA
    assert finished["run_artifact"]["dir"] == "hnp-pilot-test/b2_llm"
    assert {key: finished[key] for key in row} == row
    # A pure function of the run's files: the kept copy gives the same block.
    assert trace_of(tmp_path / "runs" / "hnp-pilot-test" / "b2_llm") == finished["flow_trace"]

    failed = finished_episode(kept, tmp_path / "never-started", row)
    assert failed["flow_trace"] is None and failed["run_artifact"] is not None
    assert "flow_trace" not in (failed.get("measurement_errors") or {})
