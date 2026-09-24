import sqlite3
from pathlib import Path

import pytest
from assistant_support import PURCHASE_REQUEST, build_app, checkout_details
from assistant_support import settings as fixture_settings
from fastapi.testclient import TestClient

from commerce_agent.api import create_app
from commerce_agent.buyer_auth import BuyerPrincipal


class TwoBuyers:
    def authenticate(self, authorization: str | None) -> BuyerPrincipal | None:
        if authorization == "Bearer buyer-a":
            return BuyerPrincipal("buyer-a")
        if authorization == "Bearer buyer-b":
            return BuyerPrincipal("buyer-b")
        return None


def _headers(buyer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {buyer}"}


def test_pending_purchase_and_approval_are_owned_by_authenticated_buyer(tmp_path: Path) -> None:
    app = build_app(tmp_path / "agent.db", buyer_authenticator=TwoBuyers())
    with TestClient(app) as client:
        body = {
            "turn_id": "same-turn-id",
            "conversation_id": "same-conversation-id",
            "messages": [{"role": "user", "content": PURCHASE_REQUEST}],
            "details": checkout_details(),
        }
        assert client.post("/conversation/turn", json=body).status_code == 401
        created = client.post("/conversation/turn", json=body, headers=_headers("buyer-a"))
        assert created.status_code == 200, created.text
        response = created.json()
        run_id = response["purchase"]["run_id"]
        fingerprint = response["approval_terms"]["fingerprint"]

        owner_pending = client.get("/me/pending-purchase", headers=_headers("buyer-a"))
        assert owner_pending.status_code == 200
        assert owner_pending.json()["purchase"]["run_id"] == run_id
        assert owner_pending.json()["approval_terms"]["fingerprint"] == fingerprint
        assert client.get("/me/pending-purchase", headers=_headers("buyer-b")).json() is None

        for method, path, json_body in (
            ("GET", f"/purchases/{run_id}", None),
            ("GET", f"/purchases/{run_id}/approval", None),
            ("POST", f"/purchases/{run_id}/approval", {"expected_fingerprint": fingerprint}),
            ("POST", f"/purchases/{run_id}/cancel", {}),
            ("POST", f"/purchases/{run_id}/complete", {}),
        ):
            denied = client.request(method, path, json=json_body, headers=_headers("buyer-b"))
            assert denied.status_code == 404, (method, path, denied.text)

        approved = client.post(
            f"/purchases/{run_id}/approval",
            json={"expected_fingerprint": fingerprint},
            headers=_headers("buyer-a"),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["principal_id"] == "buyer-a"


def test_missing_approval_snapshot_is_rebuilt_for_its_owner(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    app = build_app(database, buyer_authenticator=TwoBuyers())
    with TestClient(app) as client:
        created = client.post(
            "/conversation/turn",
            json={
                "turn_id": "prepare",
                "messages": [{"role": "user", "content": PURCHASE_REQUEST}],
                "details": checkout_details(),
            },
            headers=_headers("buyer-a"),
        )
        assert created.status_code == 200, created.text
        run_id = created.json()["purchase"]["run_id"]
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM approval_terms WHERE run_id = ?", (run_id,))

        restored = client.get("/me/pending-purchase", headers=_headers("buyer-a"))
        assert restored.status_code == 200, restored.text
        assert (
            restored.json()["approval_terms"]["fingerprint"]
            == created.json()["approval_terms"]["fingerprint"]
        )
        assert client.get("/me/pending-purchase", headers=_headers("buyer-b")).json() is None


def test_public_ui_cannot_start_with_local_buyer_auth(tmp_path: Path) -> None:
    config = fixture_settings(tmp_path / "agent.db").model_copy(
        update={"agent_ui_origin": "https://public.example.test"}
    )
    with pytest.raises(RuntimeError, match="AGENT_AUTH_MODE=jwt"):
        create_app(settings=config)


def test_turn_ids_are_scoped_per_buyer(tmp_path: Path) -> None:
    app = build_app(tmp_path / "agent.db", buyer_authenticator=TwoBuyers())
    with TestClient(app) as client:
        body = {
            "turn_id": "shared-turn",
            "conversation_id": "shared-conversation",
            "messages": [{"role": "user", "content": "hola"}],
            "details": checkout_details(),
        }
        for buyer in ("buyer-a", "buyer-b"):
            result = client.post("/conversation/turn", json=body, headers=_headers(buyer))
            assert result.status_code == 200, result.text
            assert result.json()["action"] == "respond"
