"""`verify-evidence`, HNP profile: the seventeen links of docs/design/hnp-governed.md §9.2.

This first part verifies links 1 to 6, the order webhook and the terminals; the others are
`missing` for a run past its checkout. Fixture databases come from real HNP runs
(tests/support/hnp_chain.py): the delegation service, the executor, D1/D2/D3-HNP, the freshness
guard, the issuer and the Agent Key are the product's over one SQLite file. Manipulated chains
are those databases edited afterwards, as someone with the file could, re-chaining the delegation
events when the edit should survive the hash chain.
"""

import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.delegation.models import event_digest
from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.persistence.delegation_sqlite import read_events
from commerce_evaluation.verify_evidence import main, verify, verify_database
from commerce_evaluation.verify_evidence_hnp import HNP_LINKS, HP_ONLY
from tests.evaluation.test_verify_evidence import body_edit, edit
from tests.governance.test_d1_hnp_gate import AUTHORIZED_ONLY
from tests.governance.test_shadow_integration import OWNER
from tests.support.hnp_chain import hnp_chain

GRANT = "dg_1"
UP_TO_CHECKOUT = HNP_LINKS[:6]
# Verified by the second part of HNP-V1.
PENDING = (
    "freshness",
    "d2_decision",
    "reservation",
    "closed_mandates",
    "d3_decision",
    "receipts",
    "consumption",
)


def expected(**statuses: tuple[str, ...]) -> dict[str, str]:
    """Every link `not_applicable` unless named under its status."""
    result: dict[str, str] = dict.fromkeys((*HNP_LINKS, *HP_ONLY), "not_applicable")
    for status, names in statuses.items():
        result |= dict.fromkeys(names, status)
    return result


def statuses(run: dict[str, Any]) -> dict[str, str]:
    return {name: link["status"] for name, link in run["links"].items()}


def only_run(database: Path, **options: Any) -> dict[str, Any]:
    [run] = verify_database(database, **options)
    return run


# Scenarios: each one real run, built once per module and copied per test.


def _applied(directory: Path) -> Path:
    with hnp_chain(directory) as chain:
        assert chain.run().status == "applied"
    return chain.path


def _ended(directory: Path, before: Callable[[Any], None] | None = None, **options: Any) -> Path:
    with hnp_chain(directory, **options) as chain:
        if before is not None:
            before(chain)
        chain.run()
    return chain.path


SCENARIOS: dict[str, Callable[[Path], Path]] = {
    "b2_hnp": _applied,
    "d1_escalated": lambda directory: _ended(directory, total_max_minor=71_999),
    "d1_deny": lambda directory: _ended(
        directory, agent_d1_allowed_merchant_origins=AUTHORIZED_ONLY
    ),
    "revoked": lambda directory: _ended(directory, lambda chain: chain.revoke()),
    "kill_switch": lambda directory: _ended(directory, lambda chain: chain.kill_switch()),
    "expired": lambda directory: _ended(
        directory, lambda chain: chain.clock.advance(120), valid_for_seconds=60
    ),
    "b0_hnp": lambda directory: _ended(directory, mode="off"),
}


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Callable[[str], Path]]:
    cache: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name not in cache:
            cache[name] = SCENARIOS[name](tmp_path_factory.mktemp(name))
            with closing(sqlite3.connect(cache[name])) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return cache[name]

    yield get


@pytest.fixture
def scenario(built: Callable[[str], Path], tmp_path: Path) -> Callable[[str], Path]:
    def copy(name: str) -> Path:
        target = tmp_path / f"{name}.db"
        shutil.copyfile(built(name), target)
        return target

    return copy


def rechain(database: Path, event_type: str, *, by: str | None = None, **changes: Any) -> None:
    """Rewrite one event's body (and, with `by`, its actor and principal) and recompute every
    digest after it: the chain still verifies."""
    with closing(sqlite3.connect(database)) as connection, connection:
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'delegation_events'"
        ).fetchall():
            connection.execute(f"DROP TRIGGER {name}")
        previous = None
        for event in read_events(connection, GRANT):
            if event.event_type == event_type:
                event = replace(event, body={**event.body, **changes})
                if by is not None:
                    event = replace(event, actor_ref=by, on_behalf_of=by)
            changed = replace(event, previous_digest=previous)
            changed = replace(changed, digest=event_digest(changed.link()))
            connection.execute(
                "UPDATE delegation_events SET body = ?, actor_ref = ?, on_behalf_of = ?, "
                "previous_digest = ?, digest = ? WHERE grant_id = ? AND sequence = ?",
                (
                    canonical_bytes(dict(event.body)).decode(),
                    event.actor_ref,
                    event.on_behalf_of,
                    changed.previous_digest,
                    changed.digest,
                    GRANT,
                    event.sequence,
                ),
            )
            previous = changed.digest


# The chain of each kind of run


def test_a_b2_hnp_run_verifies_up_to_its_checkout_and_leaves_the_rest_missing(
    scenario: Callable[[str], Path],
) -> None:
    run = only_run(scenario("b2_hnp"))

    assert statuses(run) == expected(ok=UP_TO_CHECKOUT, missing=PENDING)
    assert (run["purchase_mode"], run["grant_id"], run["task_state"]) == ("HNP", GRANT, "running")
    assert run["links"]["d1_decision"]["refs"]["replay"] == "reproduced"
    assert "segunda parte" in run["links"]["d2_decision"]["detail"]
    # A link nobody verified yet is never `ok`, so the chain is not complete.
    assert run["complete"] is False


def test_a_d1_budget_deny_escalates_before_any_checkout(scenario: Callable[[str], Path]) -> None:
    """HN03: D1-HNP denies with POL-11; the task escalates and revokes the grant."""
    run = only_run(scenario("d1_escalated"))

    assert statuses(run) == expected(ok=(*HNP_LINKS[:5], "escalation"))
    assert run["links"]["escalation"]["refs"]["outcome_code"] == "budget_not_satisfiable"
    assert run["complete"] is True


def test_a_d1_deny_ends_the_chain_at_d1(scenario: Callable[[str], Path]) -> None:
    """HN08: the operator's list leaves the merchant out (POL-02); the task fails."""
    run = only_run(scenario("d1_deny"))

    assert statuses(run) == expected(ok=HNP_LINKS[:5])
    assert run["links"]["d1_decision"]["refs"]["outcome"] == "deny"
    assert (run["task_state"], run["outcome_code"]) == ("failed", "merchant_not_allowed")


def test_a_revocation_before_activation_is_verified_by_grant_and_task(
    scenario: Callable[[str], Path],
) -> None:
    """HN05: no purchase row and no run_id; the chain ends at the owner's revocation."""
    database = scenario("revoked")

    run = only_run(database, grant_id=GRANT)

    assert statuses(run) == expected(ok=("delegation_consent", "open_mandates", "revocation"))
    assert (run["run_id"], run["stage"], run["task_state"]) == (None, None, "canceled")
    assert run["ledger_chain"]["status"] == "ok"
    open_mandates = run["links"]["open_mandates"]["refs"]
    assert open_mandates["not_verified"] == ["merchant_refs", "owner_ref"]
    assert run["links"]["revocation"]["refs"] == {
        "revoked_reason": "owner",
        "epoch_before": 1,
        "epoch_after": 2,
        "effect": "no_effect",
    }
    assert run["complete"] is True


def test_the_kill_switch_before_activation_ends_the_chain(
    scenario: Callable[[str], Path],
) -> None:
    """HN06: the claim meets the switch; the operator revokes the grant."""
    run = only_run(scenario("kill_switch"), grant_id=GRANT)

    assert statuses(run) == expected(
        ok=("delegation_consent", "open_mandates", "revocation", "kill_switch")
    )
    assert run["links"]["kill_switch"]["refs"] == {"control_epoch": 2, "stage": "claim"}


def test_an_expired_grant_leaves_no_terminal_link(scenario: Callable[[str], Path]) -> None:
    """HN04: the claim finds the grant expired; nothing after the open mandates applies."""
    run = only_run(scenario("expired"), grant_id=GRANT)

    assert statuses(run) == expected(ok=("delegation_consent", "open_mandates"))
    assert (run["task_state"], run["outcome_code"]) == ("canceled", "grant_expired")


def test_b0_hnp_is_incomplete_by_construction(scenario: Callable[[str], Path]) -> None:
    """Without PDPs there is no D1, freshness, D2 or D3 to verify (pre-registration, B0-HNP)."""
    run = only_run(scenario("b0_hnp"))

    assert statuses(run) == expected(
        ok=("delegation_consent", "open_mandates", "task_activation"),
        missing=("intent", "d1_decision", "checkout_created", *PENDING),
    )
    assert run["links"]["d1_decision"]["detail"] != run["links"]["d2_decision"]["detail"]
    assert run["complete"] is False


# One manipulation per kind of link


def _sql(table: str, statement: str, *values: Any) -> Callable[[Path], None]:
    return lambda database: edit(database, table, statement, *values)


MANIPULATIONS: list[tuple[str, str, Callable[[Path], None]]] = [
    (
        "b2_hnp",
        "intent",
        _sql(
            "delegation_grants",
            "UPDATE delegation_grants SET intent_json = replace(intent_json, '80000', '90000')",
        ),
    ),
    (
        "b2_hnp",
        "delegation_consent",
        _sql(
            "delegation_proposals",
            "UPDATE delegation_proposals SET scope_json = replace(scope_json, '80000', '90000')",
        ),
    ),
    (
        "b2_hnp",
        "open_mandates",
        _sql(
            "delegation_mandates",
            "UPDATE delegation_mandates SET public_jwk_json = (SELECT public_jwk_json FROM "
            "delegation_mandates WHERE kind = 'closed_payment') WHERE kind = 'open_payment'",
        ),
    ),
    ("b2_hnp", "task_activation", lambda db: rechain(db, "task_claimed", trigger="operator")),
    (
        "b2_hnp",
        "d1_decision",
        body_edit("governance_evidence", "phase = 'D1'", "$.recorded_at", "2026-01-01T00:00:00Z"),
    ),
    (
        "b2_hnp",
        "checkout_created",
        _sql("d1_checkout_results", "UPDATE d1_checkout_results SET checkout_id = 'cs_other'"),
    ),
    # Another user revokes in the owner's name, re-chained.
    ("revoked", "revocation", lambda database: rechain(database, "revoked", by="usr_other")),
    (
        "revoked",
        "revocation",
        _sql("delegation_grants", "UPDATE delegation_grants SET authority_epoch = 3"),
    ),
    (
        "kill_switch",
        "kill_switch",
        _sql("hnp_controls", "UPDATE hnp_controls SET kill_switch = 0 WHERE control_epoch = 2"),
    ),
    (
        "d1_escalated",
        "escalation",
        _sql("hnp_tasks", "UPDATE hnp_tasks SET outcome_code = 'offer_changed'"),
    ),
]


@pytest.mark.parametrize(
    ("name", "link", "manipulate"),
    MANIPULATIONS,
    ids=[f"{name}-{link}" for name, link, _ in MANIPULATIONS],
)
def test_each_kind_of_link_detects_its_manipulation(
    scenario: Callable[[str], Path], name: str, link: str, manipulate: Callable[[Path], None]
) -> None:
    database = scenario(name)
    before = statuses(only_run(database, grant_id=GRANT))
    manipulate(database)

    run = only_run(database, grant_id=GRANT)

    assert before[link] == "ok"
    assert run["links"][link]["status"] == "mismatch", run["links"][link]
    assert not run["links"][link]["detail"].startswith("No se pudo interpretar")
    assert run["complete"] is False
    # Each link reads the evidence up to itself: the links before it keep verifying.
    earlier = HNP_LINKS[: HNP_LINKS.index(link)]
    assert {name: run["links"][name]["status"] for name in earlier} == {
        name: before[name] for name in earlier
    }


def test_an_event_edited_without_rechaining_breaks_only_the_links_after_it(
    scenario: Callable[[str], Path],
) -> None:
    database = scenario("d1_escalated")
    body_edit(
        "delegation_events", "event_type = 'task_finished'", "$.outcome_code", "offer_changed"
    )(database)

    run = only_run(database)

    assert statuses(run) == expected(ok=HNP_LINKS[:5], mismatch=("escalation",))


# The ledger as a whole: edits no single link reads still break `complete`


def _steps(*steps: Callable[[Path], None]) -> Callable[[Path], None]:
    def apply(database: Path) -> None:
        for step in steps:
            step(database)

    return apply


def _drop_event(sequence: int) -> Callable[[Path], None]:
    return _sql("delegation_events", "DELETE FROM delegation_events WHERE sequence = ?", sequence)


def _terminal(outcome: str) -> Callable[[Path], None]:
    return body_edit("delegation_events", "event_type = 'task_finished'", "$.outcome_code", outcome)


LEDGER: list[tuple[str, str, Callable[[Path], None]]] = [
    (
        "d1_deny",
        "terminal_and_task_edited",
        _steps(
            _terminal("selection_not_eligible"),
            _sql("hnp_tasks", "UPDATE hnp_tasks SET outcome_code = 'selection_not_eligible'"),
        ),
    ),
    ("d1_deny", "revocation_deleted", _drop_event(4)),
    (
        "d1_deny",
        "claim_deleted",
        _steps(_drop_event(3), _sql("hnp_tasks", "UPDATE hnp_tasks SET run_id = NULL")),
    ),
    (
        "d1_deny",
        "terminal_rechained",
        lambda database: rechain(database, "task_finished", outcome_code="selection_not_eligible"),
    ),
    (
        "d1_deny",
        "epoch_without_event",
        _sql("delegation_grants", "UPDATE delegation_grants SET authority_epoch = 3"),
    ),
    (
        "d1_deny",
        "grant_reopened",
        _sql(
            "delegation_grants",
            "UPDATE delegation_grants SET state = 'active', revoked_reason = NULL, "
            "authority_epoch = 1",
        ),
    ),
    (
        "expired",
        "expiry_edited",
        body_edit(
            "delegation_events", "event_type = 'expired'", "$.observed_at", "2026-01-01T00:00:00Z"
        ),
    ),
    ("expired", "expiry_deleted", _drop_event(3)),
    ("kill_switch", "terminal_edited", _terminal("revoked_by_owner")),
]


@pytest.mark.parametrize(
    ("name", "label", "manipulate"), LEDGER, ids=[f"{name}-{label}" for name, label, _ in LEDGER]
)
def test_the_whole_chain_and_the_rows_it_leaves_bind_complete(
    scenario: Callable[[str], Path], name: str, label: str, manipulate: Callable[[Path], None]
) -> None:
    database = scenario(name)
    before = only_run(database, grant_id=GRANT)
    manipulate(database)

    run = only_run(database, grant_id=GRANT)

    assert before["complete"] is True
    assert run["complete"] is False, label
    assert before["ledger_chain"]["status"] == "ok"
    assert run["ledger_chain"]["status"] == "mismatch"


def test_the_report_names_the_chained_terminal_not_the_table(
    scenario: Callable[[str], Path],
) -> None:
    database = scenario("d1_deny")
    _terminal("selection_not_eligible")(database)
    edit(database, "hnp_tasks", "UPDATE hnp_tasks SET outcome_code = 'selection_not_eligible'")

    run = only_run(database)

    # The edited `task_finished` no longer verifies: the chain proves a claimed, running task.
    assert (run["task_state"], run["outcome_code"], run["run_id"]) == ("running", None, "run_1")


def _without_d1(database: Path) -> None:
    """Every D1 row of the run deleted, and the purchase's selection with them."""
    with closing(sqlite3.connect(database)) as connection, connection:
        tables = ("d1_provenance_sources", "governance_evidence", "d1_checkout_operations")
        for table in (*tables, "purchases"):
            for (trigger,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
            ).fetchall():
                connection.execute(f"DROP TRIGGER {trigger}")
        for table in tables:
            connection.execute(f"DELETE FROM {table} WHERE run_id = 'run_1'")
        connection.execute("UPDATE purchases SET selected_variant_id = NULL, checkout_id = NULL")


@pytest.mark.parametrize("name", ["d1_deny", "d1_escalated"])
def test_a_d1_terminal_without_its_d1_evidence_is_missing(
    scenario: Callable[[str], Path], name: str
) -> None:
    """HN08 and HN03 end with a D1 code: the chained terminal says D1 ran."""
    database = scenario(name)
    _without_d1(database)

    run = only_run(database)

    assert run["links"]["d1_decision"]["status"] == "missing"
    assert run["links"]["intent"]["status"] == "missing"
    assert run["complete"] is False


# Integration with the HP verifier and the CLI


def test_an_hnp_purchase_is_never_verified_as_an_hp_one(scenario: Callable[[str], Path]) -> None:
    document = verify([scenario("b2_hnp"), scenario("d1_escalated")])

    assert [run["purchase_mode"] for run in document["runs"]] == ["HNP", "HNP"]
    assert document["h2"]["all_runs"]["n"] == 0
    assert document["h2_hnp"]["all_runs"] == {"n": 2, "complete": 1, "rate": 0.5}
    assert document["h2_hnp"]["blocked"] == {"n": 1, "complete": 1, "rate": 1.0}
    assert document["h2_hnp"]["blocked_states"] == ["canceled", "escalated", "failed"]


def test_the_report_carries_no_token_mandate_receipt_or_owner(
    scenario: Callable[[str], Path],
) -> None:
    database = scenario("b2_hnp")

    report = json.dumps(verify([database]))

    with closing(sqlite3.connect(database)) as connection:
        tokens = [row[0] for row in connection.execute("SELECT token FROM delegation_mandates")]
        error = connection.execute("SELECT checkout_details_json FROM delegation_grants")
        details = error.fetchone()[0]
    assert len(tokens) == 4
    assert all(token not in report for token in tokens)
    assert OWNER not in report and "buyer@example.test" not in report and details not in report


def test_the_cli_verifies_one_grant(
    scenario: Callable[[str], Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = scenario("revoked")
    output = tmp_path / "chain.json"

    assert main(["--agent-db", str(database), "--grant", GRANT, "--output", str(output)]) == 0
    [run] = json.loads(output.read_text(encoding="utf-8"))["runs"]
    assert run["grant_id"] == GRANT
    with pytest.raises(SystemExit) as unknown:
        main(["--agent-db", str(database), "--grant", "dg_unknown"])
    assert unknown.value.code == 2
    assert "no HNP task for grant dg_unknown" in capsys.readouterr().err
