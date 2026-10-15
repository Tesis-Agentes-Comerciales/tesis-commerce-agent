"""Actual PurchaseService + D1 reference + temporary SQLite; simulated transport only."""

import secrets
import sqlite3
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
from pydantic import SecretBytes, SecretStr, ValidationError

from commerce_agent.acp.feed import ProductFeed, load_product_feed
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.application.purchase import CheckoutGateway
from commerce_agent.approval import ApprovalService
from commerce_agent.config import Settings
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.governance.enforce import InitialCheckoutDispatch, SqliteD1EnforceGate
from commerce_agent.governance.evaluator import ReferencePolicyEvaluator
from commerce_agent.governance.evidence import DecisionRecord, EvidenceError
from commerce_agent.governance.models import EvaluationMetadata, PolicyBinding
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    D1Configuration,
    D1EnforceConfiguration,
    ProvenanceKeys,
    build_d1_policy,
)
from commerce_agent.governance.shadow import ShadowD1Observer
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.d1_dispatch_sqlite import SqliteD1DispatchStore
from commerce_agent.persistence.d1_uow_sqlite import SqliteD1UnitOfWork
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from tests.test_purchase_service import FakeCheckoutGateway, details

FEED = Path(__file__).parents[1] / "fixtures/feed"
OWNER = "private-test-owner@example.test"
MARKER = "private-secret-must-not-be-logged"
NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


class SpyGateway(FakeCheckoutGateway):
    def __init__(self, path: Path, fault: str | None = None):
        super().__init__()
        self.path = path
        self.fault = fault
        self.remote_created = False
        self.write_lock_probes = 0

    def _free(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=0.05)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        self.write_lock_probes += 1

    def create_checkout(self, variant_id: str, *, currency: str, idempotency_key: str):
        self._free()
        result = super().create_checkout(
            variant_id, currency=currency, idempotency_key=idempotency_key
        )
        self.remote_created = True
        if self.fault == "timeout":
            raise TimeoutError(MARKER)
        if self.fault == "malformed":
            return cast(CheckoutSession, {"secret": MARKER})
        if self.fault == "invalid-instance":
            return result.model_copy(update={"id": True})
        if self.fault == "variant":
            result.line_items[0]["item"]["id"] = "foreign"
        if self.fault == "currency":
            result.currency = "usd"
        return result

    def get_checkout(self, checkout_id: str):
        self._free()
        result = super().get_checkout(checkout_id)
        if self.fault == "identity":
            return result.model_copy(update={"id": "foreign-checkout"}, deep=True)
        return result

    def update_checkout(self, checkout_id, update, *, idempotency_key):
        self._free()
        return super().update_checkout(checkout_id, update, idempotency_key=idempotency_key)

    def cancel_checkout(self, checkout_id, *, idempotency_key, reason_code=None):
        self._free()
        return super().cancel_checkout(
            checkout_id, idempotency_key=idempotency_key, reason_code=reason_code
        )


class SingleAttemptSpy:
    """Test-only transport implementation; no assertion about remote idempotence."""

    def create_once(
        self, gateway: CheckoutGateway, *, variant_id: str, currency: str, idempotency_key: str
    ):
        return gateway.create_checkout(
            variant_id, currency=currency, idempotency_key=idempotency_key
        )


@dataclass
class Case:
    path: Path
    keys: ProvenanceKeys
    gate: SqliteD1EnforceGate
    service: PurchaseService
    gateway: SpyGateway
    store: SqliteAgentStore


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def make_case(
    path: Path,
    *,
    fault: str | None = None,
    blocked: bool = False,
    deny: bool = False,
    evaluator_factory: Any = None,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
    dispatch: InitialCheckoutDispatch | None = None,
) -> Case:
    store = SqliteAgentStore(path)
    store.initialize()
    profile = ClockProfile(
        authority_source="system_utc",
        commercial_source="fixed_sandbox",
        authority_provider=None,
        commercial_provider=None,
        fixed_commercial_at="2026-09-20T12:00:00.000Z",
    )
    config = D1EnforceConfiguration(
        governance_mode="enforce",
        merchant_base_url="http://127.0.0.1:4120",
        policy_set_id="backend-cop-d1",
        policy_revision=1,
        currency="COP",
        quantity=1,
        clock=profile,
    )
    keys = ProvenanceKeys(
        key_id="test-ephemeral",
        pseudonym_key=SecretBytes(secrets.token_bytes(32)),
        encryption_key=SecretBytes(secrets.token_bytes(32)),
    )
    policy, binding = (
        build_d1_policy(
            policy_set_id=config.policy_set_id, version=1, merchant_ref="denied-merchant"
        )
        if deny
        else (None, None)
    )
    gate = SqliteD1EnforceGate(
        path,
        config,
        keys,
        ClockReader(profile),
        dispatch=None if blocked else dispatch or SingleAttemptSpy(),
        policy_bytes=policy,
        policy_binding=binding,
        evaluator_factory=evaluator_factory,
        connection_factory=connection_factory,
    )
    gateway = SpyGateway(path, fault)
    service = PurchaseService(
        feed=load_product_feed(FEED),
        checkout=gateway,
        store=store,
        approvals=ApprovalService(store, SecretStr(secrets.token_urlsafe(40)), clock=lambda: NOW),
        d1_gate=gate,
    )
    return Case(path, keys, gate, service, gateway, store)


def start(case: Case, **changes):
    args: dict[str, Any] = dict(
        run_id="run-a",
        owner_id=OWNER,
        request=MARKER,
        intent=PurchaseIntent(hard=HardConstraints(currency="COP")),
        selected_variant_id="SON-01",
        details=details(),
    )
    args.update(changes)
    return case.service.start(**args)


def records(case: Case):
    with closing(connect(case.path)) as connection:
        return SqliteGovernanceEvidenceStore(connection).for_run("run-a")


def snapshot(case: Case) -> tuple:
    with closing(connect(case.path)) as connection:
        return (
            connection.execute(
                "SELECT stage,state_version,checkout_id FROM purchases WHERE run_id='run-a'"
            ).fetchone(),
            tuple(
                connection.execute(
                    "SELECT operation_id,state,idempotency_key FROM d1_checkout_operations"
                )
            ),
            tuple(connection.execute("SELECT * FROM d1_checkout_results")),
        )


def test_allowed_real_purchase_path_and_committed_evidence(tmp_path: Path):
    case = make_case(tmp_path / "agent.db")
    result = start(case)
    assert result.stage == "awaiting_confirmation" and result.checkout.id == "cs_1"
    assert [name for name, _ in case.gateway.calls] == ["create", "get", "update", "get"]
    record = records(case)[0]
    assert isinstance(record, DecisionRecord)
    assert record.decision.governance_mode == "enforce" and record.decision.outcome == "allow"
    assert record.application.resulting_state_version == 2
    assert case.store.purchase_state_version("run-a") == 4
    status = case.gate.recovery_status("run-a")
    assert status is not None and status[0:2] == ("confirmed", "cs_1")
    assert case.gateway.write_lock_probes == 4
    with closing(connect(case.path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_normative_pol02_deny_preserved_and_no_gateway(tmp_path: Path):
    case = make_case(tmp_path / "agent.db", deny=True)
    with pytest.raises(PurchaseServiceError, match="D1 denied"):
        start(case)
    assert case.gateway.calls == []
    record = records(case)[0]
    assert isinstance(record, DecisionRecord)
    assert record.decision.reason_codes == ("ACTION_OR_MERCHANT_NOT_ALLOWED",)
    assert record.application.disposition == "blocked"
    assert snapshot(case) == (("received", 1, None), (), ())


@pytest.mark.parametrize("fault", ["exception", "invalid", "shadow", "forged-pol01"])
def test_engine_errors_and_shadow_never_authorize(tmp_path: Path, fault: str):
    class Engine:
        def __init__(self, policy: bytes, binding: PolicyBinding, metadata: EvaluationMetadata):
            from commerce_agent.governance.evaluator import measure_reference_engine

            self.reference = ReferencePolicyEvaluator(
                artifact_bytes=policy,
                binding=binding,
                metadata=metadata,
                identity=measure_reference_engine(),
            )

        def evaluate(self, data):
            if fault == "exception":
                raise RuntimeError(MARKER)
            if fault == "invalid":
                return {"outcome": "allow", "secret": MARKER}
            if fault == "shadow":
                return self.reference.evaluate(
                    data.model_copy(update={"governance_mode": "shadow"})
                )
            # A selection inconsistent with B0 cannot be smuggled in as a PDP denial.
            return self.reference.evaluate(
                data.model_copy(update={"selected_variant_id": "foreign"})
            )

    case = make_case(tmp_path / "agent.db", evaluator_factory=Engine)
    with pytest.raises(PurchaseServiceError):
        start(case)
    assert case.gateway.calls == []
    record = records(case)[0]
    assert isinstance(record, DecisionRecord) and record.decision.origin == "runtime_guard"
    assert record.decision.outcome == "deny"
    assert snapshot(case) == (("received", 1, None), (), ())


@pytest.mark.parametrize("fault", ["no_candidate", "invalid_candidate"])
def test_b0_selection_guards_run_before_pdp(
    tmp_path: Path, fault: str, monkeypatch: pytest.MonkeyPatch
):
    case = make_case(tmp_path / "agent.db")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("PDP must not be evaluated")

    monkeypatch.setattr(ReferencePolicyEvaluator, "evaluate", forbidden)
    with pytest.raises(PurchaseServiceError) as exc:
        start(
            case,
            **(
                {
                    "intent": PurchaseIntent(
                        hard=HardConstraints(currency="COP", max_unit_amount_minor=1)
                    )
                }
                if fault == "no_candidate"
                else {"selected_variant_id": "foreign"}
            ),
        )
    assert exc.value.code == fault and calls == [] and case.gateway.calls == []
    purchase = case.store.purchase("run-a")
    assert purchase is not None and purchase["stage"] == "failed"


@pytest.mark.parametrize("fault", ["ledger", "stale", "reservation", "artifact"])
def test_local_failures_never_reach_create(
    tmp_path: Path, fault: str, monkeypatch: pytest.MonkeyPatch
):
    case = make_case(tmp_path / "agent.db")
    original = SqliteD1UnitOfWork.prepare

    def prepare(uow, request):
        original(uow, request)
        if fault == "ledger":
            uow.connection.execute(
                "CREATE TRIGGER lost BEFORE INSERT ON governance_evidence "
                "BEGIN SELECT RAISE(ABORT,'lost'); END"
            )
        if fault == "stale":
            case.store.update_purchase("run-a", stage="failed")
            case.store.update_purchase("run-a", stage="received")
        if fault == "reservation":
            case.store.idempotency_key("run-a", "checkout.create")
        if fault == "artifact":
            raise EvidenceError("GOV_ARTIFACT_MISMATCH")

    monkeypatch.setattr(SqliteD1UnitOfWork, "prepare", prepare)
    with pytest.raises(PurchaseServiceError):
        start(case)
    assert case.gateway.calls == []
    current, operations, results = snapshot(case)
    assert current[0] == "received" and operations == results == ()
    if fault != "ledger":
        record = records(case)[0]
        assert isinstance(record, DecisionRecord) and record.decision.outcome == "allow"
        assert record.application.disposition == "blocked"


@pytest.mark.parametrize("phase", ["authorization", "claim"])
@pytest.mark.parametrize("failure", ["before", "after", "no_commit"])
def test_commit_failure_blocks_http(tmp_path: Path, phase: str, failure: str):
    path = tmp_path / "agent.db"
    fired = []

    class Connection(sqlite3.Connection):
        def commit(self):
            exists = self.execute(
                "SELECT 1 FROM sqlite_master WHERE name='d1_checkout_operations'"
            ).fetchone()
            rows = (
                self.execute("SELECT state FROM d1_checkout_operations").fetchall()
                if exists
                else []
            )
            target = "reserved" if phase == "authorization" else "claimed"
            if not fired and rows == [(target,)]:
                fired.append(True)
                if failure == "after":
                    super().commit()
                if failure == "no_commit":
                    self.rollback()
                    return
                raise sqlite3.OperationalError(MARKER)
            return super().commit()

    case = make_case(
        path, connection_factory=lambda: sqlite3.connect(path, factory=Connection, timeout=10)
    )
    with pytest.raises(PurchaseServiceError):
        start(case)
    assert fired and case.gateway.calls == []


@pytest.mark.parametrize("phase", ["before_commit", "before_claim", "after_claim"])
def test_crash_reopen_never_blindly_resends(
    tmp_path: Path, phase: str, monkeypatch: pytest.MonkeyPatch
):
    class CrashDispatch(SingleAttemptSpy):
        def create_once(self, *args, **kwargs):
            raise KeyboardInterrupt("simulated process loss before HTTP")

    case = make_case(
        tmp_path / "agent.db", dispatch=CrashDispatch() if phase == "after_claim" else None
    )
    if phase == "before_commit":
        original = SqliteD1UnitOfWork.apply

        def apply(uow, request, *, owner_id):
            original(uow, request, owner_id=owner_id)
            raise KeyboardInterrupt("simulated process loss")

        monkeypatch.setattr(SqliteD1UnitOfWork, "apply", apply)
    if phase == "before_claim":

        def claim(*args, **kwargs):
            raise KeyboardInterrupt("simulated process loss")

        monkeypatch.setattr(SqliteD1UnitOfWork, "claim", claim)
    with pytest.raises(KeyboardInterrupt):
        start(case)
    assert case.gateway.calls == []
    state = case.gate.recovery_status("run-a")
    assert (state[0] if state else None) == {
        "before_commit": None,
        "before_claim": "reserved",
        "after_claim": "uncertain",
    }[phase]
    before = snapshot(case)
    if phase != "before_commit":
        with pytest.raises(PurchaseServiceError) as exc:
            start(case)
        assert exc.value.code == "d1_recovery_required"
        assert snapshot(case) == before


@pytest.mark.parametrize(
    "fault",
    [
        "timeout",
        "malformed",
        "invalid-instance",
        "variant",
        "currency",
        "identity",
        "foreign-known-id",
    ],
)
def test_uncertain_dispatch_preserves_identity_and_blocks_retry(tmp_path: Path, fault: str, caplog):
    case = make_case(tmp_path / "agent.db", fault=fault)
    if fault == "foreign-known-id":
        case.store.create_purchase("other", "other", OWNER)
        case.store.update_purchase("other", stage="checkout_prepared", checkout_id="cs_1")
    with pytest.raises(PurchaseServiceError) as exc:
        start(case)
    assert exc.value.code == "d1_dispatch_uncertain"
    before = snapshot(case)
    assert before[0] == ("uncertain", 3, None)
    status = case.gate.recovery_status("run-a")
    assert status is not None and status[0:2] == ("uncertain", None)
    with pytest.raises(PurchaseServiceError):
        start(case)
    assert sum(name == "create" for name, _ in case.gateway.calls) == 1
    assert snapshot(case) == before and MARKER not in caplog.text
    record = records(case)[0]
    assert isinstance(record, DecisionRecord) and record.decision.outcome == "allow"
    assert OWNER.encode() not in canonical_bytes(record) and MARKER.encode() not in canonical_bytes(
        record
    )
    assert before[1][0][2].encode() not in canonical_bytes(record)


def test_no_default_dispatch_or_public_enforce_path(tmp_path: Path):
    case = make_case(tmp_path / "agent.db", blocked=True)
    with pytest.raises(PurchaseServiceError) as exc:
        start(case)
    assert exc.value.code == "d1_dispatch_blocked" and case.gateway.calls == []
    status = case.gate.recovery_status("run-a")
    assert status is not None and status[0] == "reserved"
    assert Settings(_env_file=None).agent_governance_mode == "off"  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_governance_mode="enforce")  # type: ignore[call-arg]


def test_two_purchase_attempts_one_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    case = make_case(tmp_path / "agent.db")
    barrier = Barrier(2)
    original = case.gate.capture

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.wait(timeout=30)
        return result

    monkeypatch.setattr(case.gate, "capture", capture)

    def attempt(_):
        try:
            return start(case).stage
        except PurchaseServiceError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (1, 2)))
    assert sorted(results) == ["awaiting_confirmation", "blocked"]
    assert sum(name == "create" for name, _ in case.gateway.calls) == 1
    assert len(records(case)) == 1


def test_primary_rank_once_and_independent_uow_rank_once(tmp_path: Path):
    case = make_case(tmp_path / "agent.db")
    calls = []

    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "rank_candidates":
            calls.append(Path(frame.f_back.f_code.co_filename).name)

    sys.setprofile(profile)
    try:
        start(case)
    finally:
        sys.setprofile(None)
    assert calls.count("purchase.py") == 1 and calls.count("d1_uow_sqlite.py") == 1


def test_schema_init_repeated_ddl_rollback_and_incompatible(tmp_path: Path):
    path = tmp_path / "agent.db"
    SqliteAgentStore(path).initialize()
    with closing(connect(path)) as connection:
        store = SqliteD1DispatchStore(connection)
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        store.initialize()
        connection.rollback()
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='d1_enforce_inputs'"
        ).fetchone()
        connection.execute("CREATE TABLE d1_enforce_inputs(run_id TEXT)")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(EvidenceError):
                store.initialize()


def test_source_mutation_between_capture_and_rank_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    case = make_case(tmp_path / "agent.db")
    original = case.gate.capture

    def capture(feed: ProductFeed, intent, ranking):
        result = original(feed, intent, ranking)
        feed.products[0].variants[0].title = "changed after capture"
        return result

    monkeypatch.setattr(case.gate, "capture", capture)
    with pytest.raises(PurchaseServiceError):
        start(case)
    assert case.gateway.calls == []


def baseline(path: Path, *, shadow: bool, deny: bool = False):
    store = SqliteAgentStore(path)
    store.initialize()
    gateway = SpyGateway(path)
    observer = None
    if shadow:
        profile = ClockProfile(
            authority_source="system_utc",
            commercial_source="fixed_sandbox",
            authority_provider=None,
            commercial_provider=None,
            fixed_commercial_at="2026-09-20T12:00:00.000Z",
        )
        configuration = D1Configuration(
            governance_mode="shadow",
            merchant_base_url="http://127.0.0.1:4120",
            policy_set_id="backend-cop-d1",
            policy_revision=1,
            currency="COP",
            quantity=1,
            clock=profile,
        )
        keys = ProvenanceKeys(
            key_id="test-ephemeral",
            pseudonym_key=SecretBytes(secrets.token_bytes(32)),
            encryption_key=SecretBytes(secrets.token_bytes(32)),
        )
        policy, binding = (
            build_d1_policy(
                policy_set_id=configuration.policy_set_id, version=1, merchant_ref="denied-merchant"
            )
            if deny
            else (None, None)
        )
        observer = ShadowD1Observer(
            path,
            configuration,
            keys,
            ClockReader(profile),
            policy_bytes=policy,
            policy_binding=binding,
        )
    service = PurchaseService(
        feed=load_product_feed(FEED),
        checkout=gateway,
        store=store,
        approvals=ApprovalService(store, SecretStr(secrets.token_urlsafe(40)), clock=lambda: NOW),
        d1_observer=observer,
    )
    return service, gateway, observer


@pytest.mark.parametrize("mode", ["off", "shadow", "shadow-deny"])
def test_off_shadow_keep_same_commercial_result(tmp_path: Path, mode: str):
    service, gateway, observer = baseline(
        tmp_path / "baseline.db", shadow=mode != "off", deny=mode == "shadow-deny"
    )
    expected = service.start(
        run_id="run-a",
        owner_id=OWNER,
        request=MARKER,
        intent=PurchaseIntent(hard=HardConstraints(currency="COP")),
        selected_variant_id="SON-01",
        details=details(),
    )
    case = make_case(tmp_path / "enforce.db")
    actual = start(case)
    assert actual == expected
    assert [name for name, _ in gateway.calls] == ["create", "update", "get"]
    if mode == "shadow-deny":
        assert observer is not None and observer.diagnostics.last_outcome == "deny"


def test_two_workers_claim_only_one_durable_operation(tmp_path: Path):
    case = make_case(tmp_path / "agent.db", blocked=True)
    with pytest.raises(PurchaseServiceError):
        start(case)
    barrier = Barrier(2)

    def claim(index):
        with closing(connect(case.path)) as connection:
            policy = connection.execute(
                "SELECT policy_bytes FROM governance_episodes WHERE run_id='run-a'"
            ).fetchone()[0]
            row = records(case)[0]
            assert isinstance(row, DecisionRecord)
            uow = SqliteD1UnitOfWork(
                connection,
                keys=case.keys,
                policy_bytes=policy,
                policy_binding=row.decision.policy_binding,
            )
            operation = connection.execute(
                "SELECT operation_id FROM d1_checkout_operations"
            ).fetchone()[0]
            barrier.wait(timeout=30)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                return uow.claim(operation, claimant=f"worker-{index}", owner_id=OWNER)

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(claim, (1, 2))) == 1
    assert case.gateway.calls == []
    status = case.gate.recovery_status("run-a")
    assert status is not None and status[0] == "uncertain"


def test_invalid_configured_artifact_cannot_create(tmp_path: Path):
    case = make_case(tmp_path / "agent.db", deny=True)
    assert case.gate._binding is not None
    case.gate._binding = case.gate._binding.model_copy(update={"version": 2})
    with pytest.raises(PurchaseServiceError):
        start(case)
    assert case.gateway.calls == []
    assert case.store.purchase_state_version("run-a") == 1


def test_attempt_and_result_rows_are_immutable(tmp_path: Path):
    case = make_case(tmp_path / "agent.db")
    start(case)
    before = snapshot(case)
    with closing(connect(case.path)) as connection:
        for table in ("d1_enforce_inputs", "d1_checkout_results"):
            for command in (
                f"DELETE FROM {table}",
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
            ):
                with pytest.raises(sqlite3.IntegrityError), connection:
                    connection.execute(command)
    assert snapshot(case) == before


def test_retry_after_authorization_rollback_keeps_operation_and_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    case = make_case(tmp_path / "agent.db")
    original = SqliteD1UnitOfWork.apply

    def crash(uow, request, *, owner_id):
        original(uow, request, owner_id=owner_id)
        raise KeyboardInterrupt("crash before first authorization commit")

    monkeypatch.setattr(SqliteD1UnitOfWork, "apply", crash)
    with pytest.raises(KeyboardInterrupt):
        start(case)
    with closing(connect(case.path)) as connection:
        attempt = connection.execute(
            "SELECT operation_id,evidence_id,idempotency_key FROM d1_enforce_inputs"
        ).fetchone()
    monkeypatch.setattr(SqliteD1UnitOfWork, "apply", original)
    start(case)
    with closing(connect(case.path)) as connection:
        assert (
            connection.execute(
                "SELECT operation_id,evidence_id,idempotency_key FROM d1_checkout_operations"
            ).fetchone()
            == attempt
        )
    assert sum(name == "create" for name, _ in case.gateway.calls) == 1


def test_gate_cannot_be_combined_with_shadow(tmp_path: Path):
    case = make_case(tmp_path / "agent.db")
    _, _, observer = baseline(tmp_path / "shadow.db", shadow=True)
    with pytest.raises(ValueError, match="configured separately"):
        PurchaseService(
            feed=load_product_feed(FEED),
            checkout=case.gateway,
            store=case.store,
            approvals=ApprovalService(
                case.store, SecretStr(secrets.token_urlsafe(40)), clock=lambda: NOW
            ),
            d1_gate=case.gate,
            d1_observer=observer,
        )


def test_invalid_gate_result_cannot_fall_through_to_b0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    case = make_case(tmp_path / "agent.db")
    monkeypatch.setattr(case.gate, "create_checkout", lambda *args, **kwargs: None)
    with pytest.raises(PurchaseServiceError) as exc:
        start(case)
    assert exc.value.code == "d1_invalid_result" and case.gateway.calls == []
    assert case.store.purchase_state_version("run-a") == 1
