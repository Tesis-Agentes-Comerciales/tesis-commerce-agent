"""Opt-in runtime D1 enforce through create_app/build_runtime; mock transports only."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_agent.api import create_app
from commerce_agent.application import PurchaseServiceError
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance import bootstrap
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.provenance import build_d1_policy
from commerce_agent.governance.shadow import UnavailableShadowObserver, build_shadow_observer
from commerce_agent.llm import CandidateDecision, CommerceDecision
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.runtime import build_runtime
from tests.governance.test_shadow_integration import SpyGateway, configuration, start
from tests.test_api import StubConversationRouter, checkout_details

MERCHANT = Settings.model_fields["merchant_base_url"].default
UNLISTED = "https://otro-comercio.example"
DISCOVERY = {
    "protocol": {"name": "acp", "version": "2026-04-17", "supported_versions": ["2026-04-17"]},
    "api_base_url": MERCHANT,
    "transports": ["rest"],
    "capabilities": {
        "services": ["checkout"],
        "supported_currencies": ["cop"],
        "supported_locales": ["es-CO"],
    },
}


class Merchant:
    """Read-only discovery; any commercial request fails the test."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/.well-known/acp.json":
            return httpx.Response(200, json=DISCOVERY)
        raise AssertionError("D1 enforce must not reach a commercial endpoint")


def runtime(settings: Settings, merchant: Merchant):
    return build_runtime(
        settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(merchant),
    )


def decisions(path: Path) -> list[DecisionRecord]:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ledger = SqliteGovernanceEvidenceStore(connection)
        return [
            item
            for (run_id,) in connection.execute("SELECT run_id FROM purchases").fetchall()
            for item in ledger.for_run(run_id)
            if isinstance(item, DecisionRecord)
        ]


def test_unlisted_merchant_is_denied_through_the_app_before_checkout(tmp_path: Path) -> None:
    settings = configuration(
        tmp_path / "agent.db", mode="enforce", agent_d1_allowed_merchant_origins=UNLISTED
    )
    merchant = Merchant()
    router = StubConversationRouter(
        CommerceDecision(action="purchase", assistant_message="Preparo.", intent=PurchaseIntent()),
        CandidateDecision(
            action="purchase", assistant_message="Elegí Sonora.", selected_variant_id="SON-01"
        ),
    )
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(merchant),
        conversation_router=router,
    )
    with TestClient(app) as client:
        response = client.post(
            "/conversation/turn",
            json={
                "turn_id": "turn-d1-deny",
                "conversation_id": "conversation-d1",
                "messages": [{"role": "user", "content": "Quiero los audífonos disponibles"}],
                "details": checkout_details(),
            },
        )
    assert response.status_code == 409 and response.json()["code"] == "d1_denied"
    assert merchant.requests == [("GET", "/.well-known/acp.json")]
    (record,) = decisions(settings.agent_database_path)
    assert record.decision.governance_mode == "enforce" and record.decision.outcome == "deny"
    assert record.decision.reason_codes == ("ACTION_OR_MERCHANT_NOT_ALLOWED",)
    assert record.application.disposition == "blocked"
    with closing(sqlite3.connect(settings.agent_database_path)) as connection:
        assert connection.execute("SELECT stage,checkout_id FROM purchases").fetchall() == [
            ("received", None)
        ]


@pytest.mark.parametrize("origins", [None, f"{UNLISTED}, {MERCHANT}/"])
def test_allowed_merchant_reserves_but_runtime_has_no_dispatch(
    tmp_path: Path, origins: str | None
) -> None:
    settings = configuration(
        tmp_path / "agent.db", mode="enforce", agent_d1_allowed_merchant_origins=origins
    )
    merchant = Merchant()
    built = runtime(settings, merchant)
    try:
        assert isinstance(built.d1_gate, SqliteD1EnforceGate) and built.d1_shadow is None
        with pytest.raises(PurchaseServiceError) as blocked:
            start(built.purchases)
        assert blocked.value.code == "d1_dispatch_blocked"
        (record,) = decisions(settings.agent_database_path)
        assert record.decision.governance_mode == "enforce" and record.decision.outcome == "allow"
        status = built.d1_gate.recovery_status("run-real")
        assert status is not None and status[0] == "reserved"
        assert merchant.requests == []
        # Same gate for every later B0 step: no approval or completion bypasses it.
        with pytest.raises(PurchaseServiceError, match="independent authorization"):
            built.purchases.approval_terms("run-real")
    finally:
        built.close()


@pytest.mark.parametrize(
    "change",
    [
        {"agent_d1_hmac_key_hex": None},
        {"agent_d1_key_id": None},
        {"agent_d1_policy_revision": "0"},
        {"agent_d1_policy_digest": "sha256:" + "0" * 64},
        {"agent_d1_allowed_merchant_origins": ""},
        {"agent_d1_allowed_merchant_origins": "ftp://otro-comercio.example"},
        {"agent_d1_allowed_merchant_origins": "http://otro-comercio.example"},
        {"agent_d1_allowed_merchant_origins": f"{MERCHANT}/private?token=1"},
    ],
)
def test_invalid_enforce_settings_refuse_to_start(tmp_path: Path, change: dict[str, Any]) -> None:
    settings = configuration(tmp_path / "agent.db", mode="enforce", **change)
    merchant = Merchant()
    with pytest.raises(RuntimeError) as refused:
        runtime(settings, merchant)
    assert str(refused.value) == "AGENT_GOVERNANCE_MODE=enforce requires valid D1 settings"
    assert refused.value.__suppress_context__ and refused.value.__cause__ is None
    assert merchant.requests == [] and not settings.agent_database_path.exists()


def test_policy_file_and_allowlist_are_exclusive(tmp_path: Path) -> None:
    payload, binding = build_d1_policy(
        policy_set_id="test-backend-d1", version=1, merchant_ref="merchant:reviewed"
    )
    (tmp_path / "policy.json").write_bytes(payload)
    files = {
        "agent_d1_policy_path": tmp_path / "policy.json",
        "agent_d1_policy_digest": binding.artifact_digest,
    }
    accepted = runtime(configuration(tmp_path / "file.db", mode="enforce", **files), Merchant())
    accepted.close()
    with pytest.raises(RuntimeError):
        runtime(
            configuration(
                tmp_path / "both.db",
                mode="enforce",
                agent_d1_allowed_merchant_origins=MERCHANT,
                **files,
            ),
            Merchant(),
        )


def test_defaults_never_assemble_enforce(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    assert defaults.agent_governance_mode == "off"
    assert defaults.agent_d1_allowed_merchant_origins is None

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("enforce assembled without opt-in")

    monkeypatch.setattr(bootstrap, "build_enforce_gate", forbidden)
    for mode in ("off", "shadow"):
        built = runtime(configuration(tmp_path / f"{mode}.db", mode=mode), Merchant())
        try:
            assert built.d1_gate is None and built.purchases._d1_gate is None
        finally:
            built.close()


def test_shadow_allowlist_only_observes(tmp_path: Path) -> None:
    settings = configuration(tmp_path / "shadow.db", agent_d1_allowed_merchant_origins=UNLISTED)
    built = runtime(settings, Merchant())
    try:
        built.purchases._checkout = SpyGateway(settings.agent_database_path)
        assert start(built.purchases).stage == "awaiting_confirmation"
    finally:
        built.close()
    (record,) = decisions(settings.agent_database_path)
    assert record.decision.governance_mode == "shadow" and record.decision.outcome == "deny"
    invalid = configuration(tmp_path / "invalid.db", agent_d1_allowed_merchant_origins="ftp://x")
    assert isinstance(build_shadow_observer(invalid, None), UnavailableShadowObserver)
