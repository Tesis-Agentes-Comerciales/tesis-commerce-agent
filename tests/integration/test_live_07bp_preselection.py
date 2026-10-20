"""07B-P live: authoritative preselection against the Merchant's published catalog over HTTP."""

import secrets

import pytest
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.authority_observation import (
    AuthorityObservationClient,
    AuthoritySession,
    CatalogBinding,
    ObservationBlocked,
)
from commerce_agent.acp.offer_snapshot import DeliveryContext, MerchantOffer, offer_snapshot_sha256
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.models import ObjectRef
from commerce_agent.governance.preselection import PreselectionAuthority, projected_candidate
from commerce_agent.governance.remote_creation import LOOKUP_PATH, SessionScope
from commerce_agent.persistence.preselection_sqlite import SqlitePreselectionStore
from tests.integration.live_stack import live_stack_requirements

pytestmark = live_stack_requirements()
BOGOTA = DeliveryContext(country="CO", city="Bogota", postal_code="110111")
# Observation only: the feed, the scope challenge and offer reads.
READS = {("GET", "/feeds/current"), ("POST", LOOKUP_PATH), ("POST", "/lab/offers/get")}


def binding(stack) -> CatalogBinding:
    catalog = stack.catalog()
    receipt = {
        "origin": stack.origin,
        "run_id": catalog["run_id"],
        "actor_id": catalog["actor_id"],
        "catalog_kind": "merchant",
        "source": "managed_catalog_context + issue_lab_session; Merchant control worker",
    }
    ref = ObjectRef(
        schema_version="commerce-policy/1.0",
        id="p07:provisioning",
        version=1,
        digest=sha256_digest(canonical_bytes(receipt)),
    )
    return CatalogBinding(
        assurance="backend_managed_catalog_registry/1",
        merchant_origin=stack.origin,
        merchant_id=catalog["merchant_id"],
        catalog_kind="merchant",
        feed_id="feed_merchant_catalog",
        source_ref=ref,
        scope=SessionScope(
            assurance="backend_session_registry/1",
            source_ref=ref,
            remote_run_id=catalog["run_id"],
            remote_actor_id=catalog["actor_id"],
        ),
    )


def client(stack, credential) -> AuthorityObservationClient:
    return AuthorityObservationClient(
        AuthoritySession(binding(stack), SecretStr(credential["bearer"]))
    )


def test_07bp_real_merchant_catalog_session_and_snapshot(stack, proxy):
    catalog = stack.catalog()
    before = stack.rpc("effects")
    observer = client(stack, catalog["credential"])
    store = SqlitePreselectionStore(
        stack.root / ("07bp-" + secrets.token_hex(6) + ".sqlite"),
        SecretBytes(secrets.token_bytes(32)),
    )
    store.initialize()
    try:
        flow = PreselectionAuthority(observer, store)
        episode = flow.begin(
            owner_id="synthetic-buyer", intent=PurchaseIntent(), destination=BOGOTA
        )
        result = flow.capture(episode, owner_id="synthetic-buyer")
        assert len(result.observations) == len(result.episode.feed.available_candidates)
        assert len(result.observations) > 1
        assert not result.commercial_continuation
        assert stack.rpc("effects") == before
        assert set(proxy.requests()) <= READS
        stack.record("07bp:selection", observations=len(result.observations))
    finally:
        observer.close()


def test_07bp_real_same_id_different_catalog_is_not_authority(stack, proxy):
    catalog = stack.catalog()
    before = stack.rpc("effects")
    observer = client(stack, catalog["other_credential"])
    try:
        with pytest.raises(ObservationBlocked, match="SCOPE_CONFLICT"):
            observer.verify_scope()
        # Authenticated, valid Offer with the same ID really exists in that other run.
        offer, _ = observer.observe(catalog["offer"]["id"], BOGOTA)
        assert offer.id == catalog["offer"]["id"]
        assert offer.expires_at != catalog["offer"]["expires_at"]
        assert stack.rpc("effects") == before
    finally:
        observer.close()


def test_07bp_real_merchant_digest_equivalence_and_noninjective_projection(stack, proxy):
    catalog = stack.catalog()
    pair = stack.rpc("projection", offer=catalog["offer"])
    offer = MerchantOffer.model_validate(catalog["offer"])
    assert offer_snapshot_sha256(offer) == pair["first_digest"]
    assert pair["same_feed"] and pair["first_digest"] != pair["second_digest"]
    changed = offer.model_dump(mode="json")
    changed["revision"] += 1
    changed["pricing"]["shipping_total_minor"] += 1
    changed["pricing"]["total_minor"] += 1
    newer = MerchantOffer.model_validate(changed)
    assert projected_candidate(offer, stack.origin) == projected_candidate(newer, stack.origin)
    assert offer_snapshot_sha256(newer) == pair["second_digest"]


def test_07bp_real_revoked_session_conservative(stack, proxy):
    catalog = stack.catalog()
    # Its own session: the catalog credential stays valid for the other live modules.
    credential = stack.rpc("session", run_id=catalog["run_id"], actor_id=catalog["actor_id"])
    stack.rpc("session_state", state="revoked", session_sha256=credential["session_sha256"])
    before = stack.rpc("effects")
    observer = client(stack, credential)
    try:
        with pytest.raises(ObservationBlocked, match="SESSION_REJECTED"):
            observer.verify_scope()
        assert stack.rpc("effects") == before
    finally:
        observer.close()
