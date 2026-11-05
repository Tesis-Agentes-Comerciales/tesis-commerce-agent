"""Scripted controls of the second P0 real-model batch (docs/design/p0-llm-batch2.md §8).

Layer 4, opt-in (RUN_LIVE_STACK=1), never in ci.yml: the real Merchant, payment provider and
PostgreSQL of the live stack, and the real OpenAI SDK over the scripted transport of
tests/support/model_server.py. No model, no request to a provider, no cost. Every control must pass
on run_ref before the smoke spends anything.
"""

import os
import shutil

import pytest

from commerce_evaluation.p0_llm_batch2 import (
    list_unchanged,
    mount_injection,
    preflight,
    protocol,
)
from tests.integration.live_purchase import PurchaseLane

PLAN = protocol()
CT21 = PLAN["ct21"]


@pytest.fixture(autouse=True)
def no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OPENAI_* variable of the developer's shell reaches the SDK (a base URL included)."""
    for name in list(os.environ):
        if name.startswith("OPENAI_"):
            monkeypatch.delenv(name)


def test_c_preflight_exported_feed_is_the_registered_catalog(b0: PurchaseLane, tmp_path):
    """C-PREFLIGHT on a real episode: export, the unmounted copy, the mount and the Merchant."""
    episode = b0.episode("p0-llm2-preflight")
    catalog = b0.rpc("p0_llm_batch2_catalog", run_id=episode.run_id)
    assert catalog["checkouts"] == [] and catalog["orders"] == []
    plain = tmp_path / "plain-feed"
    shutil.copytree(episode.feed, plain)

    unmounted = preflight(plain, catalog, mounted=False)
    mount = mount_injection(episode.feed)
    mounted = preflight(episode.feed, catalog, mounted=True)

    assert unmounted["shortlist"] == mounted["shortlist"] == CT21["shortlist"]
    assert unmounted["R"] == mounted["R"] == sorted(CT21["R"])
    assert mount["before_sha256"] == unmounted["products_sha256"]
    assert mount["after_sha256"] == mounted["products_sha256"]
    # The baseline of the list post-check is this unmounted copy.
    assert list_unchanged(plain, CT21["reference_intent"], CT21["shortlist"])
    # The Merchant's own offer is untouched: the mount is feed-side only.
    after = b0.rpc("p0_llm_batch2_catalog", run_id=episode.run_id)
    assert after == catalog
