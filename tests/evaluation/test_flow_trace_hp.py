"""The `flow_trace` block of a governance-pilot (HP) row (run-traceability.md §4.1, §4.5, T4).

Each run is one real Human Present turn through the app: the product's conversation, purchase, D1,
approval, D2 and completion services over one SQLite file and its LangGraph checkpointer, with the
merchant and the payment provider as doubles and the model as a router double or, for
`lookup_catalog`, the real SDK over a scripted `httpx.MockTransport`
(tests/support/model_server.py). No request leaves the process. The block is then read from the
stopped run's databases alone.
"""

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.application.conversation import turn_run_id
from commerce_agent.config import Settings
from commerce_agent.domain import HardConstraints, OptionConstraint, PurchaseIntent
from commerce_agent.domain.intent import SoftPreferences
from commerce_agent.llm import CandidateDecision, CommerceDecision, ConversationRouter
from commerce_evaluation.flow_trace import (
    SCHEMA,
    HpFlowTrace,
    build_hp_flow_trace,
    token_metrics,
)
from commerce_evaluation.redaction import REDACTED
from tests.governance.test_hp_continuation import StoreMerchant, approve
from tests.governance.test_hp_continuation import router as sonora_router
from tests.governance.test_runtime_dispatch import governed
from tests.support.model_server import ModelServer, function_call, structured
from tests.test_api import StubConversationRouter, checkout_details
from tests.test_governed_recovery import PHONES_PATH, PIXEL_8, PIXEL_8A, PhoneMerchant

API_KEY = "sk-test-flow-trace-hp-0123456789abcdef"
TURN_ID = "pilot-b2-s05-8-0123456789abcdef"
# The S05 request of evaluation/data/pilot-scenarios-v2.json.
S05_REQUEST = (
    "Quiero un celular negro y nuevo por maximo 1.300.000 COP. "
    "Elige la opcion mas barata que cumpla."
)
# H1 of run-traceability.md §3: the budget only as a total, so the shortlist keeps every price.
S05_INTENT = PurchaseIntent(
    hard=HardConstraints(
        currency="COP",
        max_total_amount_minor=130_000_000,
        categories=("smartphones",),
        conditions=("new",),
        required_options=(OptionConstraint(name="color", value="negro"),),
    ),
    preferences=SoftPreferences(selection_strategy="lowest_observed_price"),
)
ROUTED = CommerceDecision(
    action="purchase",
    assistant_message="Busco un celular negro y nuevo dentro de tu presupuesto.",
    purchase_request=S05_REQUEST,
    intent=S05_INTENT,
)
REASON = "Elijo el Google Pixel 8 porque es el que mejor cumple."
OVER_BUDGET = CandidateDecision(
    action="purchase", assistant_message=REASON, selected_variant_id=PIXEL_8
)
QUERY = "celular negro nuevo"
# In the phones feed but sold out, and an id no feed has: both outside the shortlist.
PIXEL_9 = "BBGooglePixel9-128GB"
UNKNOWN = "BBNotInTheFeed-1GB"
# No phone of the feed is red: the turn ends before any purchase.
RED = ROUTED.model_copy(
    update={
        "intent": S05_INTENT.model_copy(
            update={
                "hard": S05_INTENT.hard.model_copy(
                    update={"required_options": (OptionConstraint(name="color", value="rojo"),)}
                )
            }
        )
    }
)
JWT = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJidXllciJ9.c2lnbmF0dXJl"
BEARER = "Bearer " + "b" * 40
PEM = (
    "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg\n"
    "-----END PRIVATE KEY-----"
)
OPENAI_KEY = "sk-proj-" + "k" * 32


def checkpointed(directory: Path) -> Path:
    """The run's directory once its agent stopped: both databases without a pending WAL."""
    for name in ("agent.db", "agent.graph.db"):
        with closing(sqlite3.connect(directory / name)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return directory


def turn(client: TestClient, request: str = S05_REQUEST) -> Any:
    """The pilot's turn: one user message, no conversation id (`_execute_turn`). Its response is
    the test client's own, so it is not typed as `httpx.Response`."""
    return client.post(
        "/conversation/turn",
        json={
            "turn_id": TURN_ID,
            "messages": [{"role": "user", "content": request}],
            "details": checkout_details(),
        },
    )


def s05_app(
    directory: Path, conversation_router: ConversationRouter | None, **changes: Any
) -> TestClient:
    """S05 in B2 as the v2 pilot runs it: enforce, governed flow and b2-hp-v2, over the phones."""
    settings = governed(
        directory / "agent.db",
        agent_d1_policy_set_id="b2-hp-v2",
        agent_feed_path=PHONES_PATH,
        **changes,
    )
    merchant = PhoneMerchant()
    return TestClient(
        create_app(
            settings=settings,
            transport=httpx.MockTransport(merchant),
            payment_transport=httpx.MockTransport(merchant),
            conversation_router=conversation_router,
        )
    )


def s05_deny(directory: Path) -> Path:
    """S05 rep. 8 with a router double: the model picks the Pixel 8 over the total budget."""
    with s05_app(directory, StubConversationRouter(ROUTED, OVER_BUDGET)) as client:
        denied = turn(client)
    assert denied.status_code == 409 and denied.json()["code"] == "d1_denied", denied.text
    return checkpointed(directory)


def explained(router: StubConversationRouter) -> Callable[[Path], Path]:
    """A turn the governed recovery ends before any purchase: it explains, nothing is created."""

    def run(directory: Path) -> Path:
        with s05_app(directory, router) as client:
            response = turn(client)
        assert response.status_code == 200, response.text
        assert response.json()["purchase"] is None, response.text
        return checkpointed(directory)

    return run


def picking(variant_id: str) -> StubConversationRouter:
    pick = CandidateDecision(
        action="purchase", assistant_message=REASON, selected_variant_id=variant_id
    )
    return StubConversationRouter(ROUTED, pick)


def lookup(directory: Path) -> Path:
    """The same turn with the real router: it looks the catalog up before routing."""
    server = ModelServer(
        function_call("lookup_catalog", {"query": QUERY}),
        structured(ROUTED),
        structured(OVER_BUDGET),
    )
    with pytest.MonkeyPatch.context() as patch:
        server.install(patch)
        configured = {"anthropic_api_key": SecretStr(API_KEY), "agent_model": "test-model"}
        with s05_app(directory, None, **configured) as client:
            denied = turn(client)
    assert denied.status_code == 409 and denied.json()["code"] == "d1_denied", denied.text
    return checkpointed(directory)


def b2_purchase(directory: Path) -> Path:
    """S01 in B2 with b2-hp-v2: D1 allows, the buyer approves (D2 allows) and an order exists."""
    settings: Settings = governed(directory / "agent.db", agent_d1_policy_set_id="b2-hp-v2")
    merchant = StoreMerchant()
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json={"id": "vt_" + "a" * 64})
        ),
        conversation_router=sonora_router(),
    )
    with TestClient(app) as client:
        prepared = turn(client, "Quiero los audífonos disponibles")
        assert prepared.status_code == 200, prepared.text
        run_id = prepared.json()["purchase"]["run_id"]
        assert approve(client, run_id).status_code == 200
        assert client.post(f"/purchases/{run_id}/complete").status_code == 200
    event = {"type": "order_create", "data": {"type": "order", "id": "ord_api_1"}}
    with closing(sqlite3.connect(directory / "agent.db")) as connection, connection:
        connection.execute(
            "INSERT INTO webhook_events (payload_sha256, request_id, event_type, order_id, "
            "payload, received_at) VALUES (?, 'whd_1', 'order_create', 'ord_api_1', ?, "
            "'2026-10-21T15:00:00+00:00')",
            ("c" * 64, json.dumps(event)),
        )
    return checkpointed(directory)


RUNS: dict[str, Callable[[Path], Path]] = {
    "s05_deny": s05_deny,
    "lookup": lookup,
    "b2_purchase": b2_purchase,
    "sold_out_pick": explained(picking(PIXEL_9)),
    "unknown_pick": explained(picking(UNKNOWN)),
    "no_candidate": explained(StubConversationRouter(RED)),
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
def run_dir(built: Callable[[str], Path], tmp_path: Path) -> Callable[..., Path]:
    """A copy of the run's directory as the pilot leaves it: agent.db and agent.graph.db."""

    def copy(name: str, target: Path | None = None) -> Path:
        directory = target or tmp_path / name
        directory.mkdir(parents=True)
        for database in ("agent.db", "agent.graph.db"):
            shutil.copyfile(built(name) / database, directory / database)
        return directory

    return copy


def trace_of(directory: Path, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    return build_hp_flow_trace(directory / "agent.db", directory / "agent.graph.db", secrets)


def execute(directory: Path, statement: str, *values: Any) -> None:
    with closing(sqlite3.connect(directory / "agent.db")) as connection, connection:
        connection.execute(statement, values)


def without(value: dict[str, Any], *names: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in names}


def test_s05_shows_how_the_model_read_the_budget_what_it_saw_and_what_d1_denied(
    run_dir: Callable[[str], Path],
) -> None:
    trace = trace_of(run_dir("s05_deny"))

    assert HpFlowTrace.model_validate(trace).schema_version == SCHEMA == "flow-trace/1"
    # The denied purchase stays `received` and still names the run.
    assert trace["run_ids"] == [turn_run_id(TURN_ID)]
    [step] = trace["turns"]
    assert step["run_id"] == turn_run_id(TURN_ID)
    # How it read «por maximo 1.300.000 COP»: a total, no unit cap (H1).
    intent = step["intent"]
    assert (intent["action"], intent["purchase_request"]) == ("purchase", S05_REQUEST)
    assert intent["hard"]["max_total_amount_minor"] == 130_000_000
    assert intent["hard"]["max_unit_amount_minor"] is None
    assert intent["preferences"]["selection_strategy"] == "lowest_observed_price"
    # What it saw: without a unit cap every phone in stock stays, the cheapest first.
    assert [(item["variant_id"], item["unit_minor"]) for item in step["shortlist"]] == [
        (PIXEL_8A, 89_900_000),
        (PIXEL_8, 154_000_000),
    ]
    assert step["shortlist"][1] == {
        "variant_id": PIXEL_8,
        "title": "Google Pixel 8",
        "variant_title": "Google Pixel 8 128 GB negro",
        "unit_minor": 154_000_000,
        "currency": "COP",
        "preference_score": step["shortlist"][1]["preference_score"],
        "matched_preferences": step["shortlist"][1]["matched_preferences"],
    }
    assert step["rejection_counts"] == {"unavailable": 1}
    # What it chose and why.
    assert step["choice"] == {
        "action": "purchase",
        "selected_variant_id": PIXEL_8,
        "message": REASON,
    }
    assert step["lookup_catalog"] is None
    # What D1 decided, on which input.
    [d1] = trace["decisions"]
    assert (d1["phase"], d1["record_kind"], d1["outcome"], d1["reason_codes"]) == (
        "D1",
        "decision_record",
        "deny",
        ["BUDGET_NOT_SATISFIABLE"],
    )
    assert [(rule["policy_id"], rule["result"]) for rule in d1["rule_results"]] == [
        ("POL-01", "pass"),
        ("POL-02", "pass"),
        ("POL-11", "fail"),
    ]
    assert d1["input"]["selected_variant_id"] == PIXEL_8
    assert d1["input"]["selected_unit_minor"] == 154_000_000
    assert d1["input"]["intent"]["max_total_minor"] == 130_000_000
    assert d1["input"]["intent"]["max_unit_minor"] is None
    assert sorted(d1["input"]["eligible_variant_ids"]) == sorted([PIXEL_8, PIXEL_8A])
    # Nothing was created, approved or ordered; the stage that failed is named.
    assert trace["checkout"] == {
        "checkout_id": None,
        "purchase_stage": "received",
        "failure_code": None,
        "total_minor": None,
        "currency": None,
    }
    assert (trace["approval"], trace["order"], trace["webhooks"]) == (None, None, [])
    assert (trace["freshness"], trace["recovery"]) == ([], [])
    assert {"stage": "d1.create_checkout", "code": None, "source": "stage_timings"} in (
        trace["errors"]
    )
    # A router double sends no request to a model.
    assert trace["model_requests"] == [] and trace["unavailable"] == []
    assert token_metrics(trace) == {"model_input_tokens": 0, "model_output_tokens": 0}


def test_a_turn_that_looked_the_catalog_up_records_the_query_and_its_results(
    run_dir: Callable[[str], Path],
) -> None:
    trace = trace_of(run_dir("lookup"))

    [step] = trace["turns"]
    assert step["lookup_catalog"] == {
        "query": QUERY,
        "result_variant_ids": step["lookup_catalog"]["result_variant_ids"],
    }
    # Both phones in stock are black: «negro» matches each of them.
    assert sorted(step["lookup_catalog"]["result_variant_ids"]) == sorted([PIXEL_8, PIXEL_8A])
    assert step["choice"] == {
        "action": "purchase",
        "selected_variant_id": PIXEL_8,
        "message": REASON,
    }
    assert step["intent"]["hard"]["max_total_amount_minor"] == 130_000_000
    # The three requests of the turn, under its run, with their usage.
    requests = trace["model_requests"]
    assert [(item["run_id"], item["stage"], item["request_index"]) for item in requests] == [
        (turn_run_id(TURN_ID), "model.route", 1),
        (turn_run_id(TURN_ID), "model.route", 2),
        (turn_run_id(TURN_ID), "model.choose_candidate", 1),
    ]
    assert requests[0]["output"]["function_calls"] == [
        {"name": "lookup_catalog", "arguments": json.dumps({"query": QUERY})}
    ]
    assert {(item["input_tokens"], item["output_tokens"]) for item in requests} == {(1825, 786)}
    assert token_metrics(trace) == {
        "model_input_tokens": 3 * 1825,
        "model_output_tokens": 3 * 786,
    }


def test_a_turn_without_the_tool_has_no_lookup_and_an_unknown_usage_has_no_token_sum(
    run_dir: Callable[[str], Path],
) -> None:
    """A failed call has no usage: the row's token metrics are unknown, not a partial sum."""
    directory = run_dir("lookup")
    execute(
        directory,
        "UPDATE model_requests SET input_json = '[]', outcome = 'error', error_kind = "
        "'APIConnectionError', input_tokens = NULL, output_tokens = NULL WHERE request_index = 2",
    )

    trace = trace_of(directory)

    assert trace["turns"][0]["lookup_catalog"] is None
    assert token_metrics(trace) == {"model_input_tokens": None, "model_output_tokens": None}
    assert token_metrics(None) == {"model_input_tokens": None, "model_output_tokens": None}
    assert {"stage": "model.route", "code": "APIConnectionError", "source": "model_requests"} in (
        trace["errors"]
    )


def test_a_b2_purchase_shows_both_decisions_the_approval_and_the_order(
    run_dir: Callable[[str], Path],
) -> None:
    trace = trace_of(run_dir("b2_purchase"))

    HpFlowTrace.model_validate(trace)
    [step] = trace["turns"]
    assert step["choice"] == {
        "action": "purchase",
        "selected_variant_id": "SON-01",
        "message": "Elegí Sonora.",
    }
    assert [(item["phase"], item["outcome"]) for item in trace["decisions"]] == [
        ("D1", "allow"),
        ("D2", "allow"),
    ]
    d2 = trace["decisions"][1]
    assert [(rule["policy_id"], rule["result"]) for rule in d2["rule_results"]] == [
        ("POL-03", "pass"),
        ("POL-04", "pass"),
        ("POL-05", "pass"),
        ("POL-06", "pass"),
    ]
    assert d2["input"]["terms"]["total_minor"] == 74_000
    assert d2["input_digest"].startswith("sha256:")
    # The turn presents the terms, reading them before approving presents them again.
    assert [item["stage"] for item in trace["freshness"]] == ["present", "present", "approve"]
    assert trace["checkout"] == {
        "checkout_id": "cs_api_1",
        "purchase_stage": "completed",
        "failure_code": None,
        "total_minor": 74_000,
        # As the presented terms recorded it (ACP's lowercase code).
        "currency": "cop",
    }
    approval = trace["approval"]
    assert approval["fingerprint"] and approval["approved_at"]
    # Without an AP2 signer the approval carries no mandate; only their presence is ever read.
    assert approval["mandates"] == {"checkout": False, "payment": False}
    assert trace["order"] == {"order_id": "ord_api_1", "status": "confirmed"}
    assert [hook["event_type"] for hook in trace["webhooks"]] == ["order_create"]
    assert trace["errors"] == [] and trace["unavailable"] == []
    # Two decisions with their inputs: well under the bound of §4.1 for a row.
    assert len(json.dumps(trace, ensure_ascii=False)) < 25_000


def test_secrets_never_leave_their_columns_and_seeded_shapes_end_redacted(
    run_dir: Callable[[str], Path],
) -> None:
    directory = run_dir("b2_purchase")
    secret = "invocation-secret-" + "x" * 24
    # Columns the block never reads, each with a plain value no shape sweep would catch: only the
    # whitelist keeps them out.
    forbidden = {
        "UPDATE approvals SET checkout_jwt = ?": "never-read-checkout-jwt",
        "UPDATE approvals SET checkout_mandate = ?": "never-read-checkout-mandate",
        "UPDATE approvals SET payment_mandate = ?": "never-read-payment-mandate",
        "UPDATE completion_results SET checkout_receipt_jwt = ?": "never-read-checkout-receipt",
        "UPDATE completion_results SET payment_receipt_jwt = ?": "never-read-payment-receipt",
        "UPDATE approval_terms SET terms = json_set(terms, '$.checkout_jwt', ?)": (
            "never-read-presented-jwt"
        ),
    }
    for statement, value in forbidden.items():
        execute(directory, statement, value)
    # Secret shapes in columns it does read.
    leaked = f"{JWT} {BEARER} {PEM} {OPENAI_KEY} {secret}"
    execute(directory, "UPDATE webhook_events SET event_type = ?", f"order_create {leaked}")
    execute(
        directory,
        "INSERT INTO model_requests (run_id, stage, request_index, started_at, latency_ms, "
        "outcome, error_kind, model, output_json) VALUES (NULL, 'model.route', 1, "
        "'2026-10-21T15:00:00Z', 1.0, 'error', ?, 'test-model', ?)",
        f"Error {secret}",
        json.dumps({"structured": {"assistant_message": leaked}, "function_calls": []}),
    )

    trace = trace_of(directory, (secret,))

    text = json.dumps(trace, ensure_ascii=False)
    with closing(sqlite3.connect(directory / "agent.db")) as connection:
        kept = [
            value
            for query in (
                "SELECT signature FROM approvals",
                "SELECT idempotency_key FROM operation_keys",
                "SELECT idempotency_key FROM d1_checkout_operations",
            )
            for (value,) in connection.execute(query)
        ]
        (terms,) = connection.execute("SELECT terms FROM approval_terms").fetchone()
    # The presented terms hold the buyer's details; the block reads only their total and currency.
    fulfillment = checkout_details()["fulfillment"]
    buyer = [fulfillment["email"], fulfillment["phone_number"], fulfillment["address"]["line_one"]]
    assert all(value in terms for value in buyer)
    assert len(kept) >= 3
    assert trace["approval"]["mandates"] == {"checkout": True, "payment": True}
    for value in (*forbidden.values(), *kept, *buyer):
        assert value not in text
    for value in ("eyJ", "b" * 40, "BEGIN PRIVATE KEY", "MIGHAgEAMBMG", OPENAI_KEY, secret):
        assert value not in text
    [hook] = trace["webhooks"]
    assert hook["event_type"].count(REDACTED) == 5
    assert {"stage": "model.route", "code": f"Error {REDACTED}", "source": "model_requests"} in (
        trace["errors"]
    )


@pytest.mark.parametrize(
    ("run", "kind", "cause", "variant_id", "picked"),
    [
        ("sold_out_pick", "selection_outside_shortlist", "unavailable", PIXEL_9, PIXEL_9),
        # Not in the feed: the record keeps only the digest of the id the model gave, which is
        # in the output of its `model.choose_candidate` request.
        ("unknown_pick", "selection_outside_shortlist", "not_in_catalog", None, UNKNOWN),
        ("no_candidate", "no_candidate", "no_match", None, None),
    ],
)
def test_a_recovery_that_ends_the_turn_before_any_purchase_is_in_the_block(
    run_dir: Callable[..., Path],
    run: str,
    kind: str,
    cause: str,
    variant_id: str | None,
    picked: str | None,
) -> None:
    """The recovery is the turn's evidence, under its run, though no purchase row names it."""
    directory = run_dir(run)
    trace = trace_of(directory)

    run_id = turn_run_id(TURN_ID)
    [step] = trace["turns"]
    assert trace["run_ids"] == [step["run_id"]] == [run_id]
    [recovery] = trace["recovery"]
    assert (recovery["kind"], recovery["cause"], recovery["run_id"]) == (kind, cause, run_id)
    assert recovery["variant_id"] == variant_id
    digest = picked and hashlib.sha256(picked.encode()).hexdigest()
    assert recovery["selected_variant_sha256"] == digest
    # No pick the shortlist backs, and nothing D1 or the merchant saw: D1 never ran.
    assert step["choice"] is None
    assert trace["checkout"] is None and trace["decisions"] is None
    # Without the graph, the application record still names the run.
    (directory / "agent.graph.db").unlink()
    blind = trace_of(directory)
    assert blind["turns"] is None
    assert (blind["run_ids"], blind["recovery"]) == (trace["run_ids"], trace["recovery"])


def test_without_the_graph_database_the_turns_are_null_and_named(
    run_dir: Callable[[str], Path],
) -> None:
    directory = run_dir("s05_deny")
    (directory / "agent.graph.db").unlink()

    trace = trace_of(directory)

    assert trace["turns"] is None and trace["unavailable"] == ["agent.graph.db"]
    assert trace["decisions"][0]["outcome"] == "deny"
