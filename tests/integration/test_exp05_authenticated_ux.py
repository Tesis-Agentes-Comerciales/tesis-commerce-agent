"""Real buyer JWT transport. No model configured, no semantic UX verdicts."""

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing

import pytest

from commerce_agent.api import create_app
from commerce_evaluation.conversation import DETAILS, ConversationHTTPError, request_json
from tests.integration.exp01_support import ROOT, recorded_case, sqlite_snapshot
from tests.integration.live_stack import AGENT, clean_environment, free_port
from tests.integration.test_exp02_commercial import local_effects, observe

PROTOCOL = ROOT / "evaluation/scenarios/exp05-authenticated-transport.json"


class InvalidAuth:
    def token(self) -> str:
        return "synthetic-invalid"


def test_real_auth_adapter_without_model(b0, tmp_path):
    # POSIX service helpers are reused lazily, keeping offline collection portable.
    from commerce_evaluation.auth_client import AuthClient
    from tests.integration.live_browser import ExplicitSettings, NodeServer
    from tests.support.agent_server import AgentServer

    episode = b0.episode("exp05-auth")
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
    settings = ExplicitSettings(
        merchant_base_url=b0.origin,
        merchant_bearer_token=episode.bearer,
        merchant_webhook_secret=b0.webhook_secret,
        agent_approval_secret=b0.approval_secret,
        agent_database_path=tmp_path / "agent.sqlite",
        agent_feed_source="file",
        agent_feed_path=episode.feed,
        agent_auth_mode="jwt",
        agent_auth_issuer=origin,
        openai_api_key=None,
        payment_provider_url=b0.payment.origin,
        payment_agent_bearer_token=b0.payment_token,
    )
    plan = json.loads(PROTOCOL.read_text())
    with recorded_case(
        tmp_path,
        "AUTH:transport",
        {
            "run_id": episode.run_id,
            "actor_id": episode.actor_id,
            "mode": "HP",
            "prompts": plan["prepared_prompts"],
            "openai_api_key_configured": False,
            "router": "none; production model-unavailable path, not scripted clarification",
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
            for case_id, prompt in plan["prepared_prompts"].items():
                with pytest.raises(ConversationHTTPError) as unavailable:
                    request_json(
                        server.origin,
                        "/conversation/turn",
                        {
                            "turn_id": "exp05-auth-" + case_id,
                            "messages": [{"role": "user", "content": prompt}],
                            "details": DETAILS,
                        },
                        auth=auth,
                    )
                turns[case_id] = {
                    "status": unavailable.value.status_code,
                    "body": unavailable.value.body,
                }
                assert unavailable.value.status_code == 502
                assert unavailable.value.body["code"] == "intent_model_error"
            counts = local_effects(settings.agent_database_path)
            with closing(sqlite3.connect(settings.agent_database_path)) as db:
                for table in ("purchases", "model_requests"):
                    counts[table] = db.execute("SELECT count(*) FROM " + table).fetchone()[0]
            after = observe(b0, episode)
            facts.update(
                prepared_turns=turns,
                agent_effect_counts=counts,
                merchant_after=after,
                psp_after=sqlite_snapshot(b0.root / "payments.sqlite"),
                auth_after_requests=sqlite_snapshot(auth_db),
                semantic_scenarios=dict.fromkeys(plan["prepared_prompts"], "not_executed"),
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
