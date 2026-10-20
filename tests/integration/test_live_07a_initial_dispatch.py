"""07A live: the real first POST goes through D1, never a recovery fixture's setup POST."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from pydantic import SecretStr

from commerce_agent.acp.creation_lookup import BackendLookupSession
from commerce_agent.acp.initial_dispatch import HttpInitialCheckoutDispatch
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application import PurchaseServiceError
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.provenance import ClockReader
from commerce_agent.governance.remote_creation import LOOKUP_PATH, original_response_sha256
from commerce_agent.persistence import SqliteAgentStore
from tests.integration.live_stack import (
    CREATE_PATH,
    FIXED_CLOCK,
    JointCase,
    connect,
    live_stack_requirements,
)

pytestmark = live_stack_requirements()
UNCHANGED = ("orders", "checkout_mutations", "checkout_completion_attempts")


def provision(stack, *, credential_fault=None, timeout: float = 5):
    case = JointCase.open(stack, "07a")
    scope = case.scope()
    if credential_fault == "scope":
        scope = scope.model_copy(update={"remote_actor_id": "contradictory"})
    bearer = None if credential_fault == "absent" else SecretStr(case.credential["bearer"])
    dispatch = HttpInitialCheckoutDispatch(
        BackendLookupSession(case.origin, scope, bearer), timeout_seconds=timeout
    )
    store = SqliteAgentStore(case.path)
    store.initialize()
    gate = SqliteD1EnforceGate(
        case.path,
        case.configuration("07a-cop"),
        case.keys,
        ClockReader(FIXED_CLOCK),
        initial_dispatch=dispatch,
        expected_remote_binding=case.expected_binding(),
    )
    service = case.service(store, gate)

    def attempt() -> str:
        try:
            case.start(service, "07A deliberate D1 first dispatch")
        except PurchaseServiceError as error:
            return error.code
        return "unexpected_continuation"

    return case, gate, dispatch, attempt


def committed_before_http(case) -> Callable[[bytes, dict[str, str]], None]:
    def check(body: bytes, headers: dict[str, str]) -> None:
        case.recover_envelope()
        with closing(connect(case.path)) as db:
            operation = db.execute(
                "SELECT state FROM d1_checkout_operations WHERE operation_id=?",
                (case.operation_id,),
            ).fetchone()
        assert operation is not None and operation[0] == "claimed"
        assert body == case.envelope.body_canonical.encode("utf-8")
        assert headers["Idempotency-Key"] == case.envelope.idempotency_key.get_secret_value()

    return check


@pytest.mark.parametrize("mode", ["pass", "drop", "reset", "delay", "concurrent"])
def test_07a_real_d1_initial_dispatch_then_recovery_without_continuation(stack, proxy, mode):
    case, gate, dispatch, attempt = provision(stack, timeout=0.5 if mode == "delay" else 5)
    proxy.before_forward[CREATE_PATH] = committed_before_http(case)
    if mode in {"drop", "reset", "delay"}:
        proxy.faults[CREATE_PATH] = mode
        proxy.delay_seconds = 1.0
    try:
        before = stack.rpc("snapshot")
        if mode == "concurrent":
            with ThreadPoolExecutor(2) as pool:
                outcomes = list(pool.map(lambda _: attempt(), range(2)))
        else:
            outcomes = [attempt()]
        # The Merchant processed the POST even when its answer was lost or late.
        assert proxy.calls[0].answered.wait(10)
        case.recover_envelope()
        lost = mode in {"drop", "reset", "delay"}
        assert ("d1_dispatch_uncertain" if lost else "d1_initial_creation_observed") in outcomes
        after = stack.rpc("snapshot")
        assert after["checkout_sessions"]["count"] == before["checkout_sessions"]["count"] + 1
        assert proxy.requests() == [("POST", CREATE_PATH)]
        for table in UNCHANGED:
            assert before[table] == after[table]
        assert case.setup_creations == 0  # Unlike 06D-C, creation came from the gate.
        assert attempt() == "d1_recovery_required"
        assert proxy.requests() == [("POST", CREATE_PATH)]
        observed_checkout_id = observed_snapshot_digest = None
        if not lost:
            events = gate.post_create_flow(case.local_run, case.owner).inspect()
            assert len(events) == 1 and events[0].phase == "creation_observed"
            observed_checkout_id = events[0].checkout_id
            observed_snapshot_digest = events[0].snapshot_digest
        result = case.verify_zero(expected="found_verified")
        assert result.durable and result.event is not None
        original_response = json.loads(proxy.calls[0].response_body or b"null")
        assert proxy.calls[0].status == 201
        assert result.verification.original_response_digest == (
            "sha256:" + original_response_sha256(original_response)
        )
        if not lost:
            original = CheckoutSession.model_validate(original_response)
            assert observed_snapshot_digest == sha256_digest(canonical_bytes(original))
        assert (
            not result.verification.replay_allowed and not result.verification.continuation_allowed
        )
        # Reopen all SQLite connections and the verifier; cached evidence, zero HTTP.
        with closing(case.client()) as client:
            restored = case.verifier(client).inspect(case.request())
        assert len(restored) == 1
        network_count = len(proxy.calls)
        restarted = case.probe()
        assert restarted is not None and restarted["count"] == 1
        assert len(proxy.calls) == network_count
        if not lost:
            assert restored[0].local.historical_checkout_id == observed_checkout_id
        assert proxy.requests() == [("POST", CREATE_PATH), ("POST", LOOKUP_PATH)]
        stack.record(
            "07a:" + mode,
            outcome=outcomes,
            initial_posts=1,
            fixture_posts=0,
            recovered="found_verified",
            checkout_count_before=before["checkout_sessions"]["count"],
            checkout_count_after=after["checkout_sessions"]["count"],
            post_create_events=0 if lost else 1,
        )
    finally:
        dispatch.close()


def test_07a_gateway_503_before_merchant_is_uncertain_not_absence(stack, proxy):
    """The POST never reached the Merchant; the Agent still may not infer absence or resend."""
    case, _gate, dispatch, attempt = provision(stack)
    proxy.faults[CREATE_PATH] = "status_503"
    try:
        before = stack.rpc("snapshot")
        assert attempt() == "d1_dispatch_uncertain"
        assert not proxy.calls[0].answered.is_set()
        assert stack.rpc("snapshot") == before
        assert attempt() == "d1_recovery_required"
        case.recover_envelope()
        result = case.verify_zero(expected="not_found")
        assert result.verification.reason == "NOT_FOUND_NOT_ABSENCE"
        assert proxy.requests() == [("POST", CREATE_PATH), ("POST", LOOKUP_PATH)]
        stack.record("07a:gateway_503", initial_posts_forwarded=0, recovered="not_found")
    finally:
        dispatch.close()


@pytest.mark.parametrize("fault", ["absent", "scope"])
def test_07a_backend_identity_must_match_before_initial_post(stack, proxy, fault):
    case, gate, dispatch, attempt = provision(stack, credential_fault=fault)
    try:
        before = stack.rpc("snapshot")
        assert attempt() == "d1_unavailable"
        assert not proxy.calls
        status = gate.recovery_status(case.local_run)
        assert status is not None and status[0] == "reserved"
        assert stack.rpc("snapshot") == before
        stack.record("07a:identity_" + fault, initial_posts=0, tables_unchanged=True)
    finally:
        dispatch.close()
