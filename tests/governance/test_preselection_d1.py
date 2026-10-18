"""Real approved D1 + SQLite; test issuer and controlled 07B-P setup, no real HTTP."""

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.authority_observation import AuthorityObservationClient, ObservationBlocked
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.models import SCHEMA, D1PolicyArtifact, PolicyBinding
from commerce_agent.governance.preselection import PreselectionAuthority
from commerce_agent.governance.preselection_d1 import PreselectionD1Binding
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    D1Configuration,
    ProvenanceKeys,
)
from commerce_agent.governance.provisioning import CatalogSource, EpisodeGrant
from commerce_agent.governance.shadow import ShadowD1Observer
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.preselection_d1_sqlite import SqlitePreselectionD1
from commerce_agent.persistence.preselection_sqlite import SqlitePreselectionStore
from commerce_agent.persistence.provisioning_sqlite import SqliteTestProvisioningRegistry
from commerce_agent.persistence.sqlite import SqliteAgentStore
from tests.governance.test_preselection import (
    DESTINATION,
    KEY,
    ORIGIN,
    ControlledMerchant,
)

REGISTRY_KEY = SecretBytes(b"registry-key-only-for-tests-32xxx")
LINK_KEY = SecretBytes(b"link-custody-only-for-tests-32xxx")
BEARER = SecretStr("synthetic-session")


def open_link(root, *, policy_bytes=None, policy_binding=None, bearer=BEARER):
    profile = ClockProfile(
        authority_source="system_utc",
        commercial_source="system_utc",
        authority_provider=None,
        commercial_provider=None,
        fixed_commercial_at=None,
    )
    config = D1Configuration(
        governance_mode="shadow",
        merchant_base_url=ORIGIN,
        policy_set_id="q-policy",
        policy_revision=1,
        currency="COP",
        quantity=1,
        clock=profile,
    )
    return SqlitePreselectionD1(
        root / "d1.sqlite",
        source=SqlitePreselectionStore(root / "source.sqlite", KEY),
        registry=SqliteTestProvisioningRegistry(root / "registry.sqlite", REGISTRY_KEY),
        bearer=bearer,
        custody_key=LINK_KEY,
        keys=ProvenanceKeys(
            key_id="q-test",
            pseudonym_key=SecretBytes(b"p" * 32),
            encryption_key=SecretBytes(b"e" * 32),
        ),
        configuration=config,
        clocks=ClockReader(profile),
        policy_bytes=policy_bytes,
        policy_binding=policy_binding,
    )


def setup_case(tmp_path, **options):
    registry = SqliteTestProvisioningRegistry(tmp_path / "registry.sqlite", REGISTRY_KEY)
    registry.initialize()
    merchant = ControlledMerchant()
    old = merchant.binding
    session = registry.issue_test_session(
        CatalogSource(
            event_id=str(uuid4()),
            merchant_origin=ORIGIN,
            merchant_id=old.merchant_id,
            remote_run_id=old.scope.remote_run_id,
            remote_actor_id=old.scope.remote_actor_id,
        ),
        BEARER,
    )
    merchant.binding = session.binding
    client = AuthorityObservationClient(session, transport=httpx.MockTransport(merchant.handle))
    source = SqlitePreselectionStore(tmp_path / "source.sqlite", KEY)
    source.initialize()
    flow = PreselectionAuthority(client, source)
    episode = flow.begin(owner_id="buyer", intent=PurchaseIntent(), destination=DESTINATION)
    seal = flow.capture(episode, owner_id="buyer")
    client.close()
    link = open_link(tmp_path, **options)
    store_id = link.initialize()
    grant = registry.grant_test_episode(
        EpisodeGrant(
            event_id=str(uuid4()),
            episode_id=episode,
            owner_id="buyer",
            d1_run_id=episode,
            d1_store_id=store_id,
            selection_digest=sha256_digest(canonical_bytes(seal)),
            receipt_ref=session.binding.source_ref,
        )
    )
    return link, link.request(episode, "buyer"), seal, registry, grant


def counters(path):
    with closing(sqlite3.connect(path)) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] if name in names else 0
            for name in (
                "orders",
                "d1_checkout_operations",
                "d1_enforce_inputs",
                "d1_checkout_envelopes",
                "completion_results",
                "operation_keys",
            )
        }


def test_new_seal_binds_real_policy_without_commercial_capability(tmp_path, monkeypatch):
    link, request, seal, _registry, _ = setup_case(tmp_path)
    before = counters(link.path)

    def forbidden(*args, **kwargs):
        pytest.fail("No HTTP allowed during Q")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    result = link.evaluate(request)
    assert (
        result.outcome == "allow" and result.operation_id is None and not result.dispatch_capability
    )
    assert result.request.run_id == seal.episode.episode_id
    assert result.offer_snapshot_sha256 == seal.observations[0].offer_snapshot_sha256
    assert result.provisioning.assurance == "test_backend_receipt_not_remote_authentication"
    assert result.provisioning.receipt_ref != result.provisioning.credential_ref
    assert result.provisioning.catalog_source_ref != result.provisioning.receipt_ref
    assert result.policy_binding.policy_set_id == "q-policy"
    assert counters(link.path) == before == dict.fromkeys(before, 0)
    with closing(sqlite3.connect(link.path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        record = SqliteGovernanceEvidenceStore(db).get(result.evidence_id)
        assert isinstance(record, DecisionRecord)
        assert record.decision.outcome == "allow"
        assert record.application.disposition == "observed_shadow"
        assert record.policy_input().run_id == request.run_id
        assert db.execute(
            "SELECT stage,state_version,checkout_id,order_id FROM purchases"
        ).fetchone() == ("received", 1, None, None)
        public = db.execute("SELECT body FROM q_d1_bindings").fetchone()[0]
        assert BEARER.get_secret_value().encode() not in public
    assert open_link(tmp_path).recover(request) == result
    with pytest.raises(ObservationBlocked):
        link.evaluate(request)
    action = "create_checkout"
    with pytest.raises(AttributeError):
        getattr(link, action)()


@pytest.mark.parametrize(
    "field,value",
    [
        ("feed_digest", "sha256:" + "1" * 64),
        ("intent_digest", "sha256:" + "2" * 64),
        ("selected_variant_id", "other"),
        ("owner_id", "foreign"),
        ("run_id", "old-run"),
        ("seal_digest", "sha256:" + "3" * 64),
    ],
)
def test_expected_facts_cannot_replace_store_selection(tmp_path, field, value):
    link, request, *_ = setup_case(tmp_path)
    with pytest.raises(ObservationBlocked):
        link.evaluate(request.model_copy(update={field: value}))
    with closing(sqlite3.connect(link.path)) as db:
        assert db.execute("SELECT count(*) FROM q_d1_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["offer", "revision", "digest", "actor", "remote_run"])
def test_seal_tamper_cannot_enter_d1(tmp_path, kind):
    link, request, seal, *_ = setup_case(tmp_path)
    value = seal.model_dump(mode="json")
    if kind == "offer":
        value["observations"][0]["offer"]["name"] = "altered"
    elif kind == "revision":
        value["observations"][0]["offer"]["revision"] += 1
    elif kind == "digest":
        value["observations"][0]["offer_snapshot_sha256"] = "0" * 64
    elif kind == "actor":
        value["episode"]["binding"]["scope"]["remote_actor_id"] = "other"
    else:
        value["episode"]["binding"]["scope"]["remote_run_id"] = str(uuid4())
    with closing(sqlite3.connect(tmp_path / "source.sqlite")) as db:
        db.execute("DROP TRIGGER pa_selections_UPDATE")
        db.execute("UPDATE pa_selections SET payload=?", (canonical_bytes(value),))
        db.commit()
    with pytest.raises(ObservationBlocked):
        link.evaluate(request)


@pytest.mark.parametrize(
    "failure",
    [
        "missing_registry",
        "missing_grant",
        "missing_bearer",
        "wrong_bearer",
        "revoked",
        "substituted_receipt",
    ],
)
def test_unproven_provisioning_never_captures_d1(tmp_path, failure):
    link, request, seal, registry, _ = setup_case(tmp_path)
    if failure == "missing_bearer":
        link._bearer = None
    elif failure == "wrong_bearer":
        link._bearer = SecretStr("another-session")
    elif failure == "revoked":
        registry.revoke_test_credential(seal.episode.binding.scope.source_ref)
    else:
        with closing(sqlite3.connect(tmp_path / "registry.sqlite")) as db:
            if failure == "missing_grant":
                db.execute("DROP TRIGGER q_registry_grants_DELETE")
                db.execute("DELETE FROM q_registry_grants")
            elif failure == "missing_registry":
                db.execute("DROP TRIGGER q_registry_objects_DELETE")
                db.execute(
                    "DELETE FROM q_registry_objects WHERE id=?",
                    (seal.episode.binding.source_ref.id,),
                )
            else:
                db.execute("DROP TRIGGER q_registry_objects_UPDATE")
                db.execute(
                    "UPDATE q_registry_objects SET body=? WHERE id=?",
                    (
                        b"{}",
                        seal.episode.binding.source_ref.id,
                    ),
                )
            db.commit()
    with pytest.raises(ObservationBlocked):
        link.evaluate(request)
    assert all(n == 0 for n in counters(link.path).values())


def test_different_context_and_store_rejected(tmp_path):
    link, request, *_ = setup_case(tmp_path)
    link.configuration = link.configuration.model_copy(
        update={"merchant_base_url": "https://other.example.test"}
    )
    with pytest.raises(ObservationBlocked):
        link.evaluate(request)
    other = open_link(tmp_path)
    other.path = tmp_path / "foreign.sqlite"
    other.initialize()
    with pytest.raises(ObservationBlocked):
        other.evaluate(request)


def test_valid_restrictive_policy_is_a_real_d1_deny(tmp_path):
    policy = D1PolicyArtifact(
        schema_version=SCHEMA,
        policy_set_id="q-policy",
        version=1,
        policy_ids=("POL-01", "POL-02"),
        allowed_actions=("prepare_checkout",),
        allowed_merchant_refs=("merchant:forbidden",),
        currency="COP",
        quantity=1,
    )
    raw = canonical_bytes(policy)
    binding = PolicyBinding(
        schema_version=SCHEMA,
        policy_set_id="q-policy",
        version=1,
        artifact_digest=sha256_digest(raw),
    )
    link, request, *_ = setup_case(tmp_path, policy_bytes=raw, policy_binding=binding)
    result = link.evaluate(request)
    assert result.outcome == "deny" and result.operation_id is None
    with closing(sqlite3.connect(link.path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        record = SqliteGovernanceEvidenceStore(db).get(result.evidence_id)
        assert isinstance(record, DecisionRecord)
        assert record.decision.origin == "policy"
        assert record.decision.reason_codes == ("ACTION_OR_MERCHANT_NOT_ALLOWED",)
    assert all(n == 0 for n in counters(link.path).values())


@pytest.mark.parametrize("raw", [b"{}", b"not-json"])
def test_invalid_policy_is_not_a_committed_allow(tmp_path, raw):
    binding = PolicyBinding(
        schema_version=SCHEMA,
        policy_set_id="q-policy",
        version=1,
        artifact_digest="sha256:" + "f" * 64,
    )
    link, request, *_ = setup_case(tmp_path, policy_bytes=raw, policy_binding=binding)
    with pytest.raises(ValueError):
        link.evaluate(request)
    with closing(sqlite3.connect(link.path)) as db:
        assert db.execute("SELECT count(*) FROM q_d1_bindings").fetchone()[0] == 0


def test_concurrent_claim_has_one_evaluation(tmp_path):
    link, request, *_ = setup_case(tmp_path)
    barrier = Barrier(2)

    def attempt():
        barrier.wait()
        try:
            return link.evaluate(request)
        except ObservationBlocked:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(isinstance(r, PreselectionD1Binding) for r in results) == 1
    with closing(sqlite3.connect(link.path)) as db:
        assert db.execute("SELECT count(*) FROM governance_evidence").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM q_d1_attempts").fetchone()[0] == 1


def test_crash_between_seal_and_capture_never_auto_resumes(tmp_path, monkeypatch):
    link, request, *_ = setup_case(tmp_path)

    def crash(*args):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(ShadowD1Observer, "capture", crash)
    with pytest.raises(RuntimeError):
        link.evaluate(request)
    restarted = open_link(tmp_path)
    with pytest.raises(ObservationBlocked):
        restarted.recover(request)
    with pytest.raises(ObservationBlocked):
        restarted.evaluate(request)


def test_crash_between_evaluation_and_ledger_persistence(tmp_path, monkeypatch):
    link, request, *_ = setup_case(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("injected ledger failure")

    monkeypatch.setattr(SqliteGovernanceEvidenceStore, "record_decision", crash)
    with pytest.raises(ObservationBlocked):
        link.evaluate(request)
    with pytest.raises(ObservationBlocked):
        open_link(tmp_path).recover(request)
    with closing(sqlite3.connect(link.path)) as db:
        assert db.execute("SELECT count(*) FROM q_d1_bindings").fetchone()[0] == 0


@pytest.mark.parametrize("already_bound", [False, True])
def test_actual_process_restart_only_links_committed_decision(tmp_path, monkeypatch, already_bound):
    link, request, *_ = setup_case(tmp_path)
    original_recover = link.recover
    if not already_bound:

        def crash(*args):
            raise RuntimeError("after ledger commit")

        monkeypatch.setattr(link, "recover", crash)
        with pytest.raises(RuntimeError):
            link.evaluate(request)
    else:
        link.evaluate(request)
    script = """
import json,sys
from pathlib import Path
from tests.governance.test_preselection_d1 import open_link
from commerce_agent.governance.preselection_d1 import PreselectionD1Request
from commerce_agent.governance.shadow import ShadowD1Observer
from commerce_agent.governance.canonical import canonical_bytes,sha256_digest
def forbidden(*a,**k): raise RuntimeError('no reevaluation on recovery')
ShadowD1Observer.capture=forbidden
ShadowD1Observer.observe=forbidden
data=json.loads(sys.stdin.read())
link=open_link(Path(data['root']))
result=link.recover(PreselectionD1Request.model_validate_json(json.dumps(data['request'])))
print(sha256_digest(canonical_bytes(result)))
"""
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + str(root)
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps({"root": str(tmp_path), "request": request.model_dump(mode="json")}),
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    expected = original_recover(request)
    assert result.stdout.strip() == sha256_digest(canonical_bytes(expected))
    assert counters(link.path) == dict.fromkeys(counters(link.path), 0)


def test_old_history_and_purchase_transition_cannot_be_promoted(tmp_path):
    link, request, *_ = setup_case(tmp_path)
    SqliteAgentStore(link.path).create_purchase(request.run_id, "older", "buyer")
    with pytest.raises(ObservationBlocked):
        link.evaluate(request)
    historical = tmp_path / "historical.sqlite"
    with closing(sqlite3.connect(historical)) as db:
        db.execute("CREATE TABLE purchases(run_id TEXT)")
        db.commit()
    old = historical.read_bytes()
    foreign = open_link(tmp_path)
    foreign.path = historical
    with pytest.raises(ObservationBlocked):
        foreign.initialize()
    assert historical.read_bytes() == old


def test_bound_purchase_cannot_become_dispatchable_or_binding_replaced(tmp_path):
    link, request, *_ = setup_case(tmp_path)
    result = link.evaluate(request)
    with pytest.raises(sqlite3.IntegrityError):
        SqliteAgentStore(link.path).compare_and_set_purchase(
            request.run_id, 1, stage="selected", selected_variant_id=request.selected_variant_id
        )
    with closing(sqlite3.connect(link.path)) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE q_d1_bindings SET body=?", (b"{}",))
        db.rollback()
    assert link.recover(request) == result


def test_another_valid_seal_cannot_bind_same_run(tmp_path):
    link, request, *_ = setup_case(tmp_path)
    result = link.evaluate(request)
    second_root = tmp_path / "second"
    second, other_request, *_ = setup_case(second_root)
    assert other_request.seal_digest != request.seal_digest
    with pytest.raises(ObservationBlocked):
        second.evaluate(other_request.model_copy(update={"run_id": request.run_id}))
    with pytest.raises(ObservationBlocked):
        link.evaluate(request.model_copy(update={"seal_digest": other_request.seal_digest}))
    assert link.recover(request) == result


@pytest.mark.parametrize("different", ["actor", "run"])
def test_another_issued_backend_session_cannot_consume_seal(tmp_path, different):
    link, request, seal, registry, _ = setup_case(tmp_path)
    source = seal.episode.binding
    other_bearer = SecretStr("another-issued-test-session")
    registry.issue_test_session(
        CatalogSource(
            event_id=str(uuid4()),
            merchant_origin=ORIGIN,
            merchant_id=source.merchant_id,
            remote_run_id=str(uuid4()) if different == "run" else source.scope.remote_run_id,
            remote_actor_id="foreign" if different == "actor" else source.scope.remote_actor_id,
        ),
        other_bearer,
    )
    link._bearer = other_bearer
    with pytest.raises(ObservationBlocked):
        link.evaluate(request)


def test_new_valid_custody_seal_cannot_replace_observed_offer_after_d1(tmp_path):
    link, request, seal, *_ = setup_case(tmp_path)
    result = link.evaluate(request)
    value = seal.model_dump(mode="json")
    observation = value["observations"][0]
    observation["offer"]["revision"] += 1
    from commerce_agent.acp.offer_snapshot import MerchantOffer, offer_snapshot_sha256

    observation["offer_snapshot_sha256"] = offer_snapshot_sha256(
        MerchantOffer.model_validate(observation["offer"])
    )
    from commerce_agent.governance.canonical import parse_model
    from commerce_agent.governance.preselection import AuthoritySelection

    newer = parse_model(AuthoritySelection, canonical_bytes(value))
    with closing(sqlite3.connect(tmp_path / "source.sqlite")) as db:
        db.execute("DROP TRIGGER pa_observations_UPDATE")
        db.execute("DROP TRIGGER pa_selections_UPDATE")
        raw = canonical_bytes(newer.observations[0])
        db.execute(
            "UPDATE pa_observations SET payload=?,seal=?",
            (raw, link.source._seal("OfferObservation", raw)),
        )
        raw = canonical_bytes(newer)
        db.execute(
            "UPDATE pa_selections SET payload=?,seal=?",
            (raw, link.source._seal("AuthoritySelection", raw)),
        )
        db.commit()
    assert link.source.recover(request.episode_id, request.owner_id) == newer
    with pytest.raises(ObservationBlocked):
        link.recover(request)
    new_request = link.request(request.episode_id, request.owner_id)
    with pytest.raises(ObservationBlocked):
        link.evaluate(new_request)
    with closing(sqlite3.connect(link.path)) as db:
        assert (
            json.loads(db.execute("SELECT body FROM q_d1_bindings").fetchone()[0])["offer_revision"]
            == result.offer_revision
        )


def test_genuine_d1_history_without_q_attempt_is_not_promoted(tmp_path):
    link, request, seal, *_ = setup_case(tmp_path)
    SqliteAgentStore(link.path).create_purchase(request.run_id, "older", "buyer")
    observer = ShadowD1Observer(link.path, link.configuration, link.keys, link.clocks)
    from commerce_agent.domain import rank_candidates

    capture = observer.capture(seal.episode.feed, seal.episode.intent, rank_candidates)
    assert capture is not None
    observer.observe(
        capture,
        run_id=request.run_id,
        owner_id=request.owner_id,
        selection=seal.selection,
        selected_variant_id=request.selected_variant_id,
    )
    assert observer.diagnostics.status == "observed"
    with closing(sqlite3.connect(link.path)) as db:
        original = db.execute("SELECT body FROM governance_evidence").fetchone()[0]
    with pytest.raises(ObservationBlocked):
        link.evaluate(request)
    with pytest.raises(ObservationBlocked):
        link.recover(request)
    with closing(sqlite3.connect(link.path)) as db:
        assert db.execute("SELECT body FROM governance_evidence").fetchone()[0] == original
        assert db.execute("SELECT count(*) FROM q_d1_bindings").fetchone()[0] == 0


def test_link_custody_and_tamper_rejected_after_restart(tmp_path):
    link, request, *_ = setup_case(tmp_path)
    link.evaluate(request)
    wrong_key = open_link(tmp_path)
    wrong_key._custody = SecretBytes(b"different-custody-key-only-test-32")
    with pytest.raises(ObservationBlocked):
        wrong_key.recover(request)
    with closing(sqlite3.connect(link.path)) as db:
        db.execute("DROP TRIGGER q_d1_bindings_UPDATE")
        db.execute("UPDATE q_d1_bindings SET body=?", (b"{}",))
        db.commit()
    with pytest.raises(ObservationBlocked):
        open_link(tmp_path).recover(request)


def test_replace_sql_cannot_overwrite_links_registry_or_frozen_purchase(tmp_path):
    link, request, seal, registry, _ = setup_case(tmp_path)
    result = link.evaluate(request)
    for path, tables in (
        (link.path, ("q_d1_meta", "q_d1_attempts", "q_d1_bindings", "purchases")),
        (tmp_path / "registry.sqlite", ("q_registry_objects", "q_registry_grants")),
    ):
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
            for table in tables:
                columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
                row = db.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
                assert row is not None
                with pytest.raises(sqlite3.IntegrityError):
                    db.execute(
                        f"INSERT OR REPLACE INTO {table}({','.join(columns)}) "
                        f"VALUES({','.join('?' for _ in row)})",
                        row,
                    )
                db.rollback()
    assert link.recover(request) == result
    registry.revoke_test_credential(seal.episode.binding.scope.source_ref)
    with (
        closing(sqlite3.connect(tmp_path / "registry.sqlite")) as db,
        pytest.raises(sqlite3.IntegrityError),
    ):
        db.execute(
            "INSERT OR REPLACE INTO q_registry_revoked VALUES(?)",
            (seal.episode.binding.scope.source_ref.id,),
        )


def test_same_credential_cannot_be_rebound_to_another_scope(tmp_path):
    link, request, seal, registry, _ = setup_case(tmp_path)
    source = seal.episode.binding
    with pytest.raises(sqlite3.IntegrityError):
        registry.issue_test_session(
            CatalogSource(
                event_id=str(uuid4()),
                merchant_origin=ORIGIN,
                merchant_id=source.merchant_id,
                remote_run_id=str(uuid4()),
                remote_actor_id="foreign",
            ),
            BEARER,
        )
    result = link.evaluate(request)
    assert result.merchant.scope.remote_run_id == source.scope.remote_run_id
    assert result.merchant.scope.remote_actor_id == source.scope.remote_actor_id
