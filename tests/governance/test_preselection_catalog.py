"""Multi-candidate catalog through P preselection and Q shadow D1. Controlled HTTP only.

The other P/Q tests use a one-variant feed, where the first observation is also the
selected one. Here the ranked winner is neither first in the feed nor alone, so every
check that must follow the sealed selection, not the feed order, is exercised.
"""

import copy
import json
import sqlite3
from contextlib import closing

import httpx
import pytest

from commerce_agent.acp.authority_observation import ObservationBlocked
from commerce_agent.acp.feed import ProductFeed
from commerce_agent.acp.offer_snapshot import MerchantOffer, offer_snapshot_sha256
from commerce_agent.domain import rank_candidates
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from tests.governance.test_preselection import (
    ControlledMerchant,
    capture,
    feed_from,
    offer_value,
    setup_case,
)
from tests.governance.test_preselection_d1 import setup_case as setup_link

OBSERVE = "/lab/offers/get"


def variant(variant_id, product_id, title, unit, *, quantity=5):
    value = copy.deepcopy(offer_value())
    value.update(
        id=variant_id,
        product_id=product_id,
        product_title=title,
        name=f"{title} {variant_id}",
        available_quantity=quantity,
        availability="in_stock" if quantity else "out_of_stock",
    )
    shipping = value["pricing"]["shipping_total_minor"]
    value["pricing"].update(items_total_minor=unit, total_minor=unit + shipping)
    return value


def catalog():
    # Two products, four variants. The cheapest available one is second in the feed;
    # the cheapest overall is out of stock and must be neither observed nor selected.
    return [
        variant("variant-1", "product-1", "Example", 100),
        variant("variant-2", "product-1", "Example", 80),
        variant("variant-3", "product-2", "Other", 150),
        variant("variant-4", "product-2", "Other", 60, quantity=0),
    ]


class CatalogMerchant(ControlledMerchant):
    """Several authoritative offers; each observation answers the requested variant."""

    def __init__(self, offers):
        super().__init__()
        self.offers = {offer["id"]: offer for offer in offers}
        self.observed = []
        feeds = [
            feed_from(MerchantOffer.model_validate(offer)).model_dump(mode="json")
            for offer in offers
        ]
        products = {}
        for feed in feeds:
            product = feed["products"][0]
            products.setdefault(product["id"], {**product, "variants": []})["variants"].extend(
                product["variants"]
            )
        # JSON mode: the frozen feed holds tuples, as in the served wire format.
        self.feed = ProductFeed.model_validate_json(
            json.dumps({"metadata": feeds[0]["metadata"], "products": list(products.values())})
        )

    def handle(self, request):
        if request.url.path == OBSERVE:
            variant_id = json.loads(request.content)["offer_id"]
            self.observed.append(variant_id)
            self.offer = self.offers[variant_id]
        return super().handle(request)


def test_every_available_candidate_is_observed_and_the_ranked_winner_sealed(tmp_path):
    merchant = CatalogMerchant(catalog())
    case = setup_case(tmp_path, merchant)
    try:
        result = capture(case)
        feed = result.episode.feed
        assert [c.variant_id for c in feed.candidates] == [
            "variant-1",
            "variant-2",
            "variant-3",
            "variant-4",
        ]
        assert merchant.observed == ["variant-1", "variant-2", "variant-3"]
        assert [o.offer.id for o in result.observations] == merchant.observed
        assert result.selection == rank_candidates(feed, result.episode.intent)
        assert [r.candidate.variant_id for r in result.selection.ranked] == [
            "variant-2",
            "variant-1",
            "variant-3",
        ]
        selected = [o for o in result.observations if o.candidate == result.selection.selected]
        assert [result.observations.index(o) for o in selected] == [1]
        assert selected[0].offer_snapshot_sha256 == offer_snapshot_sha256(
            MerchantOffer.model_validate(merchant.offers["variant-2"])
        )
        assert case[2].recover(case[4], "buyer") == result
        assert merchant.calls.count(("POST", OBSERVE)) == 3
    finally:
        case[1].close()


@pytest.mark.parametrize("variant_id", ["variant-1", "variant-2", "variant-3"])
def test_any_candidate_with_conflicting_terms_blocks_the_whole_selection(tmp_path, variant_id):
    merchant = CatalogMerchant(catalog())
    # The authoritative price no longer matches its feed row, whether it would win or not.
    pricing = merchant.offers[variant_id]["pricing"]
    pricing.update(
        items_total_minor=pricing["items_total_minor"] + 1, total_minor=pricing["total_minor"] + 1
    )
    case = setup_case(tmp_path, merchant)
    with pytest.raises(ObservationBlocked, match=r"^PRESELECTION_BLOCKED$"):
        capture(case)
    with pytest.raises(ObservationBlocked):
        case[2].recover(case[4], "buyer")
    # Observation stops at the conflicting candidate and nothing is sealed.
    assert merchant.observed[-1] == variant_id
    with closing(sqlite3.connect(tmp_path / "aux.sqlite")) as db:
        assert db.execute("SELECT state FROM pa_episodes").fetchone()[0] == "blocked"
        assert db.execute("SELECT count(*) FROM pa_selections").fetchone()[0] == 0
    case[1].close()


def test_q_binds_the_ranked_winner_not_the_first_observation(tmp_path, monkeypatch):
    link, request, seal, *_ = setup_link(tmp_path, merchant=CatalogMerchant(catalog()))

    def forbidden(*args, **kwargs):
        pytest.fail("No HTTP allowed during Q")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    assert request.selected_variant_id == "variant-2"
    result = link.evaluate(request)
    selected = seal.observations[1]
    assert selected.offer.id == "variant-2"
    assert (
        result.observation_id,
        result.offer_id,
        result.offer_revision,
        result.offer_snapshot_sha256,
    ) == (
        selected.observation_id,
        "variant-2",
        selected.offer.revision,
        selected.offer_snapshot_sha256,
    )
    assert result.observation_digest == sha256_digest(canonical_bytes(selected))
    assert result.outcome == "allow" and result.operation_id is None
    assert not result.dispatch_capability
    with closing(sqlite3.connect(link.path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        record = SqliteGovernanceEvidenceStore(db).get(result.evidence_id)
        assert isinstance(record, DecisionRecord)
        data = record.policy_input()
        assert data.selected_variant_id == "variant-2"
        assert tuple(data.eligible_variant_ids) == ("variant-1", "variant-2", "variant-3")
    assert link.recover(request) == result


@pytest.mark.parametrize("other", ["variant-1", "variant-3", "variant-4"])
def test_q_refuses_another_feed_candidate_as_the_selection(tmp_path, other):
    link, request, *_ = setup_link(tmp_path, merchant=CatalogMerchant(catalog()))
    with pytest.raises(ObservationBlocked, match="SEALED_FACTS_CONFLICT"):
        link.evaluate(request.model_copy(update={"selected_variant_id": other}))
    with closing(sqlite3.connect(link.path)) as db:
        assert db.execute("SELECT count(*) FROM q_d1_attempts").fetchone()[0] == 0
    assert link.evaluate(request).offer_id == "variant-2"
