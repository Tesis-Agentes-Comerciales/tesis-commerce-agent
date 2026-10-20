"""07B-B live: P -> Q shadow -> applied D1 -> one guarded D17 POST -> read-only recovery.

Each scenario runs Agent processes of its own against the Merchant over loopback HTTP.
I05/I06, I09 and I10 replace the unpublished temporary worker of the HP/B2 evidence:
the deny policy enters through SqliteGuardedD1's constructor, and the crashes are SIGKILLs
at named cutpoints of its injected transport.
"""

import json
import secrets
import signal
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from commerce_agent.governance.guarded_creation import CREATE_PATH, LOOKUP_PATH
from tests.integration.live_stack import AgentProcess

# About a minute per scenario: each one selects anew over the Merchant's full catalog.
pytestmark = pytest.mark.slow
UNCHANGED = ("orders", "checkout_mutations", "checkout_completion_attempts")
EDITS = ("revision", "digest", "shipping", "expiry")


def agent_data(stack, *, policy: str = "default", timeout: float = 10) -> dict[str, Any]:
    catalog = stack.catalog()
    return {
        "directory": str(stack.root / ("07bb-" + secrets.token_hex(6))),
        "ephemeral_secret": secrets.token_hex(32),
        "origin": stack.origin,
        "run_id": catalog["run_id"],
        "actor_id": catalog["actor_id"],
        "merchant_id": catalog["merchant_id"],
        "token": SecretStr(catalog["credential"]["bearer"]),
        "policy": policy,
        "timeout": timeout,
    }


def open_agent(stack, data: dict[str, Any], *, fresh: bool) -> tuple[AgentProcess, dict]:
    agent = AgentProcess(stack, Path(data["directory"]))
    try:
        return agent, agent.call("guarded", data=data, fresh=fresh)
    except BaseException:
        agent.close()
        raise


def refused(agent: AgentProcess, action: str) -> str | None:
    reply = agent.call(action, required=False)
    assert not reply["ok"]
    return reply["error_code"]


def assert_no_effects(stack, before: dict[str, int]) -> None:
    assert stack.rpc("effects") == before


@pytest.mark.parametrize(
    "scenario",
    ["received", "lost", "killed_after_answer", "revision", "digest", "shipping", "expiry"],
)
def test_07bb_new_selection_applied_d1_one_guarded_post_and_readonly_recovery(
    stack, proxy, scenario
):
    catalog = stack.catalog()
    before = stack.rpc("effects")
    data = agent_data(stack, timeout=0.5 if scenario == "lost" else 10)
    agent, _ = open_agent(stack, data, fresh=True)
    guard = None
    try:
        guard = agent.call("prepare")
        assert guard["shadow_binding"]["governance_mode"] == "shadow"
        assert guard["shadow_binding"]["operation_id"] is None
        assert proxy.count("POST", CREATE_PATH) == 0
        offer_id = guard["request"]["offer_id"]
        if scenario in EDITS:
            stack.rpc("edit_offer", run_id=catalog["run_id"], offer_id=offer_id, change=scenario)
        if scenario == "lost":
            # The Merchant commits; its answer arrives after the Agent's 0.5 s timeout.
            proxy.faults[CREATE_PATH] = "delay"
            proxy.delay_seconds = 1.5
        initial = None
        if scenario == "killed_after_answer":
            # The Agent dies after the Merchant answered, before recording the observation.
            assert agent.kill_at("response_before_record", "dispatch") == -signal.SIGKILL
        else:
            initial = agent.call("dispatch")
            assert initial["result"] == ("found" if scenario == "received" else "error")
        commercial = [call for call in proxy.calls if call.path == CREATE_PATH]
        assert len(commercial) == 1 and commercial[0].answered.wait(10)
        if scenario == "received":
            # A later catalog change never replaces the historical proof.
            stack.rpc("edit_offer", run_id=catalog["run_id"], offer_id=offer_id, change="revision")
        old_pid = agent.pid
        agent.close()
        # Recovery runs in a new process with an ordinary timeout; only the POST was late.
        agent, _ = open_agent(stack, {**data, "timeout": 10}, fresh=False)
        assert agent.pid != old_pid
        expected = "rejected" if scenario in EDITS else "found"
        first = agent.call("recover")
        assert first["result"] == expected
        assert not first["replay_allowed"] and not first["continuation_allowed"]
        assert agent.call("recover") == first
        assert refused(agent, "dispatch") == "INITIAL_DISPATCH_ALREADY_CLAIMED"
        history = agent.call("history")
        assert [event["kind"] for event in history] == (
            ["lookup", "lookup"] if initial is None else ["initial", "lookup", "lookup"]
        )
        assert proxy.count("POST", CREATE_PATH) == 1
        assert proxy.count("POST", LOOKUP_PATH) == 2
        after = stack.rpc("effects")
        created = 1 if expected == "found" else 0
        assert after["checkout_sessions"] - before["checkout_sessions"] == created
        assert after["guarded_checkout_creations"] - before["guarded_checkout_creations"] == created
        assert (
            after["rejected_guarded_checkout_attempts"]
            - before["rejected_guarded_checkout_attempts"]
            == 1 - created
        )
        for name in UNCHANGED:
            assert after[name] == before[name]
        stack.record(
            "07bb:" + scenario,
            initial=initial,
            recovered=first,
            commercial_posts=1,
            readonly_lookups=2,
            before=before,
            after=after,
        )
    finally:
        agent.close()
        if guard is not None and scenario in {"received", *EDITS}:
            stack.rpc(
                "edit_offer",
                run_id=catalog["run_id"],
                offer_id=guard["request"]["offer_id"],
                change="restore",
                snapshot=guard["selected_offer"],
            )


def test_07bb_i05_i06_shadow_allow_and_applied_deny_never_reach_the_merchant(stack, proxy):
    before = stack.rpc("effects")
    agent, opened = open_agent(stack, agent_data(stack, policy="deny_foreign_merchant"), fresh=True)
    try:
        # Q allows in shadow; that confers no authority on the applied decision.
        assert opened["shadow"]["outcome"] == "allow"
        assert opened["shadow"]["governance_mode"] == "shadow"
        assert refused(agent, "prepare") == "d1_denied"
        assert agent.call("decisions") == [
            {"outcome": "deny", "governance_mode": "enforce", "disposition": "blocked"}
        ]
        assert refused(agent, "dispatch") == "APPLIED_D1_REQUIRED"
        assert proxy.count("POST", CREATE_PATH) == proxy.count("POST", LOOKUP_PATH) == 0
        assert_no_effects(stack, before)
        stack.record("i05_i06:deny", guarded_posts=0, lookups=0)
    finally:
        agent.close()


def crash_after_claim(stack, data: dict[str, Any]) -> None:
    agent, _ = open_agent(stack, data, fresh=True)
    try:
        agent.call("prepare")
        assert agent.kill_at("claimed_before_http_send", "dispatch") == -signal.SIGKILL
    finally:
        agent.close()


def test_07bb_i09_killed_after_durable_claim_before_send_never_resends(stack, proxy):
    before = stack.rpc("effects")
    data = agent_data(stack)
    crash_after_claim(stack, data)
    assert proxy.count("POST", CREATE_PATH) == 0
    agent, _ = open_agent(stack, data, fresh=False)
    try:
        first = agent.call("recover")
        assert (first["result"], first["reason"]) == ("not_found", "uncertain")
        assert not first["replay_allowed"] and not first["continuation_allowed"]
        assert agent.call("recover") == first
        # not_found is not absence: the durable claim still forbids a first POST.
        assert refused(agent, "dispatch") == "INITIAL_DISPATCH_ALREADY_CLAIMED"
        assert [event["kind"] for event in agent.call("history")] == ["lookup", "lookup"]
        assert proxy.count("POST", CREATE_PATH) == 0
        assert proxy.count("POST", LOOKUP_PATH) == 2
        assert_no_effects(stack, before)
        stack.record("i09:killed_before_send", guarded_posts=0, lookups=2, recovered=first)
    finally:
        agent.close()


def event_rows(path: Path) -> list[tuple[int, bytes, str]]:
    with closing(sqlite3.connect(path)) as db:
        return db.execute(
            "SELECT sequence,body,seal FROM b_d17_events ORDER BY sequence"
        ).fetchall()


def flip_hex_byte(path: Path, seal: str) -> tuple[int, bytes]:
    """Change one hex character of a stored seal in the closed file, outside SQLite."""
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    data = path.read_bytes()
    assert data.count(seal.encode()) == 1
    index = data.index(seal.encode())
    flipped = b"1" if data[index : index + 1] == b"0" else b"0"
    path.write_bytes(data[:index] + flipped + data[index + 1 :])
    return index, data[index : index + 1]


def test_07bb_i10_one_tampered_seal_byte_fails_closed_after_restart(stack, proxy):
    before = stack.rpc("effects")
    data = agent_data(stack)
    crash_after_claim(stack, data)
    agent, _ = open_agent(stack, data, fresh=False)
    try:
        assert agent.call("recover")["result"] == "not_found"
        history = agent.call("history")
    finally:
        agent.close()
    applied = Path(data["directory"]) / "applied.sqlite"
    with closing(sqlite3.connect(applied)) as db:
        layout = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
    rows = event_rows(applied)
    index, original = flip_hex_byte(applied, rows[0][2])
    with closing(sqlite3.connect(applied)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall() == (
            layout
        )
    agent, _ = open_agent(stack, data, fresh=False)
    try:
        # The read-only lookup may run; its answer is never appended to a broken chain.
        assert refused(agent, "recover") == "D17_HISTORY_CONFLICT"
        assert refused(agent, "history") == "D17_HISTORY_CONFLICT"
        assert refused(agent, "dispatch") == "INITIAL_DISPATCH_ALREADY_CLAIMED"
    finally:
        agent.close()
    assert [row[0] for row in event_rows(applied)] == [1]
    assert proxy.count("POST", CREATE_PATH) == 0
    assert proxy.count("POST", LOOKUP_PATH) == 2
    assert_no_effects(stack, before)
    # Exactly the altered byte was the reason; nothing else was written meanwhile.
    data_bytes = applied.read_bytes()
    applied.write_bytes(data_bytes[:index] + original + data_bytes[index + 1 :])
    agent, _ = open_agent(stack, data, fresh=False)
    try:
        assert agent.call("history") == history
    finally:
        agent.close()
    stack.record(
        "i10:tampered_seal",
        bytes_changed=1,
        schema_unchanged=True,
        recovery="D17_HISTORY_CONFLICT",
        guarded_posts=0,
        lookups=2,
        history=json.loads(json.dumps(history)),
    )
