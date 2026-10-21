"""One HNP pilot episode with doubles of the agent API, the merchant, the agent process and the
buyer; and the readings after the stop on a real agent.db (hnp_pilot_episode).

The agent API double answers `POST /internal/hnp/tasks/{id}/run` (HNP-09b) as spec §5.2 says
(202 running, 200 terminal, 409 `hnp_task_not_runnable`). The readings run the real executor and
dispatch over the merchant's and the PSP's doubles.
"""

import json
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.governance.evidence import ApplicationFailure, DecisionRecord
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.stage_timings_sqlite import ModelUsage, SqliteStageTimings
from commerce_evaluation import verify_evidence
from commerce_evaluation.governance_pilot import ConditionMismatchError, HttpResult
from commerce_evaluation.hnp_pilot import load_pilot
from commerce_evaluation.hnp_pilot_agent import scripted_selector
from commerce_evaluation.hnp_pilot_episode import (
    AgentApi,
    EpisodeRun,
    EpisodeTimeout,
    SeededEpisodeError,
    observe,
    proposal_body,
    read_governance,
    read_ledger,
    read_model_usage,
    run_episode,
    time_to_effect_ms,
    token_occurrences,
    trigger_label,
    verify_health,
)
from commerce_evaluation.hnp_pilot_scoring import chain_vector_match
from tests.delegation.test_hnp_runtime import (
    consented,
    feed_with_another_candidate,
    governed_hnp,
    runtime,
)
from tests.governance.test_stale_terms_pending import Ap2Merchant
from tests.support.hnp_dispatch import HnpMerchant, Psp
from tests.test_delegation_api import MERCHANT, hnp_settings

_, MATRIX = load_pilot()
SCENARIOS = {scenario["id"]: scenario for scenario in MATRIX["scenarios"]}
SERVICE, OPERATOR = "service-" + "s" * 40, "operator-" + "o" * 40
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
TERMINAL = {"purchased", "escalated", "failed", "canceled", "uncertain"}


def stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class FakeAgentApi:
    """The agent's delegation routes as spec §5 describes them, in memory."""

    def __init__(self, final: tuple[str, str | None] = ("purchased", None), polls: int = 2):
        self.final, self.polls = final, polls
        self.state, self.outcome = "scheduled", None
        self.calls: list[dict[str, Any]] = []
        self.kill_switch = False
        self.lock = threading.Lock()
        self.escalation: dict[str, Any] | None = None
        self.run_status: int | None = None

    def view(self) -> dict[str, Any]:
        return {"task_id": "ht_1", "state": self.state, "outcome_code": self.outcome}

    def send(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        bearer: str | None = None,
        **options: Any,
    ) -> HttpResult:
        assert not options, "the runner never sends other options (no Origin header)"
        path = url.removeprefix("http://agent")
        self.calls.append({"method": method, "path": path, "body": body, "bearer": bearer})
        internal = path.startswith("/internal/")
        if internal:
            expected = OPERATOR if path.endswith("kill-switch") else SERVICE
            if bearer != expected:
                return HttpResult(401, {"code": "hnp_unauthorized"}, 1)
        else:
            assert bearer is not None and bearer.startswith("buyer.")
        with self.lock:
            return self._route(method, path, body)

    def _route(self, method: str, path: str, body: dict[str, Any] | None) -> HttpResult:
        if path == "/delegations/proposals":
            return HttpResult(
                201, {"proposal_id": "dpr_1", "scope_digest": "sha256:" + "a" * 64}, 1
            )
        if path == "/delegations":
            assert body == {"proposal_id": "dpr_1", "scope_digest": "sha256:" + "a" * 64}
            consented_at = NOW
            return HttpResult(
                201,
                {
                    "grant_id": "dg_1",
                    "task": self.view(),
                    "consented_at": stamp(consented_at),
                    "expires_at": stamp(consented_at + timedelta(seconds=60)),
                },
                1,
            )
        if path == "/delegations/dg_1/revoke":
            self.state, self.outcome = "canceled", "revoked_by_owner"
            return HttpResult(200, {"state": "revoked", "effect": "no_effect"}, 1)
        if path == "/delegations/dg_1":
            return HttpResult(200, {"grant_id": "dg_1", "escalation": self.escalation}, 1)
        if path == "/internal/hnp/kill-switch":
            self.kill_switch = bool((body or {})["enabled"])
            return HttpResult(200, {"enabled": self.kill_switch, "control_epoch": 2}, 1)
        if path == "/internal/hnp/tasks/ht_1/run":
            if self.run_status is not None:
                return HttpResult(self.run_status, {"code": "hnp_task_not_found"}, 1)
            if self.state != "scheduled":
                return HttpResult(409, {"code": "hnp_task_not_runnable"}, 1)
            if self.kill_switch:
                self.state, self.outcome = "canceled", "kill_switch"
                return HttpResult(200, self.view(), 1)
            if self.final == ("canceled", "grant_expired"):
                self.state, self.outcome = self.final
                return HttpResult(200, self.view(), 1)
            self.state = "running"
            return HttpResult(202, self.view(), 1)
        if path == "/internal/hnp/tasks/ht_1":
            if self.state == "running":
                self.polls -= 1
                if self.polls <= 0:
                    self.state, self.outcome = self.final
            return HttpResult(200, self.view() | {"message": None}, 1)
        raise AssertionError(f"unexpected {method} {path}")


class FakeMerchant:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def seed(self, label: str) -> str:
        self.calls.append(("seed", label))
        return "run-1"

    def export_feed(self, run_id: str, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.calls.append(("export_feed", run_id))

    def issue_session(self, run_id: str) -> str:
        return "merchant-session"

    def update_offer(self, run_id: str, variant_id: str, price_minor: int) -> dict[str, Any]:
        self.calls.append(("update_offer", variant_id, str(price_minor)))
        return {"offer_id": variant_id, "price_minor": price_minor}

    def run_checkouts(self, run_id: str) -> dict[str, Any]:
        return {
            "count": 1,
            "by_status": {"completed": 1},
            "order_count": 1,
            "checkouts": [{"checkout_id": "co_1", "status": "completed", "offer_id": "SON-01"}],
        }

    def dispatch_webhooks(self) -> dict[str, Any]:
        self.calls.append(("dispatch_webhooks",))
        return {"count": 1, "by_outcome": {"delivered": 1}}


HEALTHY = {
    "status": "ok",
    "governance_mode": "enforce",
    "hp_flow": "governed",
    "hnp_enabled": True,
    "hnp_kill_switch": False,
    "hnp_active_grants": 0,
    "hnp_running_tasks": 0,
    "hnp_uncertain_tasks": 0,
}


class FakeAgent:
    def __init__(self, health: dict[str, Any] = HEALTHY) -> None:
        self.health = health
        self.starts: list[dict[str, str]] = []
        self.stops = 0

    def start(self, variables: Any) -> dict[str, Any]:
        self.starts.append(dict(variables))
        return dict(self.health)

    def stop(self) -> None:
        self.stops += 1


class FakeBuyer:
    def __init__(self) -> None:
        self.issued: list[str] = []

    def session_token(self) -> str:
        token = f"buyer.payload.signature{len(self.issued) + 1}"
        self.issued.append(token)
        return token


def episode(
    scenario_id: str,
    tmp_path: Path,
    *,
    fake: FakeAgentApi | None = None,
    agent: FakeAgent | None = None,
    condition: str = "B2-HNP",
    sleeps: list[float] | None = None,
    poll_timeout: float = 30,
) -> tuple[EpisodeRun, FakeAgentApi, FakeMerchant, FakeAgent, FakeBuyer]:
    fake = fake or FakeAgentApi()
    merchant, agent, buyer = FakeMerchant(), agent or FakeAgent(), FakeBuyer()
    api = AgentApi("http://agent", service_token=SERVICE, operator_token=OPERATOR, send=fake.send)
    run = run_episode(
        SCENARIOS[scenario_id],
        condition,
        api=api,
        buyer=buyer,
        merchant=merchant,
        agent=agent,
        variables={"AGENT_HNP_ENABLED": "true"},
        run_dir=tmp_path / "run",
        poll_timeout_seconds=poll_timeout,
        poll_interval_seconds=0,
        sleep=(sleeps.append if sleeps is not None else lambda seconds: None),
        now=lambda: NOW,
    )
    return run, fake, merchant, agent, buyer


def paths(fake: FakeAgentApi) -> list[str]:
    return [f"{call['method']} {call['path']}" for call in fake.calls]


# The flow ----------------------------------------------------------------------------------------


def test_a_purchase_consents_triggers_polls_and_reads_the_buyer_view(tmp_path: Path) -> None:
    run, fake, merchant, agent, buyer = episode("HN01", tmp_path)
    assert paths(fake) == [
        "POST /delegations/proposals",
        "POST /delegations",
        "POST /internal/hnp/tasks/ht_1/run",
        "GET /internal/hnp/tasks/ht_1",
        "GET /internal/hnp/tasks/ht_1",
        "GET /delegations/dg_1",
    ]
    assert fake.calls[0]["body"] == proposal_body(SCENARIOS["HN01"])
    assert fake.calls[0]["body"]["details"]["fulfillment"]["address"]["postal_code"] == "110111"
    # The consent JWT serves the proposal and the consent only; a new session reads the view.
    assert fake.calls[0]["bearer"] == fake.calls[1]["bearer"] == buyer.issued[0]
    assert fake.calls[-1]["bearer"] == buyer.issued[1] != buyer.issued[0]
    assert run.token_probe == "signature1"
    assert (run.grant_id, run.task_id, run.trigger) == (
        "dg_1",
        "ht_1",
        [{"status": 202, "code": None, "label": "202"}],
    )
    assert run.task_view is not None and run.task_view["state"] == "purchased"
    assert ("dispatch_webhooks",) in merchant.calls and run.webhooks is not None
    assert merchant.calls[0] == ("seed", "B2")
    assert agent.stops == 1 and len(agent.starts) == 1
    variables = agent.starts[0]
    assert variables["AGENT_DATABASE_PATH"] == str(tmp_path / "run" / "agent.db")
    assert variables["AGENT_FEED_PATH"] == str(tmp_path / "run" / "feed")
    assert variables["MERCHANT_BEARER_TOKEN"] == "merchant-session"
    assert run.health[0]["hnp_enabled"] is True


def test_the_owner_revokes_with_a_new_session_and_the_trigger_is_refused(tmp_path: Path) -> None:
    run, fake, _, _, buyer = episode("HN05", tmp_path, fake=FakeAgentApi(("canceled", None)))
    revoke = next(call for call in fake.calls if call["path"].endswith("/revoke"))
    assert revoke["bearer"] == buyer.issued[1] != buyer.issued[0]
    assert run.revoke_receipt == {"state": "revoked", "effect": "no_effect"}
    assert [item["label"] for item in run.trigger] == ["409 hnp_task_not_runnable"]
    assert run.task_view is not None
    assert (run.task_view["state"], run.task_view["outcome_code"]) == (
        "canceled",
        "revoked_by_owner",
    )


def test_the_operator_s_kill_switch_ends_the_task_at_the_claim(tmp_path: Path) -> None:
    run, fake, merchant, _, _ = episode("HN06", tmp_path)
    switch = next(call for call in fake.calls if call["path"] == "/internal/hnp/kill-switch")
    assert switch["bearer"] == OPERATOR and switch["body"] == {"enabled": True}
    trigger = next(call for call in fake.calls if call["path"].endswith("/run"))
    assert trigger["bearer"] == SERVICE and trigger["body"] is None
    assert [item["label"] for item in run.trigger] == ["200_terminal"]
    assert run.task_view is not None and run.task_view["outcome_code"] == "kill_switch"
    assert ("dispatch_webhooks",) not in merchant.calls


def test_two_concurrent_triggers_get_one_run(tmp_path: Path) -> None:
    run, fake, _, _, _ = episode("HN07", tmp_path)
    assert sorted(item["label"] for item in run.trigger) == ["202", "409 hnp_task_not_runnable"]
    assert sum(call["path"].endswith("/run") for call in fake.calls) == 2


def test_the_runner_waits_past_the_grant_s_expiry_on_the_real_clock(tmp_path: Path) -> None:
    sleeps: list[float] = []
    run, _, _, _, _ = episode(
        "HN04", tmp_path, fake=FakeAgentApi(("canceled", "grant_expired")), sleeps=sleeps
    )
    assert sleeps[0] == 65.0  # 60 s of validity plus the 5 s margin, from the consent
    assert [item["label"] for item in run.trigger] == ["200_terminal"]
    assert run.stimuli[-1] == {"action": "wait_until_grant_expired", "waited_seconds": 65.0}


def test_hn03_changes_the_offer_re_exports_and_restarts_on_the_same_database(
    tmp_path: Path,
) -> None:
    run, _, merchant, agent, _ = episode("HN03", tmp_path)
    assert ("update_offer", "BBGooglePixel8-128GB", "170000000") in merchant.calls
    assert merchant.calls.count(("export_feed", "run-1")) == 2
    assert len(agent.starts) == 2 and agent.starts[0] == agent.starts[1]
    assert agent.stops == 2 and len(run.health) == 2


def test_hn02_changes_the_offer_without_a_restart(tmp_path: Path) -> None:
    _, _, merchant, agent, _ = episode("HN02", tmp_path)
    assert ("update_offer", "BBGooglePixel8-128GB", "156000000") in merchant.calls
    assert merchant.calls.count(("export_feed", "run-1")) == 1 and len(agent.starts) == 1


def test_a_task_still_running_at_the_timeout_is_a_harness_error(tmp_path: Path) -> None:
    with pytest.raises(SeededEpisodeError) as raised:
        episode("HN01", tmp_path, fake=FakeAgentApi(polls=10**6), poll_timeout=0)
    assert isinstance(raised.value.cause, EpisodeTimeout)
    assert raised.value.run.merchant_run_id == "run-1"
    assert raised.value.run.grant_id == "dg_1"


def test_an_unexpected_trigger_answer_is_a_harness_error(tmp_path: Path) -> None:
    fake = FakeAgentApi()
    fake.run_status = 404
    agent = FakeAgent()
    with pytest.raises(SeededEpisodeError, match="404 hnp_task_not_found"):
        episode("HN01", tmp_path, fake=fake, agent=agent)
    assert agent.stops == 1


@pytest.mark.parametrize(
    "change",
    [
        {"hnp_kill_switch": True},
        {"hnp_enabled": None},
        {"governance_mode": "off"},
        {"hnp_running_tasks": None},
    ],
)
def test_another_condition_aborts_the_invocation(tmp_path: Path, change: dict[str, Any]) -> None:
    agent = FakeAgent(HEALTHY | change)
    with pytest.raises(ConditionMismatchError):
        episode("HN01", tmp_path, agent=agent)
    assert agent.stops == 1


def test_b0_hnp_runs_with_governance_off(tmp_path: Path) -> None:
    agent = FakeAgent(HEALTHY | {"governance_mode": "off", "hp_flow": "blocked"})
    run, _, merchant, _, _ = episode("HN01", tmp_path, agent=agent, condition="B0-HNP")
    assert merchant.calls[0] == ("seed", "B0") and run.health[0]["governance_mode"] == "off"
    assert verify_health("B0-HNP", agent.health)["hp_flow"] == "blocked"


def test_trigger_labels_follow_the_oracle() -> None:
    assert trigger_label(HttpResult(202, {}, 1)) == "202"
    assert trigger_label(HttpResult(200, {}, 1)) == "200_terminal"
    assert trigger_label(HttpResult(409, {"code": "hnp_task_not_runnable"}, 1)) == (
        "409 hnp_task_not_runnable"
    )


# Readings after the stop, on a real agent.db -----------------------------------------------------


@pytest.fixture
def purchased_b2(tmp_path: Path) -> tuple[Path, str, str]:
    """B2-HNP run to its purchase by the real executor and dispatch (HNP-09b), over the merchant's
    and the PSP's doubles."""
    built = runtime(governed_hnp(tmp_path), HnpMerchant(MERCHANT), Psp())
    try:
        assert built.hnp_executor is not None
        step = built.hnp_executor.run(consented(built))
    finally:
        built.close()
    assert step.execution is not None and step.execution.task.run_id is not None
    return tmp_path / "agent.db", step.execution.grant.grant_id, step.execution.task.run_id


def test_the_ledger_and_the_decisions_of_a_b2_run(purchased_b2: tuple[Path, str, str]) -> None:
    database, grant_id, run_id = purchased_b2
    ledger = read_ledger(database, grant_id)
    assert ledger["task"]["run_id"] == run_id and ledger["task"]["state"] == "purchased"
    assert ledger["grant"]["state"] == "consumed"
    assert [event["event_type"] for event in ledger["events"]] == [
        "consented",
        "mandates_issued",
        "task_claimed",
        "reservation_reserved",
        "mandates_closed",
        "dispatch_committed",
        "receipt_recorded",
        "receipt_recorded",
        "consumed",
        "task_finished",
    ]
    governance = read_governance(database, run_id)
    assert governance["governance"] == {
        "D1": "allow",
        "D2": "allow",
        "D3": "allow",
        "freshness_close": "fresh",
        "deny": None,
    }
    assert {point: len(values) for point, values in governance["policy_latency_ms"].items()} == {
        "D1": 1,
        "D2": 1,
        "D3": 1,
    }
    # Every decision names its owner and, D1-HNP included, the executor as its principal.
    assert governance["attribution"] == {
        "rows": 3,
        "owner_ref": 3,
        "principal_ref": 3,
        "complete": 3,
    }
    assert read_governance(database, None)["governance"]["D1"] == "not_reached"


def test_a_failure_row_leaves_the_attribution_of_the_decisions(
    purchased_b2: tuple[Path, str, str],
) -> None:
    """Outside a decision_record, governance_evidence keeps canonical_input NULL (its CHECK): the
    attribution counts the decision records only (manifest decisions_pending 13)."""
    database, _, run_id = purchased_b2
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        store = SqliteGovernanceEvidenceStore(connection)
        *_, d3 = store.for_run(run_id)
        assert isinstance(d3, DecisionRecord)
        data, version = d3.any_input(), d3.application.expected_state_version
        failure = ApplicationFailure(
            schema_version=d3.schema_version,
            record_kind="application_failure",
            evidence_id="application-failed",
            sequence=d3.sequence + 1,
            recorded_at=d3.recorded_at,
            run_id=run_id,
            trace_id=data.trace_id,
            decision_id=data.decision_id,
            reason_code="GOV_STATE_CONFLICT",
            expected_state_version=version,
            observed_state_version=None,
            replayability="not_evaluable",
        )
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            store.record_decision(failure, expected_state_version=version)
        kinds = "SELECT record_kind, canonical_input FROM governance_evidence ORDER BY sequence"
        assert connection.execute(kinds).fetchall()[-1] == ("application_failure", None)
    governance = read_governance(database, run_id)
    assert governance["governance"]["D3"] == "application_failure"
    assert governance["attribution"] == {
        "rows": 3,
        "owner_ref": 3,
        "principal_ref": 3,
        "complete": 3,
    }


# HN11 (K12, spec §3.3): the scripted variant is in the feed and inside the total but outside the
# grant; SON-02 stands here for the registered OnePlus 11.


@pytest.mark.parametrize("condition", ["B0-HNP", "B2-HNP"])
def test_hn11_ends_as_its_oracle_says(tmp_path: Path, condition: str) -> None:
    """B2-HNP: the choice passes `start` and D1-HNP denies it with POL-01 before any POST.
    B0-HNP: the shortlist guard ends the task before `start`. Same terminal, 0 checkouts."""
    base = governed_hnp(tmp_path) if condition == "B2-HNP" else hnp_settings(tmp_path)
    settings = base.model_copy(update={"agent_feed_path": feed_with_another_candidate(tmp_path)})
    merchant = Ap2Merchant(MERCHANT)
    with scripted_selector("SON-02"):
        built = runtime(settings, merchant)
    try:
        assert built.hnp_executor is not None
        step = built.hnp_executor.run(consented(built))
    finally:
        built.close()
    assert step.execution is not None
    task, grant_id = step.execution.task, step.execution.grant.grant_id
    oracle, database = SCENARIOS["HN11"]["oracle"][condition], tmp_path / "agent.db"
    grant = read_ledger(database, grant_id)["grant"]
    assert {"state": task.state, "outcome_code": task.outcome_code} == oracle["task"]
    assert {"state": grant["state"], "revoked_reason": grant["revoked_reason"]} == oracle["grant"]
    assert merchant.creations() == oracle["effects"]["merchant_checkouts"] == 0
    governance = read_governance(database, task.run_id)["governance"]
    [chain] = [
        document
        for document in verify_evidence.verify_database(database, grant_id=grant_id)
        if document.get("purchase_mode") == "HNP"
    ]
    if condition == "B2-HNP":
        assert governance == oracle["governance"]
        assert chain_vector_match(oracle, chain) == 1
        return
    assert oracle["governance"] is None and governance["D1"] == "not_reached"
    # Reported, the manifest unchanged: the registered B0-HNP vector has intent and d1_decision
    # missing, but HNP-V1 marks both not_applicable when the task ends before `start`.
    links = chain["links"]
    assert (links["intent"]["status"], links["d1_decision"]["status"]) == ("not_applicable",) * 2
    assert chain_vector_match(oracle, chain) == 0


def test_every_model_row_counts_and_unknown_tokens_stay_unknown(
    purchased_b2: tuple[Path, str, str],
) -> None:
    database, _, _ = purchased_b2
    assert read_model_usage(database) == {
        "calls": 0,
        "rows": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "calls_without_tokens": 0,
    }
    timings = SqliteStageTimings(database)
    usage = ModelUsage()
    with timings.measure("model.hnp_select", usage):
        usage.input_tokens, usage.output_tokens = 1200, 300
    with pytest.raises(TimeoutError), timings.measure("model.hnp_select", ModelUsage()):
        raise TimeoutError
    with timings.measure("model.route", ModelUsage(input_tokens=10, output_tokens=5)):
        pass
    read = read_model_usage(database)
    assert read is not None
    assert (read["calls"], read["calls_without_tokens"]) == (3, 1)
    assert (read["input_tokens"], read["output_tokens"]) == (1210, 305)
    assert [row["outcome"] for row in read["rows"]] == ["ok", "error", "ok"]
    # The cost margin tells a retry after a timeout by the row's elapsed time.
    assert all(isinstance(row["elapsed_ms"], float) for row in read["rows"])


def test_the_consent_token_is_searched_in_the_agent_s_files(tmp_path: Path) -> None:
    (tmp_path / "agent.log").write_text("x signature1 y signature1", encoding="utf-8")
    (tmp_path / "agent.db").write_bytes(b"\x00signature1")
    (tmp_path / "feed").mkdir()
    (tmp_path / "feed" / "agent-notes").write_text("signature1", encoding="utf-8")
    assert token_occurrences(tmp_path, "signature1") == 3
    assert token_occurrences(tmp_path, "signature2") == 0
    assert token_occurrences(tmp_path, None) is None


def test_time_to_effect_runs_from_the_claim_to_the_end() -> None:
    events = [
        {"event_type": "consented", "recorded_at": "2026-10-21T15:00:00.000Z"},
        {"event_type": "task_claimed", "recorded_at": "2026-10-21T15:00:01.000Z"},
        {"event_type": "task_finished", "recorded_at": "2026-10-21T15:00:03.250Z"},
    ]
    assert time_to_effect_ms(events) == 2250
    assert time_to_effect_ms(events[:2]) is None


def test_observe_reads_the_effects_after_the_stop(
    purchased_b2: tuple[Path, str, str], tmp_path: Path
) -> None:
    database, grant_id, _ = purchased_b2
    psp = tmp_path / "payments.db"
    with closing(sqlite3.connect(psp)) as connection, connection:
        connection.execute("CREATE TABLE delegations (checkout_id TEXT, result_json TEXT)")
        connection.execute(
            "INSERT INTO delegations VALUES (?, ?)", ("co_1", json.dumps({"status": "approved"}))
        )
    run = EpisodeRun(
        merchant_run_id="run-1",
        grant_id=grant_id,
        task_id="ht_x",
        trigger=[{"status": 202, "code": None, "label": "202"}],
        task_view={"task_id": "ht_x", "state": "purchased", "outcome_code": None},
        delegation_view={"escalation": None},
        token_probe="never-written",
    )
    observed, errors = observe(
        "B2-HNP",
        run,
        run_dir=database.parent,
        psp_database=psp,
        merchant=FakeMerchant(),
        verifier=None,
    )
    assert errors == {"evidence_chain": "HNP-V1's verifier is not in this checkout"}
    assert observed["effects"] == {
        "merchant_checkouts": 1,
        "checkouts_canceled": 0,
        "agent_key_closures": 1,
        "chains_presented": 2,
        "psp_tokens": 1,
        "orders": 1,
    }
    assert observed["task"] == {"state": "purchased", "outcome_code": None}
    assert observed["grant"] == {"state": "consumed", "revoked_reason": None}
    assert observed["governance"]["D3"] == "allow" and observed["escalation_view"] is False
    # The dispatch (HNP-09b) keeps the merchant's completion snapshot: the destination is measured.
    assert observed["order"] == {
        "variant_id": "SON-01",
        "total_minor": 74000,
        "destination_matches_grant": True,
        "source": "merchant_snapshot",
    }
    assert observed["buyer_token_after_consent"] == 0
    assert observed["attribution"]["governance_evidence"]["rows"] == 3
    assert "evidence_chain" not in observed

    def verifier(database: Path, **refs: Any) -> dict[str, Any]:
        assert refs == {"grant_id": grant_id, "task_id": "ht_x", "psp_database": psp}
        return {"links": {"intent": "ok"}}

    observed, errors = observe(
        "B0-HNP",
        run,
        run_dir=database.parent,
        psp_database=psp,
        merchant=FakeMerchant(),
        verifier=verifier,
    )
    assert observed["evidence_chain"] == {"links": {"intent": "ok"}}
    assert observed["governance"] is None and errors == {}
