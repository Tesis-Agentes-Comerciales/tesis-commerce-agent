"""07A: real first POST through D1, never a recovery fixture's setup POST."""

import json
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.creation_lookup import BackendLookupSession
from commerce_agent.acp.feed import ProductFeed
from commerce_agent.acp.initial_dispatch import HttpInitialCheckoutDispatch
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.approval import ApprovalService
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.dispatch_envelope import ExpectedRemoteBinding
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    D1EnforceConfiguration,
    ProvenanceKeys,
)
from commerce_agent.governance.remote_creation import SessionScope, original_response_sha256
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.d1_envelope_sqlite import SqliteD1EnvelopeStore
from tests.integration.gov06dc_harness import (
    JointCase,
    LiveEnvironment,
    NoEffectsGateway,
    connect,
    live_requirements,
    port,
)
from tests.integration.test_live_creation_recovery import child_probe
from tests.test_purchase_service import details

HEAD = "9f15d20906f576b7d28138ff9f5708022b405ae8"
ALLOWED = frozenset(
    {"src/commerce_agent/governance/enforce.py", "tests/integration/gov06dc_harness.py"}
)
pytestmark = live_requirements(
    "RUN_GOV07A_REAL",
    "requires RUN_GOV07A_REAL=1 and a new isolated PostgreSQL/Merchant cluster",
    windows_listener=True,
)


@pytest.fixture(scope="module")
def live():
    # The original 06D-C profile and scenarios retain their default behavior.
    previous = os.environ.get("RUN_GOV06DC_REAL")
    os.environ["RUN_GOV06DC_REAL"] = "1"
    environment = LiveEnvironment(agent_head=HEAD, allowed_agent_changes=ALLOWED)
    try:
        environment.start()
        yield environment
    finally:
        environment.close()
        if previous is None:
            os.environ.pop("RUN_GOV06DC_REAL", None)
        else:
            os.environ["RUN_GOV06DC_REAL"] = previous


class InitialProxy:
    """Real socket failure only after Merchant answered the genuine first POST."""

    def __init__(self, live, mode="pass"):
        self.live, self.mode = live, mode
        self.requests = []
        self.original_response: dict[str, object] | None = None
        self.before_initial: Callable[[bytes, dict[str, str]], None] | None = None
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args):
                pass

            def do_POST(self):
                if self.path not in {
                    "/checkout_sessions",
                    "/lab/recovery/checkout-creation/lookup",
                }:
                    self.send_error(405)
                    return
                parent.requests.append((self.command, self.path))
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in {"host", "connection", "content-length"}
                }
                if self.path == "/checkout_sessions" and parent.before_initial is not None:
                    parent.before_initial(body, headers)
                with httpx.Client(trust_env=False, timeout=10) as client:
                    result = client.post(
                        parent.live.origin + self.path, content=body, headers=headers
                    )
                if self.path == "/checkout_sessions":
                    assert result.status_code == 201
                    parent.original_response = result.json()
                    if parent.mode == "drop":
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    if parent.mode == "delay":
                        time.sleep(0.8)
                try:
                    self.send_response(result.status_code)
                    for key in ("content-type", "cache-control", "request-id", "idempotency-key"):
                        if key in result.headers:
                            self.send_header(key, result.headers[key])
                    self.send_header("content-length", str(len(result.content)))
                    self.end_headers()
                    self.wfile.write(result.content)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port()), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def provision(live, origin, *, credential_fault=None, timeout: float = 5):
    index = secrets.token_hex(6)
    episode = live.rpc("episode", actor="gov07a-" + index)
    credential = live.rpc("issue", run_id=episode["run_id"], actor_id=episode["actor_id"])
    source = JointCase.source_ref(credential)
    scope = SessionScope(
        assurance="backend_session_registry/1",
        remote_run_id=credential["run_id"],
        remote_actor_id=credential["actor_id"],
        source_ref=source,
    )
    expected = ExpectedRemoteBinding(
        assurance="backend_expected_not_authenticated",
        remote_run_id=scope.remote_run_id,
        remote_actor_id=scope.remote_actor_id,
        source_ref=source,
    )
    if credential_fault == "scope":
        scope = scope.model_copy(update={"remote_actor_id": "contradictory"})
    bearer = None if credential_fault == "absent" else SecretStr(credential["bearer"])
    dispatch = HttpInitialCheckoutDispatch(
        BackendLookupSession(origin, scope, bearer),
        timeout_seconds=timeout,
    )
    keys = ProvenanceKeys(
        key_id="gov07a-ephemeral",
        pseudonym_key=SecretBytes(secrets.token_bytes(32)),
        encryption_key=SecretBytes(secrets.token_bytes(32)),
    )
    path = live.root / ("gov07a-agent-" + index + ".sqlite")
    owner, run = "gov07a-owner:" + index, "gov07a-local:" + index
    store = SqliteAgentStore(path)
    store.initialize()
    clock = ClockProfile(
        authority_source="system_utc",
        commercial_source="fixed_sandbox",
        authority_provider=None,
        commercial_provider=None,
        fixed_commercial_at="2026-09-10T14:00:00.000Z",
    )
    config = D1EnforceConfiguration(
        governance_mode="enforce",
        merchant_base_url=origin,
        policy_set_id="gov07a-cop",
        policy_revision=1,
        currency="COP",
        quantity=1,
        clock=clock,
    )
    gate = SqliteD1EnforceGate(
        path,
        config,
        keys,
        ClockReader(clock),
        initial_dispatch=dispatch,
        expected_remote_binding=expected,
    )
    offer = episode["offer"]
    feed = ProductFeed.model_validate_json(
        json.dumps(
            {
                "metadata": {
                    "id": "gov07a-feed",
                    "target_country": "CO",
                    "updated_at": "2026-09-10T14:00:00Z",
                },
                "products": [
                    {
                        "id": offer["product_id"],
                        "title": offer["name"],
                        "url": origin + "/fixture/product",
                        "variants": [
                            {
                                "id": offer["id"],
                                "title": offer["name"],
                                "url": origin + "/fixture/variant",
                                "condition": ["new"],
                                "price": {
                                    "amount": offer["pricing"]["items_total_minor"],
                                    "currency": "COP",
                                },
                                "availability": {"available": True, "status": "in_stock"},
                            }
                        ],
                    }
                ],
            }
        )
    )
    service = PurchaseService(
        feed=feed,
        checkout=NoEffectsGateway(),
        store=store,
        approvals=ApprovalService(store, SecretStr(secrets.token_hex(32))),
        d1_gate=gate,
    )
    case = JointCase(live, index, origin, episode, credential, path, keys, owner, run, "", None)

    def attempt():
        try:
            service.start(
                run_id=run,
                owner_id=owner,
                request="07A deliberate D1 first dispatch",
                intent=PurchaseIntent(hard=HardConstraints(currency="COP")),
                selected_variant_id=offer["id"],
                details=details(),
            )
        except PurchaseServiceError as error:
            return error.code
        return "unexpected_continuation"

    return case, gate, dispatch, attempt


def recover_envelope(case):
    with closing(connect(case.path)) as db:
        case.operation_id = db.execute(
            "SELECT operation_id FROM d1_checkout_operations"
        ).fetchone()[0]
        case.envelope = SqliteD1EnvelopeStore(db, case.keys).recover(
            case.operation_id, run_id=case.local_run, owner_id=case.owner
        )
    assert case.envelope is not None


@pytest.mark.parametrize("mode", ["pass", "drop", "delay", "concurrent"])
def test_real_d1_initial_dispatch_then_recovery_without_continuation(live, mode):
    proxy = InitialProxy(live, mode=mode)
    case, gate, dispatch, attempt = provision(
        live, proxy.origin, timeout=0.5 if mode == "delay" else 5
    )

    def committed_before_http(body, headers):
        recover_envelope(case)
        with closing(connect(case.path)) as db:
            operation = db.execute(
                "SELECT state FROM d1_checkout_operations WHERE operation_id=?",
                (case.operation_id,),
            ).fetchone()
            assert operation is not None and operation[0] == "claimed"
        assert body == case.envelope.body_canonical.encode("utf-8")
        assert headers["Idempotency-Key"] == case.envelope.idempotency_key.get_secret_value()

    proxy.before_initial = committed_before_http
    try:
        before = live.rpc("snapshot")
        if mode == "concurrent":
            with ThreadPoolExecutor(2) as pool:
                outcomes = list(pool.map(lambda _: attempt(), range(2)))
        else:
            outcomes = [attempt()]
        recover_envelope(case)
        expected = (
            "d1_dispatch_uncertain" if mode in {"drop", "delay"} else "d1_initial_creation_observed"
        )
        assert expected in outcomes
        after = live.rpc("snapshot")
        assert after["checkout_sessions"]["count"] == before["checkout_sessions"]["count"] + 1
        assert proxy.requests == [("POST", "/checkout_sessions")]
        for table in ("orders", "checkout_mutations", "checkout_completion_attempts"):
            assert before[table] == after[table]
        assert case.setup_creations == 0  # Unlike 06D-C, creation came from the gate.
        assert attempt() == "d1_recovery_required"
        assert proxy.requests == [("POST", "/checkout_sessions")]
        observed_checkout_id = None
        observed_snapshot_digest = None
        if mode in {"pass", "concurrent"}:
            events = gate.post_create_flow(case.local_run, case.owner).inspect()
            assert len(events) == 1 and events[0].phase == "creation_observed"
            observed_checkout_id = events[0].checkout_id
            observed_snapshot_digest = events[0].snapshot_digest
        result = case.verify_zero(expected="found_verified")
        assert result.durable and result.event is not None
        assert proxy.original_response is not None
        assert result.verification.original_response_digest == (
            "sha256:" + original_response_sha256(proxy.original_response)
        )
        if mode in {"pass", "concurrent"}:
            original = CheckoutSession.model_validate(proxy.original_response)
            assert observed_snapshot_digest == sha256_digest(canonical_bytes(original))
        assert (
            not result.verification.replay_allowed and not result.verification.continuation_allowed
        )
        # Reopen all SQLite connections and verifier; inspect cached evidence with zero HTTP.
        with closing(case.client()) as client:
            restored = case.verifier(client).inspect(case.request())
        assert len(restored) == 1
        network_count = len(proxy.requests)
        restarted = child_probe(case, "inspect")
        assert restarted is not None and restarted["count"] == 1
        assert len(proxy.requests) == network_count
        if mode in {"pass", "concurrent"}:
            assert restored[0].local.historical_checkout_id == observed_checkout_id
        assert proxy.requests.count(("POST", "/checkout_sessions")) == 1
        assert not any(
            path not in {"/checkout_sessions", "/lab/recovery/checkout-creation/lookup"}
            for _, path in proxy.requests
        )
        live.record(
            "gov07a_" + mode,
            outcome=outcomes,
            initial_posts=1,
            fixture_posts=0,
            recovered="found_verified",
            durable=True,
            commercial_continuation=False,
            checkout_count_before=before["checkout_sessions"]["count"],
            checkout_count_after=after["checkout_sessions"]["count"],
            post_create_events=0 if mode in {"drop", "delay"} else 1,
        )
    finally:
        dispatch.close()
        proxy.close()


@pytest.mark.parametrize("fault", ["absent", "scope"])
def test_backend_identity_must_match_before_initial_post(live, fault):
    proxy = InitialProxy(live)
    case, gate, dispatch, attempt = provision(live, proxy.origin, credential_fault=fault)
    try:
        before = live.rpc("snapshot")
        assert attempt() == "d1_unavailable"
        assert not proxy.requests
        status = gate.recovery_status(case.local_run)
        assert status is not None and status[0] == "reserved"
        assert live.rpc("snapshot") == before
        live.record("gov07a_identity_" + fault, initial_posts=0, tables_unchanged=True)
    finally:
        dispatch.close()
        proxy.close()
