"""`conditional_decisions`: the append-only record of each conditional decision (spec §3.7).

It exists in every mode, HP and HNP, because `SqliteAgentStore.initialize()` creates it; the
oracle reads it like block A reads `model_requests`. Bodies are canonical JSON with no buyer
data, tokens or model text.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.application.conditional import (
    EXCLUSIONS_ONLY,
    ConditionalClarification,
    ResolvedConditional,
    conditional_intent_sha256,
    decision_record,
    evaluate_branches,
    hard_sha256,
    record_decision,
    resolve_branches,
    verify_checkout,
)
from commerce_agent.domain.conditional import ConditionalIntent, PurchaseBranch
from commerce_agent.domain.intent import HardConstraints
from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.conditional_sqlite import ConditionalEntry
from tests.support.conditional import (
    SEARCH,
    T_A,
    T_B,
    A,
    B,
    FakeOfferReader,
    acp_error,
    checkout,
    match_b,
    offer,
    phone_feed,
    ux03,
)


@pytest.fixture
def store(tmp_path: Path) -> SqliteAgentStore:
    created = SqliteAgentStore(tmp_path / "agent.db")
    created.initialize()
    return created


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def entry(outcome: str = "none", **fields: object) -> ConditionalEntry:
    return ConditionalEntry(
        kind=fields.pop("kind", "decision"),  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        branch_index=fields.pop("branch_index", None),  # type: ignore[arg-type]
        variant_id=fields.pop("variant_id", None),  # type: ignore[arg-type]
        body=fields.pop("body", {"schema": "conditional-decision/1"}),  # type: ignore[arg-type]
    )


def test_initialize_creates_the_table_in_every_store_and_is_idempotent(tmp_path: Path) -> None:
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    store.initialize()
    with closing(connect(tmp_path / "agent.db")) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert {
        "conditional_decisions",
        "conditional_decisions_sequence",
        "conditional_decisions_no_update",
        "conditional_decisions_no_delete",
    } <= names


def test_a_database_with_the_old_schema_gains_the_table_and_keeps_its_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.db"
    with closing(connect(path)) as db, db:
        db.execute(
            """CREATE TABLE purchases (
            run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL DEFAULT 'local-human',
            request_text TEXT NOT NULL, stage TEXT NOT NULL, selected_variant_id TEXT,
            checkout_id TEXT UNIQUE, order_id TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)"""
        )
        db.execute(
            "INSERT INTO purchases(run_id, request_text, stage, created_at, updated_at) "
            "VALUES ('old', 'historic request', 'completed', 'c', 'u')"
        )
        db.execute("CREATE TABLE hp_evidence (body TEXT)")
        db.execute("INSERT INTO hp_evidence VALUES ('{}')")
        before = db.execute("SELECT * FROM purchases").fetchall()
    migrated = SqliteAgentStore(path)
    migrated.initialize()
    migrated.initialize()
    assert migrated.conditional_records("old") == ()
    migrated.append_conditional("old", entry())
    with closing(connect(path)) as db:
        columns = "run_id, owner_id, request_text, stage, selected_variant_id, checkout_id, "
        columns += "order_id, created_at, updated_at"
        assert db.execute(f"SELECT {columns} FROM purchases").fetchall() == before
        assert db.execute("SELECT body FROM hp_evidence").fetchall() == [("{}",)]
        assert db.execute("SELECT count(*) FROM conditional_decisions").fetchone() == (1,)


def test_records_append_in_sequence_per_run_and_the_last_decision_is_found(
    store: SqliteAgentStore,
) -> None:
    first = store.append_conditional("run-1", entry("undetermined"))
    second = store.append_conditional("run-1", entry("branch", branch_index=1, variant_id=B))
    check = store.append_conditional(
        "run-1",
        entry("checkout_matches", kind="checkout_check", branch_index=1, variant_id=B),
    )
    other = store.append_conditional("run-2", entry("none"))
    assert [record.sequence for record in (first, second, check, other)] == [1, 2, 3, 1]
    assert [record.outcome for record in store.conditional_records("run-1")] == [
        "undetermined",
        "branch",
        "checkout_matches",
    ]
    latest = store.latest_conditional_decision("run-1")
    assert latest is not None and (latest.sequence, latest.variant_id) == (2, B)
    assert store.latest_conditional_decision("missing") is None
    assert second.recorded_at.endswith("Z")


def test_the_table_is_append_only_for_every_writer(store: SqliteAgentStore, tmp_path: Path) -> None:
    store.append_conditional("run-1", entry())
    with closing(connect(tmp_path / "agent.db")) as db:
        for statement in (
            "UPDATE conditional_decisions SET outcome = 'branch'",
            "DELETE FROM conditional_decisions",
            "INSERT OR REPLACE INTO conditional_decisions "
            "SELECT run_id, sequence, kind, 'clarify', branch_index, variant_id, body, "
            "recorded_at FROM conditional_decisions",
            "INSERT INTO conditional_decisions VALUES "
            "('run-1', 5, 'decision', 'none', NULL, NULL, '{}', 't')",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="conditional_decisions"):
                db.execute(statement)
    assert [record.outcome for record in store.conditional_records("run-1")] == ["none"]


BAD_ROWS = {
    "decision-with-a-check-outcome": ("decision", "checkout_matches", 0, A),
    "check-with-a-decision-outcome": ("checkout_check", "none", None, None),
    "branch-without-its-index": ("decision", "branch", None, None),
    "none-with-a-branch": ("decision", "none", 0, A),
    "index-without-variant": ("decision", "branch", 0, None),
    "fourth-branch": ("decision", "branch", 3, A),
    "unknown-outcome": ("decision", "sideways", None, None),
    "unknown-kind": ("trace", "none", None, None),
    "text-index": ("decision", "branch", "first", A),
}


@pytest.mark.parametrize("case", sorted(BAD_ROWS))
def test_inconsistent_rows_are_refused_by_any_writer(
    store: SqliteAgentStore, tmp_path: Path, case: str
) -> None:
    """The CHECKs hold for SQL outside the store too."""
    kind, outcome, index, variant = BAD_ROWS[case]
    with closing(connect(tmp_path / "agent.db")) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO conditional_decisions VALUES ('run-1', 1, ?, ?, ?, ?, '{}', 't')",
            (kind, outcome, index, variant),
        )
    assert store.conditional_records("run-1") == ()


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "decision", "outcome": "checkout_matches", "branch_index": 0, "variant_id": A},
        {"kind": "checkout_check", "outcome": "none"},
        {"kind": "decision", "outcome": "branch"},
        {"kind": "decision", "outcome": "none", "branch_index": 0, "variant_id": A},
    ],
)
def test_the_store_refuses_them_too(store: SqliteAgentStore, fields: dict[str, Any]) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.append_conditional("run-1", ConditionalEntry(body={}, **fields))
    assert store.conditional_records("run-1") == ()


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "decision", "outcome": "sideways"},
        {"kind": "trace", "outcome": "none"},
        {"kind": "decision", "outcome": "branch", "branch_index": 3, "variant_id": A},
        {"kind": "decision", "outcome": "branch", "branch_index": True, "variant_id": A},
    ],
)
def test_an_entry_outside_the_contract_is_refused_before_sql(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ConditionalEntry(body={}, **fields)


def resolve(
    intent: ConditionalIntent, hard: HardConstraints = SEARCH
) -> ResolvedConditional | ConditionalClarification:
    return resolve_branches(intent, phone_feed(), hard=hard)


def evaluated(
    limit_a: int, limit_b: int, answers: dict[str, Any], hard: HardConstraints = SEARCH
) -> tuple[Any, Any]:
    resolved = resolve(ux03(limit_a, limit_b), hard)
    assert isinstance(resolved, ResolvedConditional)
    return resolved, evaluate_branches(resolved, FakeOfferReader(answers))


def test_the_decision_body_is_canonical_and_has_no_buyer_or_model_text(
    store: SqliteAgentStore, tmp_path: Path
) -> None:
    resolved, decision = evaluated(T_A - 1, T_B, {A: offer(A), B: offer(B)})
    explanation = "A no cumplía tu condición; elegí B."
    record = record_decision(
        store,
        "turn-1",
        purchase_mode="HP",
        governance_mode="enforce",
        intent=resolved.intent,
        hard=SEARCH,
        resolution=resolved,
        decision=decision,
        explanation=explanation,
    )
    assert (record.kind, record.outcome, record.branch_index, record.variant_id) == (
        "decision",
        "branch",
        1,
        B,
    )
    body = record.body
    assert body["schema"] == "conditional-decision/1"
    assert body["intent_sha256"] == conditional_intent_sha256(resolved.intent)
    assert body["hard_sha256"] == hard_sha256(SEARCH)
    assert (body["purchase_mode"], body["governance_mode"]) == ("HP", "enforce")
    assert (body["label"], body["chosen_index"], body["clarify"]) == ("B", 1, None)
    assert [branch["status"] for branch in body["branches"]] == ["over_limit", "eligible"]
    assert body["branches"][0]["offer"]["total_minor"] == T_A
    assert explanation not in json.dumps(body, ensure_ascii=False)
    assert body["explanation_sha256"].startswith("sha256:")
    with closing(connect(tmp_path / "agent.db")) as db:
        [(stored,)] = db.execute("SELECT body FROM conditional_decisions").fetchall()
    assert stored.encode() == canonical_bytes(json.loads(stored))
    assert store.latest_conditional_decision("turn-1") == record


def test_a_clarification_and_an_unknown_branch_are_recorded_too(store: SqliteAgentStore) -> None:
    ambiguous = ux03(T_A, T_B)
    in_usd = HardConstraints(currency="USD")
    clarification = resolve(ambiguous, in_usd)
    assert isinstance(clarification, ConditionalClarification)
    record = record_decision(
        store,
        "turn-2",
        purchase_mode="HP",
        governance_mode="off",
        intent=ambiguous,
        hard=in_usd,
        resolution=clarification,
        decision=None,
        explanation="¿En qué moneda?",
    )
    assert (record.outcome, record.branch_index, record.body["label"]) == ("clarify", None, None)
    assert record.body["clarify"] == {"cause": "currency", "branch_index": None, "variant_ids": []}
    assert record.body["branches"] == []

    resolved, unknown = evaluated(T_A, T_B, {A: acp_error("http_error", 503)})
    record = record_decision(
        store,
        "task-1",
        purchase_mode="HNP",
        governance_mode="enforce",
        intent=resolved.intent,
        hard=SEARCH,
        resolution=resolved,
        decision=unknown,
        explanation="No pude verificar.",
    )
    assert (record.outcome, record.body["label"]) == ("undetermined", "undetermined")
    assert record.body["branches"][0] == {
        "index": 0,
        "variant_id": A,
        "status": "unknown",
        "limit_minor": T_A,
        "unit_limit_minor": None,
        "feed_budget": False,
        "offer": None,
        "error_code": "http_error",
        "status_code": 503,
        "rejection_code": None,
    }


def test_a_mismatch_before_the_eligible_branch_is_recorded_as_a_clarify(
    store: SqliteAgentStore,
) -> None:
    """§3.3, step 2: B is eligible but A fell without a commercial fact; nothing is chosen."""
    hard = HardConstraints(currency="COP", required_terms=("oneplus",))
    resolved, decision = evaluated(T_A, T_B, {A: offer(A), B: offer(B)}, hard)
    record = record_decision(
        store,
        "turn-6",
        purchase_mode="HP",
        governance_mode="enforce",
        intent=resolved.intent,
        hard=hard,
        resolution=resolved,
        decision=decision,
        explanation="A no cumple otra condición de tu pedido.",
    )
    assert (record.outcome, record.branch_index, record.variant_id) == ("clarify", None, None)
    assert record.body["label"] is None
    assert record.body["clarify"] == {
        "cause": "intent_mismatch",
        "branch_index": 0,
        "variant_ids": [A],
    }
    assert [b["status"] for b in record.body["branches"]] == ["intent_mismatch", "eligible"]
    assert record.body["branches"][0]["rejection_code"] == "term"


def test_an_exclusion_only_intent_records_its_excluded_variants(store: SqliteAgentStore) -> None:
    """UX07: no branch is evaluated; the normal selection runs on the filtered feed and the
    record says which variants left it."""
    intent = ConditionalIntent(excluded=(match_b(),))
    resolved = resolve(intent)
    assert isinstance(resolved, ResolvedConditional)
    record = record_decision(
        store,
        "turn-4",
        purchase_mode="HP",
        governance_mode="enforce",
        intent=intent,
        hard=SEARCH,
        resolution=resolved,
        decision=evaluate_branches(resolved, FakeOfferReader({})),
        explanation="Busco sin el OnePlus 8T.",
    )
    assert (record.kind, record.outcome, record.branch_index) == (
        "decision",
        "exclusions_only",
        None,
    )
    assert (record.body["excluded_variant_ids"], record.body["branches"]) == ([B], [])
    assert record.body["label"] is None


def test_a_delegation_turn_with_exclusions_records_exclusions_only(
    store: SqliteAgentStore,
) -> None:
    """§3.9: a delegation turn evaluates no branch, even when it brings some; its exclusions
    still leave the draft and the row says which."""
    plain = ConditionalIntent(branches=ux03(T_A, T_B).branches[:1], excluded=(match_b(),))
    resolved = resolve(plain)
    assert isinstance(resolved, ResolvedConditional) and resolved.branches
    record = record_decision(
        store,
        "turn-7",
        purchase_mode="HNP",
        governance_mode="enforce",
        intent=plain,
        hard=SEARCH,
        resolution=resolved,
        decision=EXCLUSIONS_ONLY,
        explanation="Borrador sin el OnePlus 8T.",
    )
    assert (record.outcome, record.body["excluded_variant_ids"], record.body["branches"]) == (
        "exclusions_only",
        [B],
        [],
    )


def test_a_record_refuses_a_decision_that_does_not_fit_its_resolution() -> None:
    resolved, decision = evaluated(T_A, T_B, {A: offer(A)})
    common = {
        "purchase_mode": "HP",
        "governance_mode": "off",
        "intent": resolved.intent,
        "hard": SEARCH,
        "explanation": "x",
    }
    with pytest.raises(ValueError, match="needs its decision"):
        decision_record(resolution=resolved, decision=None, **common)
    clarification = resolve(ux03(T_A, T_B), HardConstraints(currency="USD"))
    with pytest.raises(ValueError, match="no decision"):
        decision_record(resolution=clarification, decision=decision, **common)
    # Without an exclusion there is nothing for an exclusion-only row to say.
    with pytest.raises(ValueError, match="needs an exclusion"):
        decision_record(resolution=resolved, decision=EXCLUSIONS_ONLY, **common)
    other = resolve(ConditionalIntent(branches=(PurchaseBranch(match=match_b()),), currency="COP"))
    with pytest.raises(ValueError, match="not of these branches"):
        decision_record(resolution=other, decision=decision, **common)


@pytest.mark.parametrize(
    ("total", "currency", "outcome", "matches"),
    [
        (T_B, "cop", "checkout_matches", True),
        (T_B - 1, "cop", "offer_changed", False),  # D4: lower is a change too
        (T_B + 1, "cop", "offer_changed", False),
        (T_B, "usd", "offer_changed", False),
    ],
)
def test_verify_checkout_compares_the_two_totals_strictly_and_records_it(
    store: SqliteAgentStore, total: int, currency: str, outcome: str, matches: bool
) -> None:
    _, decision = evaluated(T_A - 1, T_B, {A: offer(A), B: offer(B)})
    check = verify_checkout(store, "turn-3", decision, checkout(B, total, currency=currency))
    assert (check.matches, check.offer_total_minor, check.checkout_total_minor) == (
        matches,
        T_B,
        total,
    )
    record = check.record
    assert (record.kind, record.outcome, record.branch_index, record.variant_id) == (
        "checkout_check",
        outcome,
        1,
        B,
    )
    assert record.body == {
        "schema": "conditional-decision/1",
        "kind": "checkout_check",
        "outcome": outcome,
        "branch_index": 1,
        "variant_id": B,
        "checkout_id": f"chk_{B}",
        "offer_total_minor": T_B,
        "checkout_total_minor": total,
        "checkout_currency": currency,
    }
    # The oracle's decision is still the decision, not the check.
    assert store.latest_conditional_decision("turn-3") is None


def test_a_checkout_check_needs_the_chosen_branch_and_its_variant(store: SqliteAgentStore) -> None:
    _, none = evaluated(T_A - 1, T_B - 1, {A: offer(A), B: offer(B)})
    with pytest.raises(ValueError, match="chosen branch"):
        verify_checkout(store, "turn-5", none, checkout(B, T_B))
    _, chose_b = evaluated(T_A - 1, T_B, {A: offer(A), B: offer(B)})
    with pytest.raises(ValueError, match="chosen variant"):
        verify_checkout(store, "turn-5", chose_b, checkout(A, T_B))
    assert store.conditional_records("turn-5") == ()
