"""Real HP router/JWT/SDK and ledger, mock errors only; no model inference."""

import hashlib
import secrets
import sqlite3
from contextlib import closing

import pytest
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.llm.model_requests import ModelRequestRecorder
from commerce_evaluation.conversation import DETAILS, ConversationHTTPError, request_json
from commerce_evaluation.exp06 import AttemptBudget, messages, protocol_hash
from tests.integration.exp01_support import ROOT, recorded_case, sqlite_snapshot
from tests.integration.live_stack import AGENT, clean_environment, free_port
from tests.integration.test_exp02_commercial import local_effects, observe
from tests.support.model_server import ModelServer, failure

PROTOCOL = ROOT / "evaluation/scenarios/exp06-hp-infrastructure.json"


class InvalidAuth:
    def token(self) -> str:
        return "synthetic-invalid"


def test_real_router_ledger_without_paid_model(b0, tmp_path, monkeypatch):
    # POSIX service helpers are reused lazily, keeping offline collection portable.
    from commerce_evaluation.governance_pilot import AuthClient
    from tests.integration.live_browser import AgentServer, ExplicitSettings, NodeServer
    from tests.integration.test_live_b2_d2_authorization import settings_for

    model_server = ModelServer(failure(503), failure(503))
    model_server.install(monkeypatch)
    budget = AttemptBudget(None, infrastructure_only=True)
    original_client = ModelRequestRecorder.http_client

    def bounded_client(self):
        client = original_client(self)
        assert client is not None
        return budget.attach(client)

    monkeypatch.setattr(ModelRequestRecorder, "http_client", bounded_client)
    episode = b0.episode("exp06-auth")
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    auth_db = tmp_path / "auth.sqlite"
    auth_server = NodeServer(
        tmp_path,
        "auth",
        AGENT / "auth",
        ("server.js",),
        clean_environment()
        | {
            "BETTER_AUTH_URL": origin,
            "BETTER_AUTH_SECRET": secrets.token_urlsafe(32),
            "AUTH_DATABASE_PATH": str(auth_db),
            "PORT": str(port),
        },
        port,
        "/api/auth/jwks",
    )
    base = settings_for(b0, episode, tmp_path, monkeypatch)
    settings = ExplicitSettings(
        **(
            base.model_dump()
            | {
                "agent_auth_issuer": origin,
                "openai_api_key": SecretStr("exp06-mock-only-not-a-real-key"),
                "openai_model": "gpt-5-mini-2025-08-07",
                "openai_reasoning_effort": "minimal",
                "openai_timeout_seconds": 30,
            }
        )
    )

    with recorded_case(
        tmp_path,
        "EXP06:infrastructure",
        {
            "run_id": episode.run_id,
            "actor_id": episode.actor_id,
            "mode": "HP",
            "messages": messages("UX01"),
            "semantic_protocol_sha256": protocol_hash(),
            "catalog_complete": {
                file.name: file.read_text()
                for file in sorted(episode.feed.iterdir())
                if file.is_file()
            },
            "credential": "synthetic-only; MockTransport required",
            "router": "production OpenAIConversationRouter; ModelServer errors only",
            "feed_sha256": hashlib.sha256(
                (episode.feed / "products.jsonl").read_bytes()
            ).hexdigest(),
        },
        protocol_path=PROTOCOL,
    ) as record:
        facts = record["observations"]
        server = None
        try:
            before = observe(b0, episode)
            psp = sqlite_snapshot(b0.root / "payments.sqlite")
            facts.update(merchant_before=before, psp_before=psp)
            auth_server.start()
            auth = AuthClient(origin)
            buyer_id = auth.signup()
            facts["auth_after_signup"] = sqlite_snapshot(auth_db)
            server = AgentServer(create_app(settings=settings))
            server.start()
            statuses = {}
            for name, source in [
                ("missing", None),
                ("invalid", InvalidAuth()),
            ]:
                with pytest.raises(ConversationHTTPError) as refused:
                    request_json(server.origin, "/me/purchases", auth=source)
                statuses[name] = refused.value.status_code
                assert refused.value.status_code == 401
            history = request_json(server.origin, "/me/purchases", auth=auth)
            assert isinstance(history, dict)
            statuses["authenticated_history"] = 200
            with pytest.raises(ConversationHTTPError) as redirect:
                request_json(server.origin, "/me/purchases/", auth=auth)
            statuses["redirect_refused"] = redirect.value.status_code
            assert redirect.value.status_code == 307
            facts.update(http_statuses=statuses, buyer_id=buyer_id)
            record["stimulus_verified"] = True
            turns = {}
            budget.begin_episode()
            with pytest.raises(ConversationHTTPError) as unavailable:
                request_json(
                    server.origin,
                    "/conversation/turn",
                    {
                        "turn_id": "exp06-infrastructure",
                        "messages": messages("UX01"),
                        "details": DETAILS,
                    },
                    auth=auth,
                )
            turns["UX01"] = {
                "status": unavailable.value.status_code,
                "body": unavailable.value.body,
            }
            assert unavailable.value.status_code == 502
            assert unavailable.value.body["code"] == "intent_model_error"
            counts = local_effects(settings.agent_database_path)
            with closing(sqlite3.connect(settings.agent_database_path)) as db:
                for table in ("purchases", "model_requests"):
                    counts[table] = db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                db.row_factory = sqlite3.Row
                ledger = [
                    dict(row)
                    for row in db.execute("SELECT * FROM model_requests ORDER BY sequence")
                ]
            assert len(ledger) == 1
            row = ledger[0]
            assert row["outcome"] == "error" and row["retries"] == 1
            assert row["model"] == settings.openai_model and row["input_tokens"] is None
            assert row["input_json"] and row["request_sha256"] and row["latency_ms"] >= 0
            assert len(model_server.requests) == budget.attempts == 2
            assert counts.pop("model_requests") == 1
            facts["model_requests"] = ledger
            facts["budget"] = {
                "attempts": budget.attempts,
                "reserved_mock_usd": str(budget.reserved_or_spent),
                "real_provider_calls": 0,
                "actual_api_usd": "0",
                "events": budget.events,
            }
            facts["frozen_catalog_sha256"] = hashlib.sha256(
                (episode.feed / "products.jsonl").read_bytes()
            ).hexdigest()
            after = observe(b0, episode)
            facts.update(
                prepared_turns=turns,
                agent_effect_counts=counts,
                merchant_after=after,
                psp_after=sqlite_snapshot(b0.root / "payments.sqlite"),
                auth_after_requests=sqlite_snapshot(auth_db),
                semantic_scenarios=dict.fromkeys(["UX01", "UX11", "UX12"], "not_executed"),
            )
            assert all(v == 0 for v in counts.values()) and before == after
            assert facts["psp_after"] == psp and not b0.proxy.calls
        finally:
            if server is not None:
                server.stop()
            auth_server.stop()
            facts["after_cleanup"] = observe(b0, episode)
            record["cleanup_verified"] = (
                auth_server.process is None
                and (server is None or not server.thread.is_alive())
                and not facts["after_cleanup"]["checkouts"]
            )
            assert record["cleanup_verified"]
