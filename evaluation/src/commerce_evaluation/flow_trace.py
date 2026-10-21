"""The `flow_trace` block of a pilot row (docs/design/run-traceability.md §4.1 and §4.2, T3).

`build_flow_trace` explains one run from its stopped agent's databases alone, opened read only:
the delegation the task executed (scope, derived intent, the shortlist the selector saw and its
choice), each governance decision with its input and its rule results, the freshness checks, the
reservation, the dispatch and the order, the errors with their stage, and each model request. It
is a pure function of those files, so it runs again on a kept run artifact (T1) and gives the
same block.

The block is a whitelist by construction: it reads named columns only, never a mandate or receipt
JWT, an idempotency key, the buyer's checkout details, an AP2 envelope, the encrypted D1
provenance or a key. A final sweep redacts the invocation's secrets and the secret shapes in every
value, and fails closed: a secret it cannot remove (in a key) raises, and the runner writes
`flow_trace: null` with the cause in `measurement_errors.flow_trace`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from commerce_agent.acp.feed import load_product_feed
from commerce_agent.application.hnp_selection import MODEL_STAGE
from commerce_agent.domain import PurchaseIntent, rank_candidates
from commerce_agent.llm.prices import format_minor_amount
from commerce_evaluation.pilot_measures import read_only
from commerce_evaluation.redaction import redact, sweep_fields

SCHEMA = "flow-trace/1"
# An ObjectRef of a canonical input: the block keeps its id, and `input_digest` binds the rest.
REFERENCE_KEYS = frozenset({"id", "digest", "schema_version", "version"})


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShortlistItem(_Block):
    variant_id: str
    product_title: str | None
    variant_title: str | None
    display_amount: str | None
    preference_score: int | None
    matched_preferences: list[str]


class Choice(_Block):
    # `no_model_call`: the deterministic selector or HN11's scripted one, which record nothing.
    selector: Literal["llm", "no_model_call"]
    # None when the choice left no record (B0-HNP's guard ends the task before `start`).
    selected_variant_id: str | None
    message: str | None


class Delegation(_Block):
    grant_id: str
    grant_state: str
    revoked_reason: str | None
    task_id: str | None
    task_state: str | None
    claim_count: int | None
    scope: dict[str, Any]
    preference_text: str | None
    intent: dict[str, Any]
    shortlist_source: Literal["model_request", "recomputed"] | None
    shortlist: list[ShortlistItem] | None
    choice: Choice


class RuleResult(_Block):
    policy_id: str
    result: str


class Decision(_Block):
    phase: str
    record_kind: str
    decision_id: str
    evidence_id: str
    outcome: str | None
    origin: str | None
    reason_codes: list[str]
    rule_results: list[RuleResult]
    input_digest: str | None
    # The canonical input with each reference reduced to its id and without `schema_version`.
    input: dict[str, Any] | None


class Checkout(_Block):
    checkout_id: str | None
    purchase_stage: str
    failure_code: str | None


class Reservation(_Block):
    state: str
    amount_minor: int
    currency: str
    release_basis: str | None
    uncertain_reason: str | None


class Dispatch(_Block):
    state: str
    rejected_by: str | None
    ap2_error: str | None
    amount_minor: int
    order_id: str | None


class Order(_Block):
    order_id: str
    status: str | None


class Webhook(_Block):
    event_type: str
    order_id: str
    received_at: str
    payload_sha256: str


class FlowError(_Block):
    stage: str
    code: str | None
    source: str


class ModelRequest(_Block):
    run_id: str | None
    stage: str | None
    request_index: int
    outcome: str
    latency_ms: float
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    retries: int | None
    # A failed call (an invalid structured output among them) has no usage: kept, never dropped.
    without_tokens: bool
    error_kind: str | None
    request_sha256: str | None
    output: dict[str, Any] | None


class FlowTrace(_Block):
    schema_version: Literal["flow-trace/1"]
    run_ids: list[str]
    delegation: Delegation | None
    # A list is null when its table is missing, and the table is named in `unavailable`.
    decisions: list[Decision] | None
    freshness: list[dict[str, Any]] | None
    checkout: Checkout | None
    reservation: Reservation | None
    dispatch: Dispatch | None
    order: Order | None
    webhooks: list[Webhook] | None
    errors: list[FlowError]
    model_requests: list[ModelRequest] | None
    unavailable: list[str]


class _Database:
    """Named columns of a run's agent database. A missing table reads as no rows and is named in
    `unavailable`: B0-HNP never creates the governance stores, an older agent `model_requests`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.unavailable: list[str] = []

    def has(self, table: str) -> bool:
        if table not in self._tables and table not in self.unavailable:
            self.unavailable.append(table)
        return table in self._tables

    def rows(self, table: str, query: str, *values: Any) -> list[dict[str, Any]]:
        if not self.has(table):
            return []
        cursor = self._connection.execute(query, values)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor]

    def per_run(self, table: str, query: str, run_ids: list[str]) -> list[dict[str, Any]] | None:
        """`query` (with one `run_id = ?`) for each run, in order; None without the table."""
        if not self.has(table):
            return None
        return [row for run_id in run_ids for row in self.rows(table, query, run_id)]


def build_flow_trace(
    agent_db: Path, graph_db: Path | None, secrets: Iterable[str], *, feed: Path | None = None
) -> dict[str, Any]:
    """The run's `flow_trace`, swept. `graph_db` is the conversation checkpointer: an HNP task
    has no conversation, so nothing is read from it until the HP block adds `turns` (T4). `feed`
    is the run's feed directory, to recompute a shortlist no model request recorded."""
    with closing(read_only(agent_db)) as connection:
        database = _Database(connection)
        tasks = database.rows(
            "hnp_tasks",
            "SELECT task_id, grant_id, run_id, state, outcome_code, claim_count FROM hnp_tasks "
            "ORDER BY created_at",
        )
        run_ids = [task["run_id"] for task in tasks if task["run_id"]]
        requests = _model_requests(database)
        timings = database.rows(
            "stage_timings", "SELECT stage, outcome FROM stage_timings ORDER BY sequence"
        )
        evidence = database.per_run(
            "governance_evidence",
            "SELECT phase, record_kind, decision_id, evidence_id, input_digest, canonical_input, "
            "body FROM governance_evidence WHERE run_id = ? ORDER BY sequence",
            run_ids,
        )
        decisions = None if evidence is None else [_decision(row) for row in evidence]
        purchase = _last(
            database.per_run(
                "purchases",
                "SELECT checkout_id, stage AS purchase_stage, failure_code, selected_variant_id "
                "FROM purchases WHERE run_id = ?",
                run_ids,
            )
        )
        dispatch = _last(
            database.per_run(
                "dispatch_outbox",
                "SELECT state, rejected_by, ap2_error, amount_minor, order_id FROM dispatch_outbox "
                "WHERE run_id = ?",
                run_ids,
            )
        )
        trace = {
            "schema_version": SCHEMA,
            "run_ids": run_ids,
            "delegation": _delegation(
                database, tasks, requests, timings, _recorded_choice(purchase, decisions), feed
            ),
            "decisions": decisions,
            "freshness": _freshness(database, run_ids),
            "checkout": purchase and _without(purchase, "selected_variant_id"),
            "reservation": _last(
                database.per_run(
                    "grant_reservations",
                    "SELECT state, amount_minor, currency, release_basis, uncertain_reason "
                    "FROM grant_reservations WHERE run_id = ?",
                    run_ids,
                )
            ),
            "dispatch": dispatch,
            "order": _order(database, (dispatch or {}).get("order_id")),
            "webhooks": _webhooks(database),
            "errors": _errors(database, tasks, run_ids, timings, requests, decisions, dispatch),
            "model_requests": None if requests is None else [_request(row) for row in requests],
        }
        trace["unavailable"] = database.unavailable
    return _swept(FlowTrace.model_validate(trace).model_dump(mode="json"), list(secrets))


def with_flow_trace(
    row: Mapping[str, Any], run_dir: Path, secrets: Iterable[str]
) -> dict[str, Any]:
    """The row plus its `flow_trace`. The runner calls it once the agent stopped and the row was
    scored (C8 included), and it only adds keys: a block that cannot be built is null, its cause
    goes to `measurement_errors.flow_trace`, and the row stands."""
    try:
        trace = build_flow_trace(
            run_dir / "agent.db", run_dir / "agent.graph.db", secrets, feed=run_dir / "feed"
        )
    except Exception as error:
        errors = dict(row.get("measurement_errors") or {})
        errors["flow_trace"] = f"{type(error).__name__}: {error}"
        return dict(row) | {"flow_trace": None, "measurement_errors": errors}
    return dict(row) | {"flow_trace": trace}


def _last(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    return rows[-1] if rows else None


def _without(row: Mapping[str, Any], name: str) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != name}


def _model_requests(database: _Database) -> list[dict[str, Any]] | None:
    """Every request of the run's agent (one run per database); `input_json` is read only for the
    shortlist and never copied."""
    if not database.has("model_requests"):
        return None
    return database.rows(
        "model_requests",
        "SELECT run_id, stage, request_index, outcome, latency_ms, input_tokens, "
        "cached_input_tokens, output_tokens, reasoning_tokens, retries, error_kind, "
        "request_sha256, input_json, output_json FROM model_requests ORDER BY sequence",
    )


def _request(row: Mapping[str, Any]) -> dict[str, Any]:
    output = row["output_json"]
    return _without(_without(row, "input_json"), "output_json") | {
        "without_tokens": row["input_tokens"] is None or row["output_tokens"] is None,
        "output": None if output is None else json.loads(output),
    }


def _delegation(
    database: _Database,
    tasks: list[dict[str, Any]],
    requests: list[dict[str, Any]] | None,
    timings: list[dict[str, Any]],
    recorded_choice: str | None,
    feed: Path | None,
) -> dict[str, Any] | None:
    grants = database.rows(
        "delegation_grants",
        "SELECT grant_id, state, revoked_reason, scope_json, intent_json FROM delegation_grants",
    )
    if not grants:
        return None
    if len(grants) > 1:
        raise ValueError(f"An HNP run holds one grant, this one holds {len(grants)}")
    [grant] = grants
    consent = json.loads(grant["scope_json"])
    task = next((item for item in tasks if item["grant_id"] == grant["grant_id"]), {})
    selection = next(
        (
            row
            for row in reversed(requests or [])
            if row["stage"] == MODEL_STAGE and row["run_id"] == task.get("run_id")
        ),
        None,
    )
    if selection is not None and selection["input_json"]:
        source = "model_request"
        shortlist = [
            _shortlisted(item) for item in json.loads(selection["input_json"])["candidates"]
        ]
    elif task.get("run_id") is None:
        source, shortlist = None, None  # never claimed: no shortlist was computed
    elif feed is not None and feed.is_dir():
        source = "recomputed"
        shortlist = _recomputed(feed, grant["intent_json"], consent["scope"]["variant_ids"])
    else:
        source, shortlist = None, None
        database.unavailable.append("feed")
    answer = json.loads((selection or {}).get("output_json") or "{}").get("structured") or {}
    timed = any(row["stage"] == MODEL_STAGE for row in timings)
    return {
        "grant_id": grant["grant_id"],
        "grant_state": grant["state"],
        "revoked_reason": grant["revoked_reason"],
        "task_id": task.get("task_id"),
        "task_state": task.get("state"),
        "claim_count": task.get("claim_count"),
        "scope": consent["scope"],
        "preference_text": consent.get("preference_text"),
        "intent": json.loads(grant["intent_json"]),
        "shortlist_source": source,
        "shortlist": shortlist,
        "choice": {
            "selector": "llm" if selection is not None or timed else "no_model_call",
            "selected_variant_id": answer.get("selected_variant_id")
            if selection is not None
            else recorded_choice,
            "message": answer.get("assistant_message"),
        },
    }


def _recorded_choice(
    purchase: Mapping[str, Any] | None, decisions: list[dict[str, Any]] | None
) -> str | None:
    """The variant a selector without a model request chose, as the purchase or D1-HNP's input
    recorded it (a D1 deny leaves the purchase `received` without it)."""
    inputs = [item["input"] for item in decisions or [] if item["phase"] == "D1" and item["input"]]
    return (purchase or {}).get("selected_variant_id") or next(
        (item.get("selected_variant_id") for item in inputs), None
    )


def _shortlisted(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """A candidate of the selector's payload, field by field."""
    return {name: candidate.get(name) for name in ShortlistItem.model_fields} | {
        "matched_preferences": list(candidate.get("matched_preferences") or [])
    }


def _recomputed(feed: Path, intent_json: str, granted: list[str]) -> list[dict[str, Any]]:
    """The executor's shortlist: `rank_candidates` on the run's feed, restricted to the grant."""
    intent = PurchaseIntent.model_validate_json(intent_json)
    return [
        {
            "variant_id": item.candidate.variant_id,
            "product_title": item.candidate.product_title,
            "variant_title": item.candidate.variant_title,
            "display_amount": format_minor_amount(
                item.candidate.observed_unit_amount_minor, item.candidate.currency
            ),
            "preference_score": item.preference_score,
            "matched_preferences": list(item.matched_preferences),
        }
        for item in rank_candidates(load_product_feed(feed), intent).ranked
        if item.candidate.variant_id in granted
    ]


def _decision(row: Mapping[str, Any]) -> dict[str, Any]:
    body = json.loads(row["body"])
    decision = body.get("decision") or {}
    # An input_rejected or application_failure record has a reason code and no decision.
    failure = [body["reason_code"]] if body.get("reason_code") else []
    return {
        "phase": row["phase"],
        "record_kind": row["record_kind"],
        "decision_id": row["decision_id"],
        "evidence_id": row["evidence_id"],
        "outcome": decision.get("outcome"),
        "origin": decision.get("origin"),
        "reason_codes": decision.get("reason_codes") or failure,
        "rule_results": [
            {"policy_id": rule["policy_id"], "result": rule["result"]}
            for rule in decision.get("rule_results", [])
        ],
        "input_digest": row["input_digest"],
        "input": None
        if row["canonical_input"] is None
        else _compact(json.loads(row["canonical_input"])),
    }


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        if REFERENCE_KEYS.issuperset(value) and {"id", "digest"} <= value.keys():
            return value["id"]
        return {key: _compact(item) for key, item in value.items() if key != "schema_version"}
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def _freshness(database: _Database, run_ids: list[str]) -> list[dict[str, Any]] | None:
    checks = database.per_run(
        "hp_evidence",
        "SELECT body FROM hp_evidence WHERE run_id = ? AND record_kind = 'freshness_check' "
        "ORDER BY sequence",
        run_ids,
    )
    return None if checks is None else [json.loads(row["body"]) for row in checks]


def _order(database: _Database, order_id: str | None) -> dict[str, Any] | None:
    if order_id is None:
        return None
    found = database.rows("orders", "SELECT status FROM orders WHERE order_id = ?", order_id)
    return {"order_id": order_id, "status": found[0]["status"] if found else None}


def _webhooks(database: _Database) -> list[dict[str, Any]] | None:
    if not database.has("webhook_events"):
        return None
    return database.rows(
        "webhook_events",
        "SELECT event_type, order_id, received_at, payload_sha256 FROM webhook_events "
        "ORDER BY received_at",
    )


def _errors(
    database: _Database,
    tasks: list[dict[str, Any]],
    run_ids: list[str],
    timings: list[dict[str, Any]],
    requests: list[dict[str, Any]] | None,
    decisions: list[dict[str, Any]] | None,
    dispatch: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Each error with the stage it happened in, from every table that records one."""
    checkouts = database.per_run(
        "d1_checkout_results",
        "SELECT result.code FROM d1_checkout_results result JOIN d1_checkout_operations operation "
        "ON operation.operation_id = result.operation_id WHERE operation.run_id = ? "
        "AND result.code != 'CHECKOUT_VERIFIED'",
        run_ids,
    )
    errors = [
        *(
            _error("hnp.task", task["outcome_code"], "hnp_tasks")
            for task in tasks
            if task["outcome_code"]
        ),
        *(
            _error("d1.create_checkout", row["code"], "d1_checkout_results")
            for row in checkouts or []
        ),
        *(
            _error(row["phase"], code, "governance_evidence")
            for row in decisions or []
            if row["record_kind"] != "decision_record"
            for code in row["reason_codes"]
        ),
        *(
            _error(row["stage"], None, "stage_timings")
            for row in timings
            if row["outcome"] == "error"
        ),
        *(
            _error(row["stage"] or "model", row["error_kind"], "model_requests")
            for row in requests or []
            if row["outcome"] == "error"
        ),
    ]
    if dispatch is not None and dispatch["ap2_error"] is not None:
        source = f"dispatch_outbox.{dispatch['rejected_by']}"
        errors.append(_error("dispatch", dispatch["ap2_error"], source))
    return errors


def _error(stage: str, code: str | None, source: str) -> dict[str, Any]:
    return {"stage": stage, "code": code, "source": source}


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _swept(block: dict[str, Any], secrets: list[str]) -> dict[str, Any]:
    """Every value redacted (redaction.py); a secret the sweep cannot remove fails closed."""
    clean = sweep_fields({"flow_trace": block}, ("flow_trace",), secrets)["flow_trace"]
    if any(redact(text, secrets) != text for text in _strings(clean)):
        raise ValueError("The flow_trace keeps a secret or a secret shape the sweep cannot remove")
    return clean
