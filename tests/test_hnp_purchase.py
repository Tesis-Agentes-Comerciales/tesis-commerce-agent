"""The HNP purchase in `purchases` (docs/design/hnp-governed.md §4.3 and §4.6), real SQLite.

`start` with a grant stops at `checkout_prepared`; an HNP purchase is never the pending HP purchase;
`transition_hnp_purchase` is the CAS the executor's units of work run inside their transaction.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import SecretStr

from commerce_agent.acp import load_product_feed
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.application.conversation import PENDING_STAGES
from commerce_agent.approval import ApprovalService
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.hnp_policy import HnpGrant
from commerce_agent.persistence import SqliteAgentStore
from tests.test_purchase_service import (
    FIXTURE,
    FakeCheckoutGateway,
    details,
    failure_code,
    make_service,
)

OWNER = "owner-1"
GRANT = HnpGrant(grant_id="dg_0123456789abcdef", variant_ids=("SON-01",))


@pytest.fixture
def store(tmp_path: Path) -> SqliteAgentStore:
    result = SqliteAgentStore(tmp_path / "agent.db")
    result.initialize()
    return result


def row(path: Path, run_id: str) -> tuple[str, str | None, str, int]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT purchase_mode,grant_id,stage,state_version FROM purchases WHERE run_id=?",
            (run_id,),
        ).fetchone()


def hnp_at(store: SqliteAgentStore, run_id: str, stage: str) -> int:
    store.create_purchase(run_id, "delegated", OWNER, grant_id="dg_" + run_id)
    if stage != "received":
        store.update_purchase(run_id, stage=stage, checkout_id="cs_" + run_id)
    version = store.purchase_state_version(run_id)
    assert version is not None
    return version


def transition(path: Path, run_id: str, **changes) -> int:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        return SqliteAgentStore.transition_hnp_purchase(connection, run_id, **changes)


def test_a_purchase_is_hp_unless_it_executes_a_grant(store: SqliteAgentStore) -> None:
    store.create_purchase("run-hp", "buy", OWNER)
    store.create_purchase("run-hnp", "delegated", OWNER, grant_id="dg_1")

    assert row(store._path, "run-hp") == ("HP", None, "received", 1)
    assert row(store._path, "run-hnp") == ("HNP", "dg_1", "received", 1)


@pytest.mark.parametrize(("mode", "grant"), [("HNP", None), ("HP", "dg_1"), ("XHP", None)])
def test_the_schema_binds_the_mode_to_the_grant(
    store: SqliteAgentStore, mode: str, grant: str | None
) -> None:
    with closing(sqlite3.connect(store._path)) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO purchases(run_id,request_text,stage,created_at,updated_at,"
            "purchase_mode,grant_id) VALUES ('run-x','r','received','t','t',?,?)",
            (mode, grant),
        )


@pytest.mark.parametrize(
    "assignment", ["purchase_mode='HNP',grant_id='dg_1'", "grant_id='dg_2'", "purchase_mode='HP'"]
)
def test_the_mode_and_the_grant_of_a_purchase_never_change(
    store: SqliteAgentStore, assignment: str
) -> None:
    store.create_purchase("run-hp", "buy", OWNER)
    store.create_purchase("run-hnp", "delegated", OWNER, grant_id="dg_1")
    run_id = "run-hp" if assignment.startswith("purchase_mode='HNP'") else "run-hnp"

    with closing(sqlite3.connect(store._path)) as connection, pytest.raises(sqlite3.Error):
        connection.execute(f"UPDATE purchases SET {assignment} WHERE run_id=?", (run_id,))


def test_a_run_is_never_reused_in_another_mode(store: SqliteAgentStore) -> None:
    store.create_purchase("run-1", "buy", OWNER)
    store.create_purchase("run-2", "delegated", OWNER, grant_id="dg_1")

    with pytest.raises(ValueError, match="conflicts"):
        store.create_purchase("run-1", "buy", OWNER, grant_id="dg_1")
    with pytest.raises(ValueError, match="conflicts"):
        store.create_purchase("run-2", "delegated", OWNER)
    with pytest.raises(ValueError, match="conflicts"):
        store.create_purchase("run-2", "delegated", OWNER, grant_id="dg_other")
    store.create_purchase("run-2", "delegated", OWNER, grant_id="dg_1")


def test_legacy_purchases_migrate_as_human_present(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE purchases (run_id TEXT PRIMARY KEY, request_text TEXT NOT NULL, "
            "stage TEXT NOT NULL, selected_variant_id TEXT, checkout_id TEXT UNIQUE, "
            "order_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO purchases(run_id,request_text,stage,created_at,updated_at) "
            "VALUES ('old','historic','completed','c','u')"
        )
    migrated = SqliteAgentStore(path)
    migrated.initialize()
    migrated.initialize()

    assert row(path, "old") == ("HP", None, "completed", 1)


def test_an_hnp_purchase_is_never_the_pending_hp_purchase(store: SqliteAgentStore) -> None:
    hnp_at(store, "run-hnp", "checkout_prepared")

    assert store.pending_purchase(PENDING_STAGES, OWNER) is None

    store.create_purchase("run-hp", "buy", OWNER)
    store.update_purchase("run-hp", stage="awaiting_confirmation")
    pending = store.pending_purchase(PENDING_STAGES, OWNER)
    assert pending is not None and pending["run_id"] == "run-hp"


def test_hp_completion_never_claims_an_hnp_purchase(store: SqliteAgentStore) -> None:
    hnp_at(store, "run-hnp", "uncertain")

    assert store.claim_completion("run-hnp") is False
    assert row(store._path, "run-hnp")[2] == "uncertain"


def test_the_executor_moves_an_hnp_purchase_by_cas(store: SqliteAgentStore) -> None:
    version = hnp_at(store, "run-hnp", "checkout_prepared")

    completing = transition(
        store._path, "run-hnp", expected_state_version=version, stage="completing"
    )
    uncertain = transition(
        store._path, "run-hnp", expected_state_version=completing, stage="uncertain"
    )

    assert (completing, uncertain) == (version + 1, version + 2)
    assert row(store._path, "run-hnp") == ("HNP", "dg_run-hnp", "uncertain", uncertain)


@pytest.mark.parametrize(
    ("source", "stage", "code"),
    [
        ("checkout_prepared", "failed", "policy_denied"),
        ("completing", "failed", "verifier_invalid_mandate"),
        ("checkout_prepared", "canceled", None),
        ("completing", "canceled", None),
        # D1 stopped `start` before any checkout existed (§8.1): POL-01/02 or no review fail,
        # POL-11 escalates and the purchase is canceled.
        ("received", "failed", "selection_not_eligible"),
        ("received", "failed", "merchant_not_allowed"),
        ("received", "failed", "governance_unavailable"),
        ("received", "canceled", None),
    ],
)
def test_a_terminal_task_closes_its_purchase(
    store: SqliteAgentStore, source: str, stage: str, code: str | None
) -> None:
    version = hnp_at(store, "run-hnp", source)

    transition(
        store._path, "run-hnp", expected_state_version=version, stage=stage, failure_code=code
    )

    assert row(store._path, "run-hnp")[2:] == (stage, version + 1)
    assert failure_code(store._path.parent, "run-hnp") == code


@pytest.mark.parametrize(
    ("source", "stage", "code", "offset"),
    [
        pytest.param("checkout_prepared", "completing", None, 1, id="stale-version"),
        pytest.param("checkout_prepared", "uncertain", None, 0, id="uncertain-before-commit"),
        pytest.param("awaiting_confirmation", "completing", None, 0, id="hp-stage"),
        pytest.param("completing", "completing", None, 0, id="already-completing"),
        pytest.param("completing", "completed", None, 0, id="completion-is-09b"),
        pytest.param("received", "uncertain", None, 0, id="uncertain-without-checkout"),
        pytest.param("received", "completing", None, 0, id="completing-without-checkout"),
        pytest.param("uncertain", "completing", None, 0, id="completing-an-uncertain-one"),
        pytest.param("uncertain", "failed", "checkout_failed", 1, id="kept-but-stale"),
    ],
)
def test_a_lost_or_foreign_transition_raises_and_changes_nothing(
    store: SqliteAgentStore, source: str, stage: str, code: str | None, offset: int
) -> None:
    version = hnp_at(store, "run-hnp", source)

    with pytest.raises((RuntimeError, ValueError)):
        transition(
            store._path,
            "run-hnp",
            expected_state_version=version + offset,
            stage=stage,
            failure_code=code,
        )

    assert row(store._path, "run-hnp")[2:] == (source, version)


@pytest.mark.parametrize("source", ["uncertain", "failed", "canceled"])
@pytest.mark.parametrize(
    ("stage", "code"),
    [("failed", "checkout_failed"), ("canceled", None), ("uncertain", None)],
)
def test_a_terminal_keeps_what_start_left(
    store: SqliteAgentStore, source: str, stage: str, code: str | None
) -> None:
    """An uncertain checkout may exist: the task's terminal never marks it failed (K14)."""
    version = hnp_at(store, "run-hnp", source)

    kept = transition(
        store._path, "run-hnp", expected_state_version=version, stage=stage, failure_code=code
    )

    assert kept == version
    assert row(store._path, "run-hnp")[2:] == (source, version)
    assert failure_code(store._path.parent, "run-hnp") is None


@pytest.mark.parametrize(("stage", "code"), [("failed", None), ("canceled", "kill_switch")])
def test_only_a_failed_purchase_carries_a_failure_code(
    store: SqliteAgentStore, stage: str, code: str | None
) -> None:
    version = hnp_at(store, "run-hnp", "checkout_prepared")

    with pytest.raises(ValueError):
        transition(
            store._path, "run-hnp", expected_state_version=version, stage=stage, failure_code=code
        )


@pytest.mark.parametrize(
    ("source", "stage", "code"),
    [
        ("checkout_prepared", "completing", None),
        ("received", "failed", "selection_not_eligible"),
        ("received", "canceled", None),
        ("uncertain", "failed", "checkout_failed"),
        ("failed", "canceled", None),
    ],
)
def test_an_hp_purchase_never_moves_through_the_hnp_transition(
    store: SqliteAgentStore, source: str, stage: str, code: str | None
) -> None:
    store.create_purchase("run-hp", "buy", OWNER)
    if source != "received":
        store.update_purchase("run-hp", stage=source, checkout_id="cs_1")
    version = store.purchase_state_version("run-hp")

    with pytest.raises(RuntimeError):
        transition(
            store._path, "run-hp", expected_state_version=version, stage=stage, failure_code=code
        )

    assert row(store._path, "run-hp")[2:] == (source, version)


def test_the_transition_needs_the_callers_transaction(store: SqliteAgentStore) -> None:
    version = hnp_at(store, "run-hnp", "checkout_prepared")

    with closing(sqlite3.connect(store._path)) as connection, pytest.raises(RuntimeError):
        store.transition_hnp_purchase(
            connection, "run-hnp", expected_state_version=version, stage="completing"
        )


def test_the_transition_rolls_back_with_the_callers_unit_of_work(store: SqliteAgentStore) -> None:
    version = hnp_at(store, "run-hnp", "checkout_prepared")

    with (
        closing(sqlite3.connect(store._path)) as connection,
        pytest.raises(LookupError),
        connection,
    ):
        connection.execute("BEGIN IMMEDIATE")
        store.transition_hnp_purchase(
            connection, "run-hnp", expected_state_version=version, stage="completing"
        )
        raise LookupError("a later write of the same unit of work failed")

    assert row(store._path, "run-hnp")[2:] == ("checkout_prepared", version)


def start(service: PurchaseService, *, total: int | None = None, grant: HnpGrant | None = GRANT):
    return service.start(
        run_id="run-hnp",
        request="delegated",
        intent=PurchaseIntent(hard=HardConstraints(currency="COP", max_total_amount_minor=total)),
        selected_variant_id="SON-01",
        details=details(),
        owner_id=OWNER,
        grant=grant,
    )


def test_start_in_hnp_stops_at_checkout_prepared(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    service, store = make_service(tmp_path, gateway)

    prepared = start(service, total=80_000)

    assert prepared.stage == "checkout_prepared"
    assert prepared.checkout.status == "ready_for_payment"
    assert [name for name, _ in gateway.calls] == ["create", "update", "get"]
    assert row(store._path, "run-hnp")[:3] == ("HNP", GRANT.grant_id, "checkout_prepared")
    assert store.pending_purchase(PENDING_STAGES, OWNER) is None


def test_the_budget_guard_closes_an_hnp_start_as_it_does_in_hp(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway(total=74_000)
    service, store = make_service(tmp_path, gateway)

    with pytest.raises(PurchaseServiceError) as exceeded:
        start(service, total=73_000)

    assert exceeded.value.code == "budget_exceeded"
    assert row(store._path, "run-hnp")[2] == "canceled"


def test_an_hp_start_still_awaits_the_human(tmp_path: Path) -> None:
    service, store = make_service(tmp_path, FakeCheckoutGateway())

    prepared = start(service, grant=None)

    assert prepared.stage == "awaiting_confirmation"
    assert row(store._path, "run-hnp")[:3] == ("HP", None, "awaiting_confirmation")


class UntouchableObserver:
    """Shadow is not defined for HNP (§2.2): the service refuses before observing anything."""

    def __getattr__(self, name: str):
        raise AssertionError(f"Shadow must not observe an HNP purchase: {name}")


def test_hnp_never_runs_with_a_shadow_observer(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    service = PurchaseService(
        feed=load_product_feed(FIXTURE),
        checkout=gateway,
        store=store,
        approvals=ApprovalService(store, SecretStr("approval-secret-with-at-least-32-characters")),
        d1_observer=UntouchableObserver(),  # type: ignore[arg-type]
    )

    with pytest.raises(PurchaseServiceError) as refused:
        start(service)

    assert refused.value.code == "d1_unavailable"
    assert store.purchase("run-hnp") is None
    assert gateway.calls == []
