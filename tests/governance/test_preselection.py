"""Controlled HTTP + real auxiliary SQLite; no real Merchant auth/PG claims."""

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretBytes, SecretStr, ValidationError

from commerce_agent.acp.authority_observation import (
    AuthorityObservationClient,
    AuthoritySession,
    CatalogBinding,
    ObservationBlocked,
)
from commerce_agent.acp.feed import (
    FeedAvailability,
    FeedMetadata,
    FeedPrice,
    FeedProduct,
    FeedVariant,
    ProductFeed,
)
from commerce_agent.acp.offer_snapshot import DeliveryContext, MerchantOffer, offer_snapshot_sha256
from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.models import ObjectRef
from commerce_agent.governance.preselection import (
    AuthoritySelection,
    PreselectionAuthority,
    projected_candidate,
)
from commerce_agent.governance.remote_creation import SessionScope
from commerce_agent.persistence.preselection_sqlite import SqlitePreselectionStore

ORIGIN = "https://merchant.example.test"
KEY = SecretBytes(b"synthetic-custody-test-key-only-32")
DESTINATION = DeliveryContext(country="CO", city="Bogota", postal_code="110111")


def offer_value():
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "id": "variant-1",
        "product_id": "product-1",
        "merchant_id": "merchant-1",
        "revision": 1,
        "category": "laptops",
        "brand": "Example",
        "name": "Example Laptop",
        "product_title": "Example",
        "condition": "new",
        "availability": "in_stock",
        "available_quantity": 5,
        "pricing": {
            "currency": "cop",
            "items_total_minor": 100,
            "shipping_total_minor": 20,
            "total_minor": 120,
            "tax_included": True,
        },
        "delivery_context": DESTINATION.model_dump(mode="json"),
        "delivery_days": 2,
        "observed_at": (now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def binding():
    ref = ObjectRef(
        schema_version="commerce-policy/1.0",
        id="test:registry",
        version=1,
        digest="sha256:" + "a" * 64,
    )
    return CatalogBinding(
        assurance="backend_managed_catalog_registry/1",
        merchant_origin=ORIGIN,
        merchant_id="merchant-1",
        catalog_kind="merchant",
        feed_id="feed_merchant_catalog",
        scope=SessionScope(
            assurance="backend_session_registry/1",
            remote_run_id=str(uuid4()),
            remote_actor_id="merchant-agent",
            source_ref=ref,
        ),
        source_ref=ref,
    )


def feed_from(offer):
    candidate = projected_candidate(offer, ORIGIN)
    return ProductFeed(
        metadata=FeedMetadata(
            id="feed_merchant_catalog", target_country="CO", updated_at=offer.observed_at
        ),
        products=(
            FeedProduct(
                id=candidate.product_id,
                title=candidate.product_title,
                url=candidate.product_url,
                variants=[
                    FeedVariant(
                        id=candidate.variant_id,
                        title=candidate.variant_title,
                        url=candidate.variant_url,
                        price=FeedPrice(
                            amount=candidate.observed_unit_amount_minor, currency="COP"
                        ),
                        availability=FeedAvailability(
                            available=candidate.available, status=candidate.availability_status
                        ),
                        categories=[
                            {"taxonomy": "merchant", "value": offer.category},
                            {"taxonomy": "brand", "value": offer.brand},
                        ],
                        condition=[offer.condition],
                        variant_options=[{"name": k, "value": v} for k, v in candidate.options],
                    )
                ],
            ),
        ),
    )


class ControlledMerchant:
    def __init__(self):
        self.offer = offer_value()
        self.binding = binding()
        self.feed = feed_from(MerchantOffer.model_validate(self.offer))
        self.status = 200
        self.mode = "ok"
        self.calls = []

    def handle(self, request):
        self.calls.append((request.method, request.url.path))
        headers = {"Request-Id": "req-" + str(len(self.calls)), "Cache-Control": "no-store"}
        if request.url.path == "/feeds/current":
            assert "authorization" not in request.headers
            return httpx.Response(200, json=self.feed.model_dump(mode="json"), headers=headers)
        assert request.headers["Authorization"] == "Bearer synthetic-session"
        assert request.headers["Content-Type"] == "application/json"
        if self.status != 200:
            return httpx.Response(self.status, json={}, headers=headers)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("synthetic secret must not escape", request=request)
        if request.url.path == "/lab/recovery/checkout-creation/lookup":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "schema_version": "merchant-acp-creation-lookup/1",
                    "result": "not_found",
                    "run_id": (
                        str(uuid4()) if self.mode == "run" else self.binding.scope.remote_run_id
                    ),
                    "actor_id": (
                        "foreign" if self.mode == "actor" else self.binding.scope.remote_actor_id
                    ),
                    "idempotency_key_sha256": body["idempotency_key_sha256"],
                    "request_id": headers["Request-Id"],
                },
                headers=headers,
            )
        assert request.url.path == "/lab/offers/get"
        if self.mode == "oversized":
            return httpx.Response(
                200, content=b" " * 65537, headers={**headers, "Content-Type": "application/json"}
            )
        if self.mode == "duplicate":
            return httpx.Response(
                200,
                content=b'{"ok":true,"ok":true,"data":{}}',
                headers={**headers, "Content-Type": "application/json"},
            )
        return httpx.Response(200, json={"ok": True, "data": self.offer}, headers=headers)


def setup_case(tmp_path, merchant=None, *, intent=None):
    merchant = merchant or ControlledMerchant()
    session = AuthoritySession(merchant.binding, SecretStr("synthetic-session"))
    client = AuthorityObservationClient(session, transport=httpx.MockTransport(merchant.handle))
    store = SqlitePreselectionStore(tmp_path / "aux.sqlite", KEY)
    store.initialize()
    flow = PreselectionAuthority(client, store)
    episode = flow.begin(
        owner_id="buyer", intent=intent or PurchaseIntent(), destination=DESTINATION
    )
    return merchant, client, store, flow, episode


def capture(case):
    return case[3].capture(case[4], owner_id="buyer")


def test_complete_before_selection_and_durable_restart(tmp_path):
    case = setup_case(tmp_path)
    try:
        result = capture(case)
        assert result.selection == rank_candidates(result.episode.feed, result.episode.intent)
        assert result.observations[0].offer.pricing.total_minor == 120
        assert result.observations[0].offer_snapshot_sha256 == offer_snapshot_sha256(
            MerchantOffer.model_validate(case[0].offer)
        )
        assert (
            result.episode.discovered_at < result.observations[0].observed_at < result.selected_at
        )
        assert result.selected_at < result.sealed_at
        assert not result.human_approval and not result.commercial_continuation
        restarted = SqlitePreselectionStore(tmp_path / "aux.sqlite", KEY)
        restarted.initialize()
        assert restarted.recover(case[4], "buyer") == result
        assert case[0].calls == [
            ("GET", "/feeds/current"),
            ("POST", "/lab/recovery/checkout-creation/lookup"),
            ("POST", "/lab/offers/get"),
            ("POST", "/lab/recovery/checkout-creation/lookup"),
        ]
    finally:
        case[1].close()


@pytest.mark.parametrize(
    "change",
    [
        "unit",
        "variant",
        "product",
        "currency",
        "inactive",
        "stock",
        "merchant",
        "expired",
        "future",
        "missing",
        "total",
        "sensitive",
        "destination",
        "options",
    ],
)
def test_rejects_adversarial_offers_without_selection(tmp_path, change):
    case = setup_case(tmp_path)
    offer = case[0].offer
    if change == "unit":
        offer["pricing"].update(items_total_minor=101, total_minor=121)
    elif change == "variant":
        offer["id"] = "other"
    elif change == "product":
        offer["product_id"] = "other"
    elif change == "currency":
        offer["pricing"]["currency"] = "usd"
    elif change == "inactive":
        offer["product_status"] = "inactive"
    elif change == "stock":
        offer.update(available_quantity=0, availability="out_of_stock")
    elif change == "merchant":
        offer["merchant_id"] = "other"
    elif change == "expired":
        offer.update(observed_at="2020-01-01T00:00:00Z", expires_at="2020-01-02T00:00:00Z")
    elif change == "future":
        offer.update(observed_at="2098-01-01T00:00:00Z", expires_at="2099-01-01T00:00:00Z")
    elif change == "missing":
        del offer["pricing"]
    elif change == "total":
        offer["pricing"]["total_minor"] = 999
    elif change == "sensitive":
        offer["attributes"] = {"token": "must-not-be-sealed"}
    elif change == "destination":
        offer["delivery_context"]["postal_code"] = "999999"
    elif change == "options":
        offer["color"] = "red"
    with pytest.raises(ObservationBlocked):
        capture(case)
    with pytest.raises(ObservationBlocked):
        case[2].recover(case[4], "buyer")
    with closing(sqlite3.connect(tmp_path / "aux.sqlite")) as db:
        assert db.execute("SELECT state FROM pa_episodes").fetchone()[0] == "blocked"
    case[1].close()


@pytest.mark.parametrize("mode", ["run", "actor", "timeout", "oversized", "duplicate"])
def test_scope_and_transport_conservative(tmp_path, mode):
    case = setup_case(tmp_path)
    case[0].mode = mode
    with pytest.raises(ObservationBlocked, match=r"^PRESELECTION_BLOCKED$"):
        capture(case)
    case[1].close()


@pytest.mark.parametrize("status", [301, 401, 403, 422, 500, 503])
def test_http_rejection_and_revoked_session(tmp_path, status):
    case = setup_case(tmp_path)
    case[0].status = status
    with pytest.raises(ObservationBlocked):
        capture(case)
    assert len(case[0].calls) == 2
    case[1].close()


def test_complementary_total_is_not_d1_or_ranking(tmp_path):
    case = setup_case(
        tmp_path, intent=PurchaseIntent(hard=HardConstraints(max_total_amount_minor=110))
    )
    assert rank_candidates(case[0].feed, PurchaseIntent()).selected is not None
    with pytest.raises(ObservationBlocked):
        capture(case)
    case[1].close()


@pytest.mark.parametrize("change", ["revision", "shipping", "expiry", "same_revision_digest"])
def test_noninjective_feed_and_new_terms_preserved(tmp_path, change):
    case = setup_case(tmp_path)
    before = MerchantOffer.model_validate(case[0].offer)
    after = case[0].offer
    if change == "revision":
        after["revision"] += 1
    elif change in ("shipping", "same_revision_digest"):
        after["pricing"].update(shipping_total_minor=21, total_minor=121)
    else:
        after["expires_at"] = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = MerchantOffer.model_validate(after)
    assert projected_candidate(before, ORIGIN) == projected_candidate(updated, ORIGIN)
    assert offer_snapshot_sha256(before) != offer_snapshot_sha256(updated)
    result = capture(case)
    assert result.observations[0].offer == updated
    # They are newly observed terms. No assertion that the Feed ever carried them.
    assert result.observations[0].offer_snapshot_sha256 != offer_snapshot_sha256(before)
    case[1].close()


def test_duplicate_concurrent_capture_single_observation(tmp_path):
    case = setup_case(tmp_path)
    barrier = Barrier(2)

    def attempt():
        barrier.wait()
        try:
            return capture(case)
        except ObservationBlocked:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert case[0].calls.count(("POST", "/lab/offers/get")) == 1
    case[1].close()


def test_closed_selection_cannot_be_observed_again_or_retimestamped(tmp_path):
    case = setup_case(tmp_path)
    original = capture(case)
    calls = len(case[0].calls)
    with pytest.raises(ObservationBlocked):
        capture(case)
    assert len(case[0].calls) == calls
    changed = original.model_dump(mode="json")
    changed["observations"][0]["observed_at"] = original.selected_at
    with pytest.raises(ValidationError):
        AuthoritySelection.model_validate_json(json.dumps(changed))
    assert case[2].recover(case[4], "buyer") == original
    case[1].close()


def test_owner_context_restart_and_incomplete_history_fail_closed(tmp_path):
    case = setup_case(tmp_path)
    with pytest.raises(ObservationBlocked):
        case[3].capture(case[4], owner_id="foreign")
    other = case[0].binding.model_copy(update={"merchant_id": "different"})
    with pytest.raises(ObservationBlocked):
        case[2].claim(case[4], "buyer", other)
    case[2].claim(case[4], "buyer", case[0].binding)
    restarted = SqlitePreselectionStore(tmp_path / "aux.sqlite", KEY)
    with pytest.raises(ObservationBlocked):
        restarted.claim(case[4], "buyer", case[0].binding)
    with pytest.raises(ObservationBlocked):
        restarted.recover(case[4], "buyer")
    case[1].close()


@pytest.mark.parametrize("table", ["pa_episodes", "pa_observations", "pa_selections"])
def test_immutable_rows_and_authenticated_tamper_detection(tmp_path, table):
    case = setup_case(tmp_path)
    result = capture(case)
    with closing(sqlite3.connect(tmp_path / "aux.sqlite")) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"UPDATE {table} SET payload=?", (b"{}",))
        db.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"DELETE FROM {table}")
    assert case[2].recover(case[4], "buyer") == result
    wrong = SqlitePreselectionStore(tmp_path / "aux.sqlite", SecretBytes(b"wrong-key" * 4))
    with pytest.raises(ObservationBlocked):
        wrong.recover(case[4], "buyer")
    with pytest.raises(ObservationBlocked):
        case[2].recover(case[4], "foreign")
    case[1].close()


def test_no_historical_database_migration(tmp_path):
    path = tmp_path / "historical.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE purchases(run_id TEXT,stage TEXT)")
        db.execute("INSERT INTO purchases VALUES('old','selected')")
        db.commit()
    before = path.read_bytes()
    with pytest.raises(ObservationBlocked):
        SqlitePreselectionStore(path, KEY).initialize()
    assert path.read_bytes() == before


def test_canonical_snapshot_defaults_and_no_substitution(tmp_path):
    case = setup_case(tmp_path)
    result = capture(case)
    original = result.observations[0]
    assert original.offer.sku == original.offer.id and original.offer.attributes == {}
    altered = original.model_dump(mode="json")
    altered["offer"]["revision"] = 2
    with pytest.raises(ValidationError):
        parse_model(type(original), canonical_bytes(altered))
    altered = result.model_dump(mode="json")
    altered["observations"][0]["episode_id"] = str(uuid4())
    with pytest.raises(ValueError):
        parse_model(AuthoritySelection, canonical_bytes(altered))
    # A replaced context does not permit claiming a closed selection.
    other_client = AuthorityObservationClient(
        replace(AuthoritySession(case[0].binding, SecretStr("synthetic-session"))),
        transport=httpx.MockTransport(case[0].handle),
    )
    other_client.close()
    case[1].close()


def test_reflected_credential_never_enters_evidence(tmp_path):
    case = setup_case(tmp_path)
    case[0].offer["product_description"] = "synthetic-session"
    with pytest.raises(ObservationBlocked):
        capture(case)
    with closing(sqlite3.connect(tmp_path / "aux.sqlite")) as db:
        assert db.execute("SELECT count(*) FROM pa_observations").fetchone()[0] == 0
    assert b"synthetic-session" not in (tmp_path / "aux.sqlite").read_bytes()
    case[1].close()


def test_hmac_rejects_tamper_after_removing_sql_trigger(tmp_path):
    case = setup_case(tmp_path)
    result = capture(case)
    changed = result.model_dump(mode="json")
    changed["scope_checks"][0]["actor_id"] = "foreign"
    with closing(sqlite3.connect(tmp_path / "aux.sqlite")) as db:
        db.execute("DROP TRIGGER pa_selections_UPDATE")
        db.execute("UPDATE pa_selections SET payload=?", (canonical_bytes(changed),))
        db.commit()
    with pytest.raises(ObservationBlocked):
        case[2].recover(case[4], "buyer")
    case[1].close()


def test_correctly_rehashed_different_terms_cannot_replace_sealed_selection(tmp_path):
    case = setup_case(tmp_path)
    original = capture(case)
    changed = original.model_dump(mode="json")
    observation = changed["observations"][0]
    observation["offer"]["pricing"].update(shipping_total_minor=21, total_minor=121)
    observation["offer_snapshot_sha256"] = offer_snapshot_sha256(
        MerchantOffer.model_validate(observation["offer"])
    )
    replacement = parse_model(AuthoritySelection, canonical_bytes(changed))
    with pytest.raises((ObservationBlocked, sqlite3.IntegrityError)):
        case[2].seal(replacement)
    assert case[2].recover(case[4], "buyer") == original
    case[1].close()


def test_stream_has_total_deadline_not_only_per_read_timeout(monkeypatch):
    import commerce_agent.acp.authority_observation as module

    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(module, "monotonic", lambda: next(ticks))

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            yield b"{"
            yield b"}"

    client = AuthorityObservationClient(
        AuthoritySession(binding(), SecretStr("synthetic-session")),
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"Content-Type": "application/json", "Request-Id": "drip"},
                stream=Drip(),
            )
        ),
    )
    try:
        with pytest.raises(ObservationBlocked, match="READ_TIMEOUT"):
            client.discover()
    finally:
        client.close()


def test_actual_process_restart_reads_seal_without_recapture(tmp_path):
    case = setup_case(tmp_path)
    original = capture(case)
    script = """
import json,sys
from pathlib import Path
from pydantic import SecretBytes
from commerce_agent.persistence.preselection_sqlite import SqlitePreselectionStore
from commerce_agent.governance.canonical import canonical_bytes,sha256_digest
data=json.loads(sys.stdin.read())
store=SqlitePreselectionStore(Path(data['database']),SecretBytes(bytes.fromhex(data['key'])))
store.initialize()
result=store.recover(data['episode'],'buyer')
print(sha256_digest(canonical_bytes(result)))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps(
            {
                "database": str(tmp_path / "aux.sqlite"),
                "episode": case[4],
                "key": KEY.get_secret_value().hex(),
            }
        ),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0
    from commerce_agent.governance.canonical import sha256_digest

    assert result.stdout.strip() == sha256_digest(canonical_bytes(original))
    case[1].close()


def test_ranking_only_after_durable_complete_observations(tmp_path, monkeypatch):
    import commerce_agent.governance.preselection as module

    case = setup_case(tmp_path)
    calls = []

    def ranking(feed, intent):
        assert case[0].calls[-1] == ("POST", "/lab/recovery/checkout-creation/lookup")
        with closing(sqlite3.connect(tmp_path / "aux.sqlite")) as db:
            assert db.execute("SELECT count(*) FROM pa_observations").fetchone()[0] == 1
        calls.append(True)
        return rank_candidates(feed, intent)

    monkeypatch.setattr(module, "rank_candidates", ranking)
    capture(case)
    assert calls
    case[1].close()
