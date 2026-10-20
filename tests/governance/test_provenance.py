"""D1 provenance from actual domain models and measured local sources; offline only."""

import json
import secrets
import sqlite3
import tomllib
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretBytes, ValidationError

from commerce_agent.acp.feed import ProductFeed, load_product_feed
from commerce_agent.domain import HardConstraints, OptionConstraint, PurchaseIntent, rank_candidates
from commerce_agent.governance import provenance_artifacts
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.evaluator import (
    GovernanceError,
    ReferencePolicyEvaluator,
    measure_reference_engine,
    verify_artifact,
)
from commerce_agent.governance.evidence import DecisionRecord, EvidenceApplication, EvidenceScope
from commerce_agent.governance.evidence_artifacts import capture_reference_sources
from commerce_agent.governance.models import (
    SCHEMA,
    EvaluationMetadata,
    ObjectRef,
    SelectionPolicyInput,
)
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    D1Configuration,
    ProvenanceKeys,
    RegisteredContext,
    build_d1_policy,
    configured_provenance,
)
from commerce_agent.governance.provenance_artifacts import (
    SOURCE_PATHS,
    lf_source,
    measure_d1_sources,
)
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.provenance_sqlite import SqliteD1ProvenanceStore

OWNER = "private-owner@example.test"
MARKER = "private-person-token@example.test"
NOW = datetime(2026, 10, 14, 12, 0, 1, 456789, tzinfo=UTC)


@pytest.fixture
def keys() -> ProvenanceKeys:
    # Ephemeral injection, no embedded key bytes, env files or persisted key material.
    return ProvenanceKeys(
        key_id="pytest-ephemeral",
        pseudonym_key=SecretBytes(secrets.token_bytes(32)),
        encryption_key=SecretBytes(secrets.token_bytes(32)),
    )


@pytest.fixture
def configuration() -> D1Configuration:
    return D1Configuration(
        governance_mode="shadow",
        merchant_base_url="http://127.0.0.1:4120",
        policy_set_id="backend-d1-cop",
        policy_revision=1,
        currency="COP",
        quantity=1,
        clock=ClockProfile(
            authority_source="injected_utc",
            authority_provider="test-authority",
            commercial_source="fixed_sandbox",
            commercial_provider=None,
            fixed_commercial_at="2026-09-20T12:00:00.000Z",
        ),
    )


@pytest.fixture
def feed() -> ProductFeed:
    return load_product_feed(Path(__file__).parents[1] / "fixtures/feed")


@pytest.fixture
def vault(tmp_path: Path, keys: ProvenanceKeys) -> Iterator[SqliteD1ProvenanceStore]:
    store = SqliteAgentStore(tmp_path / "test.db")
    store.initialize()
    store.create_purchase("run-a", "private request never copied into provenance", OWNER)
    store.create_purchase("run-b", "second request", OWNER)
    store.create_purchase("run-c", "third request", "other@example.test")
    store.save_graph_thread("run-a", "private-conversation@example.test")
    with closing(sqlite3.connect(tmp_path / "test.db")) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        result = SqliteD1ProvenanceStore(connection, keys)
        result.initialize()
        for run, owner in (("run-a", OWNER), ("run-b", OWNER), ("run-c", "other@example.test")):
            result.register_episode(run, owner, experiment_run_id=None)
        yield result
        connection.rollback()


@pytest.fixture
def context(vault: SqliteD1ProvenanceStore, configuration: D1Configuration) -> RegisteredContext:
    return vault.register_context("run-a", OWNER, configuration)


def test_intent_roundtrip_versions_and_historical_recovery(vault: SqliteD1ProvenanceStore) -> None:
    first = PurchaseIntent(hard=HardConstraints(currency="COP", max_total_amount_minor=100_000))
    second = PurchaseIntent(hard=HardConstraints(currency="COP", max_total_amount_minor=90_000))
    a = vault.register_intent("run-a", OWNER, first)
    assert a.version == 1
    assert vault.recover_intent("run-a", OWNER, a) == first
    assert vault.register_intent("run-a", OWNER, first) == a
    b = vault.register_intent("run-a", OWNER, second)
    assert b.id == a.id and b.version == 2 and b.digest != a.digest
    assert vault.recover_intent("run-a", OWNER, a) == first
    assert vault.recover_intent("run-a", OWNER, b) == second
    reverted = vault.register_intent("run-a", OWNER, first)
    assert reverted.version == 3 and reverted.digest == a.digest
    public = vault.public_source("run-a", OWNER, a, "intent")
    assert sha256_digest(public) == a.digest
    assert a.digest != sha256_digest(canonical_bytes(first))


def test_exact_feed_model_roundtrip_mutation_and_selection(
    vault: SqliteD1ProvenanceStore, feed: ProductFeed
) -> None:
    # The ranking consumes the ProductFeed model, not original HTTP JSON whitespace.
    original = feed.model_copy(deep=True)
    ref = vault.register_feed("run-a", OWNER, feed)
    recovered = vault.recover_feed("run-a", OWNER, ref)
    assert recovered == original
    assert rank_candidates(recovered, PurchaseIntent()) == rank_candidates(
        original, PurchaseIntent()
    )
    feed.products[0].variants[0].title = "mutated title"
    feed.metadata.__pydantic_extra__["finite_measurement"] = 1.25  # type: ignore[index]
    changed = vault.register_feed("run-a", OWNER, feed)
    assert changed.id == ref.id and changed.version == 2 and changed.digest != ref.digest
    assert vault.recover_feed("run-a", OWNER, ref) == original
    assert vault.recover_feed("run-a", OWNER, changed) == feed
    assert vault.register_feed("run-a", OWNER, feed) == changed


@pytest.mark.parametrize(
    "target,owner",
    [
        ("run-b", OWNER),
        ("run-c", "other@example.test"),
        ("run-a", "wrong@example.test"),
        ("missing", OWNER),
    ],
)
def test_cross_purchase_or_owner_sources_are_rejected(
    vault: SqliteD1ProvenanceStore, target: str, owner: str
) -> None:
    ref = vault.register_intent("run-a", OWNER, PurchaseIntent())
    with pytest.raises(GovernanceError):
        vault.recover_intent(target, owner, ref)
    if target == "run-b":
        other = vault.register_intent(target, owner, PurchaseIntent())
        assert other.id != ref.id and other.digest != ref.digest


def test_episode_retry_persists_trace_and_private_conversation(
    vault: SqliteD1ProvenanceStore, configuration: D1Configuration
) -> None:
    trace = vault.register_episode("run-a", OWNER, experiment_run_id=None)
    vault.connection.commit()
    vault.connection.execute("BEGIN IMMEDIATE")
    vault.initialize()
    assert vault.register_episode("run-a", OWNER, experiment_run_id=None) == trace
    with pytest.raises(GovernanceError, match="GOV_STATE_CONFLICT"):
        vault.register_episode("run-a", OWNER, experiment_run_id="different-experiment")
    context = vault.register_context("run-a", OWNER, configuration)
    assert context.data.trace_id == trace
    assert context.data.conversation_id.startswith("conversation:")  # type: ignore[union-attr]
    assert OWNER not in canonical_bytes(context).decode()
    assert "private-conversation" not in canonical_bytes(context).decode()
    assert vault.register_context("run-a", OWNER, configuration) == context


def test_measured_sources_dependencies_and_lf_profile(
    vault: SqliteD1ProvenanceStore, context: RegisteredContext
) -> None:
    measured = measure_d1_sources()
    payload = vault.public_source(
        "run-a", OWNER, context.data.eligibility_algorithm_ref, "algorithm"
    )
    assert payload == measured["algorithm"]
    manifest = json.loads(payload)
    assert set(manifest["sources"]) == set(SOURCE_PATHS)
    root = Path(__file__).parents[2]
    for path, source in manifest["sources"].items():
        assert source == lf_source((root / path).read_bytes())
        assert "\r" not in source
    assert len(manifest["dependencies"]) == 10
    assert all(
        d["version"] and d["verified_files_digest"].startswith("sha256:")
        for d in manifest["dependencies"]
    )
    normal = vault.public_source("run-a", OWNER, context.data.normalization_ref, "normalization")
    assert normal == measured["normalization"]
    assert set(json.loads(normal)["sources"]) == {"src/commerce_agent/domain/intent.py"}
    assert lf_source(b"a\nb\n") == lf_source(b"a\r\nb\r\n")
    with pytest.raises(GovernanceError):
        lf_source(b"a\rb")


def test_policy_built_from_trusted_configuration_and_exact_binding(
    vault: SqliteD1ProvenanceStore, context: RegisteredContext
) -> None:
    payload = vault.public_source("run-a", OWNER, context.data.policy_artifact_ref, "policy")
    artifact = verify_artifact(payload, context.data.expected_policy)
    assert artifact.allowed_merchant_refs == (context.data.merchant_ref,)
    assert artifact.allowed_actions == ("prepare_checkout",)
    assert artifact.currency == "COP" and artifact.quantity == 1
    assert artifact.policy_set_id == "backend-d1-cop"
    assert context.data.policy_artifact_ref.digest == context.data.expected_policy.artifact_digest
    assert b"offline-d1" not in payload and b"merchant-1" not in payload
    with pytest.raises(GovernanceError, match="GOV_ARTIFACT_MISMATCH"):
        verify_artifact(payload + b" ", context.data.expected_policy)
    wrong = context.data.expected_policy.model_copy(update={"version": 2})
    with pytest.raises(GovernanceError, match="GOV_ARTIFACT_MISMATCH"):
        verify_artifact(payload, wrong)


def test_context_configuration_versions_are_real(
    vault: SqliteD1ProvenanceStore, context: RegisteredContext, configuration: D1Configuration
) -> None:
    clock = configuration.clock.model_copy(
        update={"fixed_commercial_at": "2026-09-21T12:00:00.000Z"}
    )
    newer = vault.register_context(
        "run-a", OWNER, configuration.model_copy(update={"clock": clock})
    )
    assert newer.ref.id == context.ref.id and newer.ref.version == context.ref.version + 1
    assert newer.data.clock_profile_ref.version == 2
    assert newer.data.policy_artifact_ref == context.data.policy_artifact_ref
    assert vault.recover_context("run-a", OWNER, context.ref) == context
    changed_merchant = configuration.model_copy(
        update={"merchant_base_url": "http://127.0.0.1:4999"}
    )
    with pytest.raises(GovernanceError, match="GOV_ARTIFACT_MISMATCH"):
        vault.register_context("run-a", OWNER, changed_merchant)
    revised = vault.register_context(
        "run-a", OWNER, changed_merchant.model_copy(update={"policy_revision": 2})
    )
    assert revised.data.expected_policy.version == 2
    assert revised.data.merchant_ref != context.data.merchant_ref
    assert revised.data.policy_artifact_ref.version == 2


def test_clocks_and_real_state_revision_observation(
    tmp_path: Path,
    vault: SqliteD1ProvenanceStore,
    context: RegisteredContext,
    configuration: D1Configuration,
) -> None:
    reader = ClockReader(configuration.clock, authority=lambda: NOW)
    observed = vault.observe_context("run-a", OWNER, context.ref, reader)
    assert observed.expected_local_state_version == 1
    assert observed.clocks.authority_now == "2026-10-14T12:00:01.456Z"
    assert observed.clocks.commercial_now == "2026-09-20T12:00:00.000Z"
    vault.connection.commit()
    SqliteAgentStore(tmp_path / "test.db").update_purchase("run-a", stage="selected")
    vault.connection.execute("BEGIN IMMEDIATE")
    assert (
        vault.observe_context("run-a", OWNER, context.ref, reader).expected_local_state_version == 2
    )
    assert vault.observe_context("run-a", OWNER, context.ref, reader).context == context
    # Observing never performs a commercial transition or invents a CAS permission.
    assert vault.connection.execute(
        "SELECT state_version FROM purchases WHERE run_id='run-a'"
    ).fetchone() == (2,)


def test_clock_profile_mismatch_and_naive_clock_fail_closed(
    vault: SqliteD1ProvenanceStore, context: RegisteredContext, configuration: D1Configuration
) -> None:
    other = ClockProfile(
        authority_source="system_utc",
        authority_provider=None,
        commercial_source="system_utc",
        commercial_provider=None,
        fixed_commercial_at=None,
    )
    with pytest.raises(GovernanceError):
        vault.observe_context("run-a", OWNER, context.ref, ClockReader(other))
    with pytest.raises(GovernanceError):
        ClockReader(configuration.clock)
    with pytest.raises(GovernanceError):
        ClockReader(configuration.clock, authority=lambda: NOW.replace(tzinfo=None)).read()
    real = ClockReader(other).read()
    assert real.authority_now.endswith("Z") and real.commercial_now.endswith("Z")
    injected = other.model_copy(
        update={"commercial_source": "injected_utc", "commercial_provider": "synthetic-clock"}
    )
    assert ClockReader(injected, commercial=lambda: NOW).read().commercial_now == (
        "2026-10-14T12:00:01.456Z"
    )


def test_intent_facts_constraint_references_are_resolvable_and_private(
    vault: SqliteD1ProvenanceStore, context: RegisteredContext
) -> None:
    intent = PurchaseIntent(
        hard=HardConstraints(
            currency="COP",
            max_unit_amount_minor=90_000,
            max_total_amount_minor=100_000,
            categories=(MARKER,),
            conditions=("private condition",),
            required_terms=(MARKER, MARKER),
            required_options=(OptionConstraint(name="private option", value=MARKER),),
        )
    )
    ref = vault.register_intent("run-a", OWNER, intent)
    facts = vault.intent_facts("run-a", OWNER, ref, context.ref)
    assert facts.quantity == 1 and facts.currency == "COP"
    assert facts.max_unit_minor == 90_000 and facts.max_total_minor == 100_000
    assert len(facts.hard_constraint_refs) == 4
    assert vault.intent_facts("run-a", OWNER, ref, context.ref) == facts
    constraints = [
        vault.recover_constraint("run-a", OWNER, item) for item in facts.hard_constraint_refs
    ]
    assert {item["kind"] for item in constraints} == {"category", "condition", "term", "option"}
    assert MARKER not in canonical_bytes(facts).decode()
    assert facts.normalization_version == (
        f"{context.data.normalization_ref.id}:v{context.data.normalization_ref.version}"
    )
    with pytest.raises(GovernanceError):
        vault.recover_constraint("run-b", OWNER, facts.hard_constraint_refs[0])


@pytest.mark.parametrize("currency", [None, "COP", "USD"])
def test_currency_comes_from_explicit_profile_without_conversion(
    vault: SqliteD1ProvenanceStore, context: RegisteredContext, currency: str | None
) -> None:
    intent = PurchaseIntent(hard=HardConstraints(currency=currency))
    ref = vault.register_intent("run-a", OWNER, intent)
    if currency == "USD":
        with pytest.raises(GovernanceError, match="GOV_FACTS_UNAVAILABLE"):
            vault.intent_facts("run-a", OWNER, ref, context.ref)
    else:
        facts = vault.intent_facts("run-a", OWNER, ref, context.ref)
        assert facts.currency == "COP" and facts.max_total_minor is None
    assert vault.recover_intent("run-a", OWNER, ref).hard.currency == currency


def test_adversarial_payloads_stay_in_encrypted_operational_storage(
    vault: SqliteD1ProvenanceStore, context: RegisteredContext, feed: ProductFeed, caplog: Any
) -> None:
    feed.products[0].variants[0].title = MARKER
    feed.metadata.__pydantic_extra__["token"] = MARKER  # type: ignore[index]
    ref = vault.register_feed("run-a", OWNER, feed)
    intent_ref = vault.register_intent(
        "run-a", OWNER, PurchaseIntent(hard=HardConstraints(required_terms=(MARKER,)))
    )
    facts = vault.intent_facts("run-a", OWNER, intent_ref, context.ref)
    for column in ("public_body", "ciphertext"):
        for row in vault.connection.execute(f"SELECT {column} FROM d1_provenance_sources"):
            assert MARKER.encode() not in row[0]
            assert OWNER.encode() not in row[0]
    assert MARKER not in canonical_bytes(facts).decode()
    assert MARKER not in vault.public_source("run-a", OWNER, ref, "feed").decode()
    assert vault.recover_feed("run-a", OWNER, ref).products[0].variants[0].title == MARKER
    assert caplog.text == ""
    bad = ref.model_copy(update={"digest": "sha256:" + "0" * 64})
    with pytest.raises(GovernanceError) as error:
        vault.recover_feed("run-a", OWNER, bad)
    assert str(error.value) == "GOV_FACTS_UNAVAILABLE"
    assert MARKER not in str(error.value)


@pytest.mark.parametrize("column", ["public_body", "ciphertext", "nonce", "digest"])
def test_corruption_rejected_even_on_retry(vault: SqliteD1ProvenanceStore, column: str) -> None:
    intent = PurchaseIntent()
    ref = vault.register_intent("run-a", OWNER, intent)
    # Deliberately bypass append-only guard to emulate storage corruption, not a supported writer.
    vault.connection.execute("DROP TRIGGER d1_provenance_sources_no_update")
    value: bytes | str = "sha256:" + "0" * 64 if column == "digest" else b"corrupt"
    vault.connection.execute(
        f"UPDATE d1_provenance_sources SET {column}=? WHERE source_id=?", (value, ref.id)
    )
    with pytest.raises(GovernanceError):
        vault.recover_intent("run-a", OWNER, ref)
    with pytest.raises(GovernanceError):
        vault.register_intent("run-a", OWNER, intent)


def test_wrong_keys_and_missing_transaction_fail_closed(
    vault: SqliteD1ProvenanceStore, keys: ProvenanceKeys
) -> None:
    wrong = keys.model_copy(update={"encryption_key": SecretBytes(secrets.token_bytes(32))})
    other = SqliteD1ProvenanceStore(vault.connection, wrong)
    with pytest.raises(GovernanceError):
        other.initialize()
    vault.connection.commit()
    with pytest.raises(GovernanceError):
        vault.register_intent("run-a", OWNER, PurchaseIntent())


def test_rollback_failed_persistence_and_reopen_recover(
    vault: SqliteD1ProvenanceStore, tmp_path: Path, keys: ProvenanceKeys
) -> None:
    ref = vault.register_intent("run-a", OWNER, PurchaseIntent())
    vault.connection.commit()
    vault.connection.execute("BEGIN IMMEDIATE")
    newer = vault.register_intent(
        "run-a", OWNER, PurchaseIntent(hard=HardConstraints(max_total_amount_minor=42))
    )
    vault.connection.rollback()
    with closing(sqlite3.connect(tmp_path / "test.db")) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        reopened = SqliteD1ProvenanceStore(connection, keys)
        reopened.initialize()
        assert reopened.recover_intent("run-a", OWNER, ref) == PurchaseIntent()
        with pytest.raises(GovernanceError):
            reopened.recover_intent("run-a", OWNER, newer)
        connection.execute("""CREATE TRIGGER fail_snapshot BEFORE INSERT ON d1_provenance_sources
                           BEGIN SELECT RAISE(ABORT, 'private failure must not escape'); END""")
        with pytest.raises(GovernanceError) as error:
            reopened.register_intent(
                "run-a", OWNER, PurchaseIntent(hard=HardConstraints(max_total_amount_minor=43))
            )
        assert str(error.value) == "GOV_EVIDENCE_UNAVAILABLE"
        connection.rollback()


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_append_only_guards(vault: SqliteD1ProvenanceStore, operation: str) -> None:
    vault.register_intent("run-a", OWNER, PurchaseIntent())
    sql = {
        "UPDATE": "UPDATE d1_provenance_sources SET digest=digest",
        "DELETE": "DELETE FROM d1_provenance_sources",
        "REPLACE": (
            "INSERT OR REPLACE INTO d1_provenance_sources SELECT * FROM d1_provenance_sources"
        ),
    }
    with pytest.raises(sqlite3.IntegrityError):
        vault.connection.execute(sql[operation])


def test_off_without_keys_tables_or_factory(tmp_path: Path) -> None:
    def forbidden() -> None:
        raise AssertionError("off invoked provenance setup")

    assert configured_provenance("off", forbidden) is None
    store = SqliteAgentStore(tmp_path / "off.db")
    store.initialize()
    store.create_purchase("b0-run", "ordinary B0")
    store.update_purchase("b0-run", stage="selected")
    assert store.purchase_state_version("b0-run") == 2
    with closing(sqlite3.connect(tmp_path / "off.db")) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'd1_provenance_%'"
            ).fetchall()
            == []
        )


@pytest.mark.parametrize(
    "change",
    [
        {"currency": "USD"},
        {"quantity": 2},
        {"quantity": True},
        {"governance_mode": "enforce"},
        {"policy_revision": True},
        {"unexpected": MARKER},
    ],
)
def test_configuration_is_strict(configuration: D1Configuration, change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        D1Configuration.model_validate({**configuration.model_dump(mode="python"), **change})


def test_policy_construction_rejects_forged_binding() -> None:
    body, binding = build_d1_policy(
        policy_set_id="backend-config", version=7, merchant_ref="merchant:backend"
    )
    assert verify_artifact(body, binding).version == 7
    with pytest.raises(GovernanceError):
        verify_artifact(body, binding.model_copy(update={"policy_set_id": "unrelated"}))


def test_policy_revision_identity_is_global_and_failed_registration_is_atomic(
    vault: SqliteD1ProvenanceStore, context: RegisteredContext, configuration: D1Configuration
) -> None:
    count = vault.connection.execute("SELECT count(*) FROM d1_provenance_sources").fetchone()[0]
    other = configuration.model_copy(update={"merchant_base_url": "http://127.0.0.1:4999"})
    with pytest.raises(GovernanceError, match="GOV_ARTIFACT_MISMATCH"):
        vault.register_context("run-b", OWNER, other)
    assert (
        vault.connection.execute("SELECT count(*) FROM d1_provenance_sources").fetchone()[0]
        == count
    )
    registered = vault.register_context("run-b", OWNER, configuration)
    assert registered.data.expected_policy == context.data.expected_policy
    assert registered.data.policy_artifact_ref.id != context.data.policy_artifact_ref.id


def test_source_change_changes_measured_identity_without_editing_business_files(
    vault: SqliteD1ProvenanceStore,
    context: RegisteredContext,
    configuration: D1Configuration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read = Path.read_bytes

    def changed(path: Path) -> bytes:
        body = original_read(path)
        if path.as_posix().endswith("src/commerce_agent/domain/selection.py"):
            return body + b"\n# simulated code drift\n"
        return body

    monkeypatch.setattr(Path, "read_bytes", changed)
    # A simulated read leaves file stats intact, so drop the per-process measurement.
    monkeypatch.setattr(provenance_artifacts, "_memo", None)
    updated = vault.register_context("run-a", OWNER, configuration)
    assert updated.data.eligibility_algorithm_ref.version == 2
    assert (
        updated.data.eligibility_algorithm_ref.digest
        != context.data.eligibility_algorithm_ref.digest
    )
    assert updated.data.normalization_ref == context.data.normalization_ref
    assert updated.ref.version == 2


def test_installed_dependency_tamper_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    original_read = Path.read_bytes

    def changed(path: Path) -> bytes:
        if path.as_posix().endswith("site-packages/pydantic/__init__.py"):
            return b"simulated dependency tamper"
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", changed)
    monkeypatch.setattr(provenance_artifacts, "_memo", None)
    with pytest.raises(GovernanceError, match="GOV_ARTIFACT_MISMATCH"):
        measure_d1_sources()


def test_wheel_layout_measures_package_data_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = Path(__file__).parents[2]
    wheel = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))
    assert wheel["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"] == {
        "uv.lock": "commerce_agent/uv.lock"
    }
    package = tmp_path / "site-packages" / "commerce_agent"
    for path in (*SOURCE_PATHS, "uv.lock"):
        target = package / path.removeprefix("src/commerce_agent/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((checkout / path).read_bytes())
    expected = measure_d1_sources()
    # No checkout root beside an installed wheel: only package data is available.
    monkeypatch.setattr(provenance_artifacts, "files", lambda _name: package)
    monkeypatch.setattr(
        provenance_artifacts, "__file__", str(package / "governance/provenance_artifacts.py")
    )
    monkeypatch.setattr(provenance_artifacts, "_memo", None)
    assert measure_d1_sources() == expected
    (package / "uv.lock").unlink()
    monkeypatch.setattr(provenance_artifacts, "_memo", None)
    with pytest.raises(GovernanceError, match="GOV_ARTIFACT_MISMATCH"):
        measure_d1_sources()


def test_sources_hashed_once_until_a_measured_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "selection.py"
    source.write_bytes(b"first")
    reads: list[bytes] = []

    def measure(read: list[Path]) -> dict[str, bytes]:
        read.append(source)
        reads.append(source.read_bytes())
        return {"algorithm": reads[-1], "normalization": b""}

    monkeypatch.setattr(provenance_artifacts, "_measure", measure)
    monkeypatch.setattr(provenance_artifacts, "_memo", None)
    # Just written: inside the racy window, so every call measures again.
    assert measure_d1_sources() == measure_d1_sources()
    assert reads == [b"first", b"first"]
    monkeypatch.setattr(provenance_artifacts, "_RACY_NS", 0)
    for _ in range(3):
        assert measure_d1_sources()["algorithm"] == b"first"
    assert len(reads) == 4  # One measurement and its confirmation after the stat.
    source.write_bytes(b"changed")
    assert measure_d1_sources()["algorithm"] == measure_d1_sources()["algorithm"] == b"changed"
    assert reads[4:] == [b"changed", b"changed"]


def test_write_during_measurement_is_never_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "selection.py"
    source.write_bytes(b"x")
    observed = iter([b"before", b"after", b"after", b"after"])

    def measure(read: list[Path]) -> dict[str, bytes]:
        read.append(source)
        return {"algorithm": next(observed), "normalization": b""}

    monkeypatch.setattr(provenance_artifacts, "_measure", measure)
    monkeypatch.setattr(provenance_artifacts, "_memo", None)
    monkeypatch.setattr(provenance_artifacts, "_RACY_NS", 0)
    assert measure_d1_sources()["algorithm"] == b"before"
    assert provenance_artifacts._memo is None
    assert measure_d1_sources()["algorithm"] == measure_d1_sources()["algorithm"] == b"after"


def test_purchase_paths_reuse_one_measurement_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.governance.test_enforce_integration import baseline, make_case, start
    from tests.test_purchase_service import details

    measured = provenance_artifacts._measure
    calls: list[int] = []

    def counted(read: list[Path]) -> provenance_artifacts.Measured:
        calls.append(len(read))
        return measured(read)

    monkeypatch.setattr(provenance_artifacts, "_measure", counted)
    monkeypatch.setattr(provenance_artifacts, "_memo", None)
    # Independent of how recently the checkout or environment was written.
    monkeypatch.setattr(provenance_artifacts, "_RACY_NS", 0)
    for index in range(2):
        assert start(make_case(tmp_path / f"enforce-{index}.db")).stage == "awaiting_confirmation"
        service, _, observer = baseline(tmp_path / f"shadow-{index}.db", shadow=True)
        result = service.start(
            run_id="run-a",
            owner_id=OWNER,
            request="shadow",
            intent=PurchaseIntent(hard=HardConstraints(currency="COP")),
            selected_variant_id="SON-01",
            details=details(),
        )
        assert result.stage == "awaiting_confirmation"
        assert observer is not None and observer.diagnostics.observed == 1
    # Callers still compare on every use; only the first full hash and its check run.
    assert len(calls) == 2


@pytest.mark.parametrize("bad_field", ["budget", "extra"])
def test_constructed_invalid_intent_is_rejected_without_echo(
    vault: SqliteD1ProvenanceStore, bad_field: str
) -> None:
    intent = PurchaseIntent()
    if bad_field == "budget":
        intent = intent.model_copy(
            update={"hard": intent.hard.model_copy(update={"max_total_amount_minor": True})}
        )
    else:
        intent = intent.model_copy(
            update={
                "hard": intent.hard.model_copy(update={"required_terms": (MARKER,)}),
                "unexpected_secret": MARKER,
            }
        )
    with pytest.raises(GovernanceError) as error:
        vault.register_intent("run-a", OWNER, intent)
    assert MARKER not in str(error.value)


def test_owner_change_invalidates_previously_registered_sources(
    vault: SqliteD1ProvenanceStore,
) -> None:
    ref = vault.register_intent("run-a", OWNER, PurchaseIntent())
    vault.connection.execute(
        "UPDATE purchases SET owner_id='changed-owner',state_version=state_version+1 "
        "WHERE run_id='run-a'"
    )
    for owner in (OWNER, "changed-owner"):
        with pytest.raises(GovernanceError):
            vault.recover_intent("run-a", owner, ref)


def test_initialization_requires_fk_and_explicit_transaction(
    tmp_path: Path, keys: ProvenanceKeys
) -> None:
    store = SqliteAgentStore(tmp_path / "fresh.db")
    store.initialize()
    with closing(sqlite3.connect(tmp_path / "fresh.db")) as connection:
        vault = SqliteD1ProvenanceStore(connection, keys)
        with pytest.raises(GovernanceError):
            vault.initialize()
        connection.execute("BEGIN")
        with pytest.raises(GovernanceError):
            vault.initialize()
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'd1_provenance_%'"
            ).fetchall()
            == []
        )


def test_provenance_refs_compatible_with_real_core_and_ledger_offline(
    vault: SqliteD1ProvenanceStore,
    context: RegisteredContext,
    configuration: D1Configuration,
    feed: ProductFeed,
) -> None:
    intent = PurchaseIntent(hard=HardConstraints(currency="COP"))
    intent_ref = vault.register_intent("run-a", OWNER, intent)
    feed_ref = vault.register_feed("run-a", OWNER, feed)
    facts = vault.intent_facts("run-a", OWNER, intent_ref, context.ref)
    observed = vault.observe_context(
        "run-a", OWNER, context.ref, ClockReader(configuration.clock, authority=lambda: NOW)
    )
    selection = rank_candidates(feed, intent)
    assert selection.selected is not None
    # Only this test assembles D1 from its observed synthetic operation; no production assembler.
    data = SelectionPolicyInput(
        schema_version=SCHEMA,
        decision_id="offline-observed-decision",
        run_id=context.data.run_id,
        trace_id=context.data.trace_id,
        owner_ref=context.data.owner_ref,
        purchase_mode="HP",
        governance_mode="shadow",
        phase="D1",
        action="prepare_checkout",
        context_version=context.ref.version,
        expected_local_state_version=observed.expected_local_state_version,
        expected_policy=context.data.expected_policy,
        authority_now=observed.clocks.authority_now,
        commercial_now=observed.clocks.commercial_now,
        clock_profile_ref=context.data.clock_profile_ref,
        experiment_run_id=None,
        conversation_id=context.data.conversation_id,
        intent=facts,
        feed_snapshot_ref=feed_ref,
        eligibility_algorithm_ref=context.data.eligibility_algorithm_ref,
        eligible_variant_ids=tuple(item.candidate.variant_id for item in selection.ranked),
        selected_variant_id=selection.selected.variant_id,
        merchant_ref=context.data.merchant_ref,
    )
    payload = vault.public_source("run-a", OWNER, context.data.policy_artifact_ref, "policy")
    identity = measure_reference_engine()
    result = ReferencePolicyEvaluator(
        artifact_bytes=payload,
        binding=context.data.expected_policy,
        metadata=EvaluationMetadata(
            schema_version=SCHEMA, decided_at=data.authority_now, latency_ms=0
        ),
        identity=identity,
    ).evaluate(data)
    assert result.outcome == "allow"
    sources = (
        intent_ref,
        feed_ref,
        context.data.clock_profile_ref,
        context.data.eligibility_algorithm_ref,
        context.data.normalization_ref,
        context.data.commercial_ref,
        context.ref,
    )
    assert identity.artifact_digest is not None
    engine = ObjectRef(
        schema_version=SCHEMA,
        id="measured-reference-source-archive",
        version=1,
        digest=identity.artifact_digest,
    )
    scope = EvidenceScope(
        schema_version=SCHEMA,
        run_id=data.run_id,
        trace_id=data.trace_id,
        owner_ref=data.owner_ref,
        purchase_mode="HP",
        governance_mode="shadow",
        phase="D1",
        context_version=data.context_version,
        policy_binding=data.expected_policy,
        source_refs=sources,
        policy_artifact_ref=context.data.policy_artifact_ref,
        engine_artifact_ref=engine,
    )
    record = DecisionRecord(
        schema_version=SCHEMA,
        evidence_id="offline-observed-evidence",
        sequence=1,
        recorded_at=data.authority_now,
        record_kind="decision_record",
        canonicalization_profile="commerce-canonical-json/1",
        canonical_input_bytes=canonical_bytes(data).decode(),
        decision=result,
        policy_artifact_ref=context.data.policy_artifact_ref,
        engine_artifact_ref=engine,
        source_refs=sources,
        application=EvidenceApplication(
            schema_version=SCHEMA,
            disposition="observed_shadow",
            transition_ref=None,
            expected_state_version=1,
            resulting_state_version=None,
        ),
        replayability="full",
    )
    ledger = SqliteGovernanceEvidenceStore(vault.connection)
    ledger.initialize()
    ledger.register_episode(scope, policy_bytes=payload, engine_bytes=capture_reference_sources())
    vault.connection.commit()
    ledger.prepare_decision(record)
    vault.connection.execute("BEGIN IMMEDIATE")
    ledger.record_decision(record, expected_state_version=1)
    assert ledger.get(record.evidence_id) == record
    exported = canonical_bytes(record)
    for private in (OWNER, "private-conversation", "Sonora", "buyer@example.test"):
        assert private.encode() not in exported
    assert parse_model(SelectionPolicyInput, canonical_bytes(data)) == data
