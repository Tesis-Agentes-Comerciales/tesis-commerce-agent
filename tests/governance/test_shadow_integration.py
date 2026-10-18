"""PurchaseService/runtime with real D1, vault and ledger; local spy gateways only."""

import json
import secrets
import sqlite3
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from typing import Any

import httpx
import pytest
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.feed import ProductFeed, load_product_feed
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.approval import ApprovalService
from commerce_agent.config import Settings
from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evaluator import GovernanceError, ReferencePolicyEvaluator
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.models import D1PolicyArtifact
from commerce_agent.governance.provenance import ProvenanceKeys
from commerce_agent.governance.shadow import (
    ShadowD1Observer,
    UnavailableShadowObserver,
    build_shadow_observer,
)
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.provenance_sqlite import SqliteD1ProvenanceStore
from commerce_agent.runtime import build_runtime
from tests.test_purchase_service import FakeCheckoutGateway, details

OWNER = "private-owner@example.test"
MARKER = "private-sensitive@example.test"
COMMERCIAL_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parents[1] / "fixtures/feed"


def configuration(path: Path, *, mode: str = "shadow", **changes: Any) -> Settings:
    return Settings(
        **{
            "_env_file": None,
            "agent_database_path": path,
            "agent_governance_mode": mode,
            "agent_d1_key_id": "test-ephemeral",
            "agent_d1_policy_set_id": "test-backend-d1",
            "agent_d1_policy_revision": "1",
            "agent_d1_hmac_key_hex": SecretStr(secrets.token_hex(32)),
            "agent_d1_aes_key_hex": SecretStr(secrets.token_hex(32)),
            "agent_sandbox_clock_at": COMMERCIAL_NOW,
            "agent_feed_source": "file",
            "agent_feed_path": FIXTURE,
            "merchant_bearer_token": SecretStr(secrets.token_urlsafe(32)),
            "merchant_webhook_secret": SecretStr(secrets.token_urlsafe(32)),
            "agent_approval_secret": SecretStr(secrets.token_urlsafe(40)),
            "payment_agent_bearer_token": SecretStr(secrets.token_urlsafe(32)),
            **changes,
        },
    )


class SpyGateway(FakeCheckoutGateway):
    def __init__(self, path: Path, **kwargs: Any):
        super().__init__(**kwargs)
        self.path = path
        self.selected = "SON-01"
        self.before_create: Callable[[], None] | None = None

    def create_checkout(self, variant_id: str, *, currency: str, idempotency_key: str):
        # A second connection must acquire a write lock before every remote-effect boundary.
        with closing(sqlite3.connect(self.path, timeout=0.05)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        if self.before_create:
            self.before_create()
        self.calls.append(("create", idempotency_key))
        self.selected = variant_id
        self.current.line_items[0]["item"]["id"] = variant_id
        return self.current

    def update_checkout(self, checkout_id, update, *, idempotency_key):
        result = super().update_checkout(checkout_id, update, idempotency_key=idempotency_key)
        result.line_items[0]["item"]["id"] = self.selected
        return result


def service(
    settings: Settings,
    observer: Any = None,
    feed: ProductFeed | None = None,
    *,
    total: int = 74_000,
) -> tuple[PurchaseService, SpyGateway, SqliteAgentStore]:
    store = SqliteAgentStore(settings.agent_database_path)
    store.initialize()
    gateway = SpyGateway(settings.agent_database_path, total=total)
    approvals = ApprovalService(
        store, SecretStr(secrets.token_urlsafe(40)), clock=lambda: COMMERCIAL_NOW
    )
    return (
        PurchaseService(
            feed=feed or load_product_feed(FIXTURE),
            checkout=gateway,
            store=store,
            approvals=approvals,
            d1_observer=observer,
        ),
        gateway,
        store,
    )


def start(
    service: PurchaseService,
    *,
    intent: PurchaseIntent | None = None,
    feed: ProductFeed | None = None,
    selected: str = "SON-01",
):
    return service.start(
        run_id="run-real",
        request="private original request",
        intent=intent or PurchaseIntent(),
        selected_variant_id=selected,
        details=details(),
        graph_thread_id="private-thread@example.test",
        owner_id=OWNER,
        feed=feed,
    )


def records(path: Path) -> tuple[DecisionRecord, ...]:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        return tuple(
            item
            for item in SqliteGovernanceEvidenceStore(connection).for_run("run-real")
            if isinstance(item, DecisionRecord)
        )


def observer(settings: Settings) -> ShadowD1Observer:
    result = build_shadow_observer(settings, None)
    assert isinstance(result, ShadowD1Observer)
    return result


def test_same_commercial_result_operations_and_real_evidence_before_gateway(tmp_path: Path) -> None:
    off = configuration(tmp_path / "off.db", mode="off")
    off_service, off_gateway, _ = service(off)
    expected = start(off_service)
    settings = configuration(tmp_path / "shadow.db")
    observed = observer(settings)
    purchase, gateway, store = service(settings, observed)

    def already_recorded() -> None:
        persisted = records(settings.agent_database_path)
        assert len(persisted) == 1
        assert persisted[0].application.disposition == "observed_shadow"
        assert persisted[0].policy_input().expected_local_state_version == 2

    gateway.before_create = already_recorded
    calls = 0

    def profile(frame, event, arg):
        nonlocal calls
        if event == "call" and frame.f_code is rank_candidates.__code__:
            calls += 1

    original = sys.getprofile()
    try:
        sys.setprofile(profile)
        actual = start(purchase)
    finally:
        sys.setprofile(original)
    assert calls == 1
    assert actual == expected
    assert (
        [name for name, _ in gateway.calls]
        == [name for name, _ in off_gateway.calls]
        == ["create", "update", "get"]
    )
    assert store.purchase_state_version("run-real") == 4
    assert observed.diagnostics.status == "observed"
    record = records(settings.agent_database_path)[0]
    data = record.policy_input()
    assert data.run_id == "run-real" and data.owner_ref != OWNER
    assert data.commercial_now == "2026-09-20T12:00:00.000Z"
    assert data.authority_now.startswith(datetime.now(UTC).date().isoformat())
    with closing(sqlite3.connect(settings.agent_database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ledger = SqliteGovernanceEvidenceStore(connection)
        replay = ledger.replay(record.evidence_id)
        assert replay.outcome == record.decision.outcome == "allow"
        connection.execute("BEGIN")
        assert (
            settings.agent_d1_key_id
            and settings.agent_d1_hmac_key_hex
            and settings.agent_d1_aes_key_hex
        )
        keys = ProvenanceKeys(
            key_id=settings.agent_d1_key_id,
            pseudonym_key=SecretBytes(
                bytes.fromhex(settings.agent_d1_hmac_key_hex.get_secret_value())
            ),
            encryption_key=SecretBytes(
                bytes.fromhex(settings.agent_d1_aes_key_hex.get_secret_value())
            ),
        )
        vault = SqliteD1ProvenanceStore(connection, keys)
        assert vault.recover_intent("run-real", OWNER, data.intent.intent_ref) == PurchaseIntent()
        assert vault.recover_feed("run-real", OWNER, data.feed_snapshot_ref) == load_product_feed(
            FIXTURE
        )
        with pytest.raises(GovernanceError):
            vault.recover_feed("run-real", "wrong-owner", data.feed_snapshot_ref)
        connection.rollback()
        output = tmp_path / "example.json"
        output.write_text(
            json.dumps(
                {
                    "record": record.model_dump(mode="json"),
                    "replayed_decision": replay.model_dump(mode="json"),
                    "operations": [name for name, _ in gateway.calls],
                    "diagnostics": asdict(observed.diagnostics),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        with closing(sqlite3.connect(tmp_path / "example.sqlite")) as archive:
            connection.backup(archive)


def test_real_choice_can_be_other_than_rank_first(tmp_path: Path) -> None:
    feed = load_product_feed(FIXTURE)
    for product in feed.products:
        for variant in product.variants:
            variant.availability.available = True
    selection = rank_candidates(feed, PurchaseIntent())
    chosen = selection.ranked[-1].candidate.variant_id
    assert selection.selected is not None
    assert chosen != selection.selected.variant_id
    settings = configuration(tmp_path / "choice.db")
    purchase, _, _ = service(settings, observer(settings))
    start(purchase, feed=feed, selected=chosen)
    data = records(settings.agent_database_path)[0].policy_input()
    assert data.selected_variant_id == chosen
    assert set(data.eligible_variant_ids) == {
        item.candidate.variant_id for item in selection.ranked
    }


def test_pdp_and_export_never_receive_free_text_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = configuration(tmp_path / "privacy.db")
    feed = load_product_feed(FIXTURE)
    feed.products[0].title = MARKER
    assert feed.metadata.__pydantic_extra__ is not None
    feed.metadata.__pydantic_extra__["token"] = MARKER
    original = ReferencePolicyEvaluator.evaluate
    seen = []

    def inspect_input(self, data):
        encoded = canonical_bytes(data)
        for sensitive in (
            MARKER,
            OWNER,
            "private original request",
            "private-thread",
            "buyer@example.test",
            "Calle",
        ):
            assert sensitive.encode() not in encoded
        seen.append(data)
        return original(self, data)

    monkeypatch.setattr(ReferencePolicyEvaluator, "evaluate", inspect_input)
    purchase, _, _ = service(settings, observer(settings), feed)
    start(purchase)
    assert seen
    assert MARKER.encode() not in canonical_bytes(records(settings.agent_database_path)[0])


def test_verified_policy_deny_is_only_observational(tmp_path: Path) -> None:
    policy = D1PolicyArtifact(
        schema_version="commerce-policy/1.0",
        policy_set_id="restrictive-d1",
        version=1,
        policy_ids=("POL-01", "POL-02"),
        allowed_actions=(),
        allowed_merchant_refs=(),
        currency="COP",
        quantity=1,
    )
    payload = canonical_bytes(policy)
    path = tmp_path / "reviewed-policy.json"
    path.write_bytes(payload)
    settings = configuration(
        tmp_path / "deny.db",
        agent_d1_policy_set_id=policy.policy_set_id,
        agent_d1_policy_path=path,
        agent_d1_policy_digest=sha256_digest(payload),
    )
    purchase, gateway, _ = service(settings, observer(settings))
    assert start(purchase).stage == "awaiting_confirmation"
    record = records(settings.agent_database_path)[0]
    assert record.decision.outcome == "deny"
    assert record.decision.reason_codes == ("ACTION_OR_MERCHANT_NOT_ALLOWED",)
    assert [name for name, _ in gateway.calls] == ["create", "update", "get"]


@pytest.mark.parametrize("component", ["evaluator", "vault", "ledger", "admission", "observer"])
def test_observer_failures_do_not_block_commerce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any, component: str
) -> None:
    settings = configuration(tmp_path / "failure.db")
    observed = observer(settings)

    def fail(*args, **kwargs):
        raise RuntimeError(MARKER)

    cls, method = {
        "evaluator": (ReferencePolicyEvaluator, "evaluate"),
        "vault": (SqliteD1ProvenanceStore, "register_feed"),
        "ledger": (SqliteGovernanceEvidenceStore, "record_decision"),
        "admission": (SqliteGovernanceEvidenceStore, "prepare_decision"),
        "observer": (ShadowD1Observer, "observe"),
    }[component]
    monkeypatch.setattr(cls, method, fail)
    purchase, gateway, store = service(settings, observed)
    assert start(purchase).stage == "awaiting_confirmation"
    assert [name for name, _ in gateway.calls] == ["create", "update", "get"]
    assert store.purchase_state_version("run-real") == 4
    assert (
        observed.diagnostics.status == "degraded" and observed.diagnostics.last_evidence_id is None
    )
    assert observed.diagnostics.lost == 1 and MARKER not in caplog.text
    with closing(sqlite3.connect(settings.agent_database_path)) as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='governance_evidence'"
        ).fetchone():
            assert connection.execute("SELECT count(*) FROM governance_evidence").fetchone() == (0,)


@pytest.mark.parametrize("case", ["invalid_candidate", "no_candidate", "budget"])
def test_existing_guards_remain_effective(tmp_path: Path, case: str) -> None:
    settings = configuration(tmp_path / "guard.db")
    observed = observer(settings)
    purchase, gateway, _ = service(settings, observed, total=90_000)
    intent = (
        PurchaseIntent(hard=HardConstraints(max_total_amount_minor=80_000))
        if case == "budget"
        else (
            PurchaseIntent(hard=HardConstraints(max_unit_amount_minor=0))
            if case == "no_candidate"
            else PurchaseIntent()
        )
    )
    with pytest.raises(PurchaseServiceError):
        start(
            purchase,
            intent=intent,
            selected="nonexistent" if case == "invalid_candidate" else "SON-01",
        )
    if case == "budget":
        assert [name for name, _ in gateway.calls] == ["create", "cancel"]
        assert len(records(settings.agent_database_path)) == 1
    else:
        assert gateway.calls == [] and observed.diagnostics.observed == 0
        with closing(sqlite3.connect(settings.agent_database_path)) as connection:
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='governance_evidence'"
                ).fetchall()
                == []
            )


def test_equivalent_observation_retry_and_new_facts_have_distinct_decisions(tmp_path: Path) -> None:
    settings = configuration(tmp_path / "retry.db")
    observed = observer(settings)
    purchase, _, store = service(settings, observed)
    feed, intent = load_product_feed(FIXTURE), PurchaseIntent()
    start(purchase, feed=feed, intent=intent)
    first = records(settings.agent_database_path)[0]
    # B0's own start retry changes the real row version; it is a distinct evaluation.
    start(purchase, feed=feed, intent=intent)
    assert len(records(settings.agent_database_path)) == 2
    latest = records(settings.agent_database_path)[-1]
    assert (
        latest.policy_input().expected_local_state_version
        > first.policy_input().expected_local_state_version
    )
    capture = observed.capture(feed, intent, rank_candidates)
    assert capture is not None
    selection = rank_candidates(feed, intent)
    observed.observe(
        capture,
        run_id="run-real",
        owner_id=OWNER,
        selection=selection,
        selected_variant_id="SON-01",
    )
    current = records(settings.agent_database_path)[-1]
    count = len(records(settings.agent_database_path))
    observed.observe(
        capture,
        run_id="run-real",
        owner_id=OWNER,
        selection=selection,
        selected_variant_id="SON-01",
    )
    assert len(records(settings.agent_database_path)) == count
    assert observed.diagnostics.reused == 1
    assert records(settings.agent_database_path)[-1] == current
    feed.metadata.updated_at = "2026-09-21T12:00:00Z"
    changed_intent = PurchaseIntent(hard=HardConstraints(max_unit_amount_minor=100_000))
    start(purchase, feed=feed, intent=changed_intent)
    rows = records(settings.agent_database_path)
    assert rows[-1].policy_input().feed_snapshot_ref.version == 2
    assert rows[-1].policy_input().intent.intent_ref.version == 2
    assert len({row.decision.decision_id for row in rows}) == len(rows)
    assert [row.sequence for row in rows] == list(range(1, len(rows) + 1))
    assert store.purchase_state_version("run-real") == 10
    with closing(sqlite3.connect(settings.agent_database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ledger = SqliteGovernanceEvidenceStore(connection)
        assert all(ledger.replay(row.evidence_id).outcome == row.decision.outcome for row in rows)


def test_mutation_or_loaded_algorithm_mismatch_is_observation_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = configuration(tmp_path / "drift.db")
    observed = observer(settings)
    purchase, _, store = service(settings, observed)
    store.create_purchase("run-real", "private original request", OWNER)
    feed, intent = load_product_feed(FIXTURE), PurchaseIntent()
    capture = observed.capture(feed, intent, rank_candidates)
    assert capture is not None
    selection = rank_candidates(feed, intent)
    feed.metadata.updated_at = "changed"
    observed.observe(
        capture,
        run_id="run-real",
        owner_id=OWNER,
        selection=selection,
        selected_variant_id="SON-01",
    )
    assert observed.diagnostics.status == "degraded"
    monkeypatch.setattr(
        "commerce_agent.application.purchase.rank_candidates", lambda f, i: rank_candidates(f, i)
    )
    assert start(purchase).stage == "awaiting_confirmation"
    assert observed.diagnostics.observed == 0


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_runtime_bootstrap_and_missing_shadow_keys_do_not_break_b0(
    tmp_path: Path, mode: str
) -> None:
    settings = configuration(
        tmp_path / "runtime.db",
        mode=mode,
        agent_d1_hmac_key_hex=None,
        agent_d1_aes_key_hex=None,
        agent_d1_policy_revision="invalid" if mode == "off" else "1",
    )
    remote = []

    def no_remote(request):
        remote.append(request.url)
        raise AssertionError("No bootstrap remote calls expected")

    runtime = build_runtime(
        settings,
        transport=httpx.MockTransport(no_remote),
        payment_transport=httpx.MockTransport(no_remote),
    )
    try:
        if mode == "off":
            assert runtime.d1_shadow is None
        else:
            assert isinstance(runtime.d1_shadow, UnavailableShadowObserver)
            assert runtime.d1_shadow.diagnostics.status == "degraded"
        assert remote == []
        with closing(sqlite3.connect(settings.agent_database_path)) as connection:
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name LIKE 'governance_%' OR name LIKE 'd1_%'"
                ).fetchall()
                == []
            )
        purchase, gateway, _ = service(settings, runtime.d1_shadow)
        assert start(purchase).stage == "awaiting_confirmation"
        assert len(gateway.calls) == 3
    finally:
        runtime.close()


def test_runtime_valid_shadow_is_injected_and_uses_sandbox_profile(tmp_path: Path) -> None:
    settings = configuration(tmp_path / "runtime-shadow.db")
    runtime = build_runtime(settings)
    try:
        assert isinstance(runtime.d1_shadow, ShadowD1Observer)
        # Replace only the external gateway with a spy; retain the runtime PurchaseService wiring.
        runtime.purchases._checkout = SpyGateway(settings.agent_database_path)
        assert start(runtime.purchases).stage == "awaiting_confirmation"
        assert runtime.d1_shadow.diagnostics.status == "observed"
        assert (
            records(settings.agent_database_path)[0].policy_input().commercial_now
            == "2026-09-20T12:00:00.000Z"
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "change",
    [
        {"agent_d1_policy_revision": "invalid"},
        {"agent_d1_aes_key_hex": SecretStr("invalid")},
        {"agent_d1_policy_digest": "sha256:" + "0" * 64},
    ],
)
def test_malformed_shadow_configuration_reports_degraded(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    settings = configuration(tmp_path / "malformed.db", **change)
    observed = build_shadow_observer(settings, None)
    assert isinstance(observed, UnavailableShadowObserver)
    purchase, _, _ = service(settings, observed)
    assert start(purchase).stage == "awaiting_confirmation"


def test_measured_off_shadow_latency_separate_from_bootstrap(tmp_path: Path) -> None:
    samples = []
    for index in range(5):
        pair = {}
        for mode in ("off", "shadow"):
            settings = configuration(tmp_path / f"latency-{index}-{mode}.db", mode=mode)
            setup = perf_counter_ns()
            observed = observer(settings) if mode == "shadow" else None
            setup_ms = (perf_counter_ns() - setup) / 1_000_000
            purchase, _, _ = service(settings, observed)
            began = perf_counter_ns()
            start(purchase)
            pair[mode] = {
                "start_ms": (perf_counter_ns() - began) / 1_000_000,
                "bootstrap_ms": setup_ms,
                "diagnostics": asdict(observed.diagnostics) if observed else None,
            }
            if observed:
                assert observed.diagnostics.status == "observed"
        samples.append(pair)
    output = {
        "environment": f"{sys.platform}; Python {sys.version_info.major}.{sys.version_info.minor}; SQLite {sqlite3.sqlite_version}; spy gateway; no network",
        "pairs": samples,
        "off_median_ms": median(s["off"]["start_ms"] for s in samples),
        "shadow_median_ms": median(s["shadow"]["start_ms"] for s in samples),
        "median_paired_added_ms": median(
            s["shadow"]["start_ms"] - s["off"]["start_ms"] for s in samples
        ),
    }
    (tmp_path / "latency.json").write_text(json.dumps(output, indent=2), encoding="utf-8")


def test_retry_after_ledger_failure_preserves_actual_attempt_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = configuration(tmp_path / "recover.db")
    observed = observer(settings)
    _, _, store = service(settings, observed)
    store.create_purchase("run-real", "private original request", OWNER)
    store.update_purchase("run-real", stage="selected", selected_variant_id="SON-01")
    feed, intent = load_product_feed(FIXTURE), PurchaseIntent()
    capture = observed.capture(feed, intent, rank_candidates)
    assert capture is not None
    selection = rank_candidates(feed, intent)
    with monkeypatch.context() as temporary:

        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic failure")

        temporary.setattr(SqliteGovernanceEvidenceStore, "record_decision", unavailable)
        observed.observe(
            capture,
            run_id="run-real",
            owner_id=OWNER,
            selection=selection,
            selected_variant_id="SON-01",
        )
    assert records(settings.agent_database_path) == ()
    with closing(sqlite3.connect(settings.agent_database_path)) as connection:
        previous = connection.execute(
            "SELECT canonical_input,evidence_id FROM d1_shadow_attempts"
        ).fetchone()
    reopened = observer(settings)
    capture = reopened.capture(feed, intent, rank_candidates)
    assert capture is not None
    reopened.observe(
        capture,
        run_id="run-real",
        owner_id=OWNER,
        selection=selection,
        selected_variant_id="SON-01",
    )
    record = records(settings.agent_database_path)[0]
    assert record.canonical_input_bytes.encode() == previous[0]
    assert record.evidence_id == previous[1] and record.sequence == 1


def test_two_observers_share_one_idempotent_record(tmp_path: Path) -> None:
    settings = configuration(tmp_path / "concurrent.db")
    first, second = observer(settings), observer(settings)
    _, _, store = service(settings)
    store.create_purchase("run-real", "private original request", OWNER)
    store.update_purchase("run-real", stage="selected", selected_variant_id="SON-01")
    feed, intent = load_product_feed(FIXTURE), PurchaseIntent()
    capture_a = first.capture(feed, intent, rank_candidates)
    capture_b = second.capture(feed, intent, rank_candidates)
    assert capture_a is not None and capture_b is not None
    selection = rank_candidates(feed, intent)

    def observe_pair(pair):
        current, captured = pair
        current.observe(
            captured,
            run_id="run-real",
            owner_id=OWNER,
            selection=selection,
            selected_variant_id="SON-01",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(observe_pair, [(first, capture_a), (second, capture_b)]))
    assert first.diagnostics.status == second.diagnostics.status == "observed"
    assert len(records(settings.agent_database_path)) == 1
    assert first.diagnostics.last_evidence_id == second.diagnostics.last_evidence_id


def test_replaced_internal_ranking_helper_is_observation_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = configuration(tmp_path / "helper-drift.db")
    observed = observer(settings)
    monkeypatch.setattr("commerce_agent.domain.selection._score", lambda candidate, intent: (0, ()))
    purchase, gateway, _ = service(settings, observed)
    assert start(purchase).stage == "awaiting_confirmation"
    assert len(gateway.calls) == 3
    assert observed.diagnostics.status == "degraded" and observed.diagnostics.observed == 0


def test_off_factory_skips_all_shadow_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(**{"_env_file": None, "agent_database_path": tmp_path / "off.db"})

    def forbidden():
        raise AssertionError("off measured policy sources")

    monkeypatch.setattr("commerce_agent.governance.shadow.measure_d1_sources", forbidden)
    assert build_shadow_observer(settings, None) is None
    assert not settings.agent_database_path.exists()


def test_non_cop_b0_purchase_is_not_misrepresented_as_cop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = configuration(tmp_path / "usd.db")
    observed = observer(settings)
    feed = load_product_feed(FIXTURE)
    for product in feed.products:
        for variant in product.variants:
            variant.price.currency = "usd"
    purchase, gateway, _ = service(settings, observed, feed)
    gateway.current = gateway.current.model_copy(update={"currency": "usd"})
    original = gateway.update_checkout

    def update(*args, **kwargs):
        result = original(*args, **kwargs)
        gateway.current = result.model_copy(update={"currency": "usd"})
        return gateway.current

    monkeypatch.setattr(gateway, "update_checkout", update)
    assert start(purchase).stage == "awaiting_confirmation"
    assert len(gateway.calls) == 3
    assert observed.diagnostics.status == "degraded" and observed.diagnostics.observed == 0
    assert observed.diagnostics.last_evidence_id is None
