"""Real SQLite revision/CAS tests; only synthetic temporary databases and fake gateways."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from commerce_agent.domain import PurchaseIntent
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.sqlite import PurchaseCASResult
from tests.test_completion import FakeGateway, approve, checkout, dependencies
from tests.test_purchase_service import FakeCheckoutGateway, details, make_service


@pytest.fixture
def store(tmp_path: Path) -> SqliteAgentStore:
    result = SqliteAgentStore(tmp_path / "agent.db")
    result.initialize()
    result.create_purchase("run-1", "synthetic request")
    return result


def test_new_purchase_revision_and_legacy_projection(store: SqliteAgentStore) -> None:
    assert store.purchase_state_version("run-1") == 1
    assert store.purchase_state_version("missing") is None
    purchase = store.purchase("run-1")
    assert purchase is not None
    assert "state_version" not in purchase
    store.create_purchase("run-1", "synthetic request")
    assert store.purchase_state_version("run-1") == 1
    with pytest.raises(ValueError, match="conflicts"):
        store.create_purchase("run-1", "another request")
    assert store.purchase_state_version("run-1") == 1


@pytest.mark.parametrize("legacy_owner", [False, True])
def test_additive_legacy_migration_preserves_rows_and_other_tables(
    tmp_path: Path, legacy_owner: bool
) -> None:
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        owner = "owner_id TEXT NOT NULL DEFAULT 'local-human'," if legacy_owner else ""
        connection.execute(
            f"""CREATE TABLE purchases (
            run_id TEXT PRIMARY KEY, {owner} request_text TEXT NOT NULL, stage TEXT NOT NULL,
            selected_variant_id TEXT, checkout_id TEXT UNIQUE, order_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
        connection.execute(
            "INSERT INTO purchases(run_id, request_text, stage, selected_variant_id, checkout_id, "
            "order_id, created_at, updated_at) VALUES ('old', 'historic request', 'completed', "
            "'variant-old', 'checkout-old', 'order-old', 'original-created', 'original-updated')"
        )
        connection.execute("CREATE TABLE historical_evidence (body BLOB)")
        connection.execute("INSERT INTO historical_evidence VALUES (?)", (b"\x00\r\n\xff",))
        original = connection.execute("SELECT * FROM purchases").fetchall()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(purchases)")]
    migrated = SqliteAgentStore(path)
    migrated.initialize()
    migrated.initialize()
    assert migrated.purchase_state_version("old") == 1
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(f"SELECT {', '.join(columns)} FROM purchases").fetchall() == (
            original
        )
        # Additive: a historic row has no failure code, and the trace reads it as generic.
        assert connection.execute("SELECT failure_code FROM purchases").fetchall() == [(None,)]
        assert connection.execute("SELECT body FROM historical_evidence").fetchone() == (
            b"\x00\r\n\xff",
        )
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'purchases_revision_%'"
        ).fetchone() == (2,)
    migrated.update_purchase("old", stage="uncertain")
    assert migrated.purchase_state_version("old") == 2
    migrated.initialize()
    assert migrated.purchase_state_version("old") == 2
    assert migrated.compare_and_set_purchase("old", 2, stage="approved") == PurchaseCASResult(
        "applied", 3
    )


def test_incompatible_existing_revision_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.db"
    target = SqliteAgentStore(path)
    target.initialize()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER purchases_revision_insert")
        connection.execute("DROP TRIGGER purchases_revision_update")
        connection.execute("ALTER TABLE purchases DROP COLUMN state_version")
        connection.execute("ALTER TABLE purchases ADD COLUMN state_version TEXT")
    with pytest.raises(RuntimeError, match="Incompatible"):
        target.initialize()


def test_store_writers_increment_once_per_material_update(store: SqliteAgentStore) -> None:
    store.update_purchase("run-1", stage="selected", selected_variant_id="variant-1")
    assert store.purchase_state_version("run-1") == 2
    store.update_purchase("run-1", stage="selected", selected_variant_id="variant-1")
    assert store.purchase_state_version("run-1") == 2
    store.update_purchase("run-1", stage="selected", selected_variant_id="variant-2")
    assert store.purchase_state_version("run-1") == 3
    store.update_purchase("run-1", stage="approved", checkout_id="checkout-1")
    assert store.purchase_state_version("run-1") == 4
    assert store.claim_completion("run-1")
    assert store.purchase_state_version("run-1") == 5
    assert not store.claim_completion("run-1")
    assert store.purchase_state_version("run-1") == 5
    store.release_completion("run-1", stage="uncertain")
    assert store.purchase_state_version("run-1") == 6
    with pytest.raises(RuntimeError, match="reservation"):
        store.release_completion("run-1", stage="approved")
    assert store.purchase_state_version("run-1") == 6


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("run_id", "run-renamed"),
        ("owner_id", "owner-2"),
        ("request_text", "request-2"),
        ("stage", "selected"),
        ("selected_variant_id", "variant-1"),
        ("checkout_id", "checkout-1"),
        ("order_id", "order-1"),
        ("created_at", "changed-created"),
    ],
)
def test_direct_sql_cannot_omit_revision(
    tmp_path: Path, store: SqliteAgentStore, column: str, value: str
) -> None:
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection, connection:
        with pytest.raises(sqlite3.IntegrityError, match="purchase_revision_mismatch"):
            connection.execute(
                f"UPDATE purchases SET {column} = ? WHERE run_id = 'run-1'", (value,)
            )
        assert store.purchase_state_version("run-1") == 1
        connection.execute(
            f"UPDATE purchases SET {column} = ?, state_version = state_version + 1 "
            "WHERE run_id = 'run-1'",
            (value,),
        )
    identity = value if column == "run_id" else "run-1"
    assert store.purchase_state_version(identity) == 2


@pytest.mark.parametrize("recursive", [0, 1])
def test_direct_sql_noop_timestamp_null_change_and_rollback(
    tmp_path: Path, store: SqliteAgentStore, recursive: int
) -> None:
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        connection.execute(f"PRAGMA recursive_triggers = {recursive}")
        connection.execute(
            "UPDATE purchases SET stage=stage, updated_at='new' WHERE run_id='run-1'"
        )
        connection.commit()
        assert store.purchase_state_version("run-1") == 1
        with pytest.raises(sqlite3.IntegrityError, match="purchase_revision_mismatch"):
            connection.execute("UPDATE purchases SET state_version=state_version+1")
        connection.rollback()
        connection.execute(
            "UPDATE purchases SET selected_variant_id='v', state_version=state_version+1"
        )
        assert connection.execute("SELECT state_version FROM purchases").fetchone() == (2,)
        connection.rollback()
        assert store.purchase_state_version("run-1") == 1
        with connection:
            connection.execute(
                "UPDATE purchases SET selected_variant_id='v', state_version=state_version+1"
            )
        with connection:
            connection.execute(
                "UPDATE purchases SET selected_variant_id=NULL, state_version=state_version+1"
            )
        assert store.purchase_state_version("run-1") == 3


@pytest.mark.parametrize("version", [0, -1, 2, 7, 1.5])
def test_initial_revision_must_be_one(tmp_path: Path, version: int | float) -> None:
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    with (
        closing(sqlite3.connect(tmp_path / "agent.db")) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            "INSERT INTO purchases"
            "(run_id,request_text,stage,created_at,updated_at,state_version) "
            "VALUES ('bad','request','received','created','updated',?)",
            (version,),
        )
    assert store.purchase_state_version("bad") is None


def test_failed_constraint_and_missing_update_do_not_advance(store: SqliteAgentStore) -> None:
    store.create_purchase("run-2", "second")
    store.update_purchase("run-2", stage="selected", checkout_id="taken")
    with pytest.raises(sqlite3.IntegrityError):
        store.update_purchase("run-1", stage="selected", checkout_id="taken")
    assert store.purchase_state_version("run-1") == 1
    with pytest.raises(KeyError):
        store.update_purchase("missing", stage="selected")
    assert store.purchase_state_version("missing") is None
    with pytest.raises(sqlite3.IntegrityError):
        store.compare_and_set_purchase("run-1", 1, stage="selected", checkout_id="taken")
    assert store.purchase_state_version("run-1") == 1


def test_cas_success_stale_noop_and_missing(store: SqliteAgentStore) -> None:
    assert store.compare_and_set_purchase("run-1", 1, stage="received") == PurchaseCASResult(
        "unchanged", 1
    )
    assert store.compare_and_set_purchase(
        "run-1", 1, stage="selected", selected_variant_id="variant-1"
    ) == PurchaseCASResult("applied", 2)
    assert store.compare_and_set_purchase("run-1", 1, stage="failed") == PurchaseCASResult(
        "stale", 2
    )
    assert store.compare_and_set_purchase("run-1", 2, stage="selected") == PurchaseCASResult(
        "unchanged", 2
    )
    assert store.compare_and_set_purchase("missing", 1, stage="selected") == PurchaseCASResult(
        "not_found", None
    )
    assert store.purchase("run-1")["selected_variant_id"] == "variant-1"  # type: ignore[index]


@pytest.mark.parametrize("version", [True, False, 0, -1, "1", 1.0, None, 2**63])
def test_cas_rejects_invalid_expected_revision(store: SqliteAgentStore, version: Any) -> None:
    with pytest.raises(ValueError, match="positive SQLite integer"):
        store.compare_and_set_purchase("run-1", version, stage="selected")
    assert store.purchase_state_version("run-1") == 1


def test_competing_connections_have_one_cas_winner(tmp_path: Path, store: SqliteAgentStore) -> None:
    barrier = Barrier(2)

    def compete(stage: str) -> PurchaseCASResult:
        other = SqliteAgentStore(tmp_path / "agent.db")
        assert other.purchase_state_version("run-1") == 1
        barrier.wait(timeout=5)
        return other.compare_and_set_purchase("run-1", 1, stage=stage)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ["selected", "failed"]))
    assert sorted(result.status for result in results) == ["applied", "stale"]
    assert [result.state_version for result in results] == [2, 2]
    assert store.purchase_state_version("run-1") == 2


def test_legacy_writer_invalidates_old_cas_without_becoming_cas(store: SqliteAgentStore) -> None:
    version = store.purchase_state_version("run-1")
    assert version == 1
    store.update_purchase("run-1", stage="selected")
    assert store.compare_and_set_purchase("run-1", version, stage="failed").status == "stale"
    store.update_purchase("run-1", stage="failed")
    assert store.purchase_state_version("run-1") == 3


def test_purchase_service_approval_keep_fake_gateway_calls(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    service, store = make_service(tmp_path, gateway)
    service.start(
        run_id="run-1",
        request="buy headphones",
        intent=PurchaseIntent(),
        selected_variant_id="SON-01",
        details=details(),
    )
    assert store.purchase_state_version("run-1") == 4
    assert [call[0] for call in gateway.calls] == ["create", "update", "get"]
    terms = service.approval_terms("run-1")
    assert store.purchase_state_version("run-1") == 4
    service.approve(run_id="run-1", actor_id="actor-1", expected_fingerprint=terms.fingerprint)
    assert store.purchase_state_version("run-1") == 5


def test_purchase_cancel_retry_is_not_a_new_transition(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    service, store = make_service(tmp_path, gateway)
    service.start(
        run_id="run-1",
        request="buy headphones",
        intent=PurchaseIntent(),
        selected_variant_id="SON-01",
        details=details(),
    )
    service.cancel(run_id="run-1")
    calls = list(gateway.calls)
    assert store.purchase_state_version("run-1") == 5
    service.cancel(run_id="run-1")
    assert store.purchase_state_version("run-1") == 5
    assert gateway.calls == calls


def test_completion_and_replay_revision_with_explicit_other_table_boundary(tmp_path: Path) -> None:
    gateway = FakeGateway(checkout())
    store, approvals, service = dependencies(tmp_path, gateway)
    assert store.purchase_state_version("run-1") == 2
    approve(approvals, gateway.current)
    # Saving approval is a separate transaction/table; it does not revise purchases.
    assert store.purchase_state_version("run-1") == 2
    result = service.complete(run_id="run-1")
    assert store.purchase_state_version("run-1") == 4  # claim + record_completion
    assert len(gateway.complete_calls) == 1
    assert service.complete(run_id="run-1") == result
    assert store.purchase_state_version("run-1") == 4
    assert len(gateway.complete_calls) == 1
    store.record_completion("run-1", result.model_dump(mode="json"))
    assert store.purchase_state_version("run-1") == 4


def test_record_completion_rolls_back_revision_and_related_writes(tmp_path: Path) -> None:
    gateway = FakeGateway(checkout())
    store, approvals, service = dependencies(tmp_path, gateway)
    approve(approvals, gateway.current)
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection, connection:
        connection.execute(
            """CREATE TRIGGER reject_consumption BEFORE UPDATE ON approvals
            BEGIN SELECT RAISE(ABORT, 'synthetic_failure_after_purchase_update'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic_failure"):
        service.complete(run_id="run-1")
    # claim committed (3), completion transaction rolled back, release uncertain committed (4).
    assert store.purchase_state_version("run-1") == 4
    assert store.purchase("run-1")["stage"] == "uncertain"  # type: ignore[index]
    assert store.completion_result("run-1") is None
    assert store.order("ord_completion_1") is None
    record = store.approval("run-1")
    assert record is not None and record.consumed_at is None


def test_failure_code_is_bookkeeping_and_only_a_code(store: SqliteAgentStore) -> None:
    def stored() -> str | None:
        with closing(sqlite3.connect(store._path)) as connection:
            return connection.execute(
                "SELECT failure_code FROM purchases WHERE run_id = 'run-1'"
            ).fetchone()[0]

    store.update_purchase("run-1", stage="failed", failure="Merchant said: card 4242 declined")
    assert (stored(), store.purchase_state_version("run-1")) == (None, 2)
    # Setting the code alone is not a business change: the revision stays.
    store.update_purchase("run-1", stage="failed", failure="checkout_mismatch")
    assert (stored(), store.purchase_state_version("run-1")) == ("checkout_mismatch", 2)
