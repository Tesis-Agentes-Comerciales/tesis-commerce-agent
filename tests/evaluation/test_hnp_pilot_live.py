"""The live HNP pilot's pieces that run without services, and the governance pilot it reuses.

The HNP runner reuses the governance pilot's stack (`services_environment`, `prepare_services`,
`running_services`). Exporting them must not change the governance pilot: its dry runs stay the
ones pinned before the refactor.
"""

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation import governance_pilot, hnp_pilot_live, verify_evidence
from commerce_evaluation.governance_pilot import RepositoryProvenance, services_environment
from commerce_evaluation.hnp_pilot import prepare, stack_variables
from commerce_evaluation.hnp_pilot_live import (
    CliMerchant,
    JwksPace,
    UvicornAgent,
    summary_document,
    verify_task,
)
from commerce_evaluation.hnp_pilot_scoring import CostMeter
from commerce_evaluation.paths import DATA

# sha256 of each dry run, without the agent's commit and worktree state, taken before HNP-E2.
GOVERNANCE_DRY_RUNS = {
    "v1": (
        [],
        "a183f08914c541a8f4ee5683e06ec45739d5048e28ddbd71a73c015f32c7552f",
    ),
    "v2": (
        [
            "--manifest",
            str(DATA / "b2-pilot-v2.json"),
            "--condition",
            "B0prime",
            "--condition",
            "B2",
        ],
        "6093e415711181e9d473db8868587c20ca74a16686766306f41924bd5eb4be54",
    ),
}


@pytest.mark.parametrize("version", sorted(GOVERNANCE_DRY_RUNS))
def test_the_governance_pilot_dry_run_does_not_change(
    version: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments, digest = GOVERNANCE_DRY_RUNS[version]
    monkeypatch.setattr(
        governance_pilot, "repository_provenance", lambda _: RepositoryProvenance("a" * 40, True)
    )
    monkeypatch.setattr(sys, "argv", ["governance-pilot", "--dry-run", *arguments])
    assert governance_pilot.main() == 0
    document = json.loads(capsys.readouterr().out)
    for name in ("agent_commit", "agent_worktree_dirty"):
        document.pop(name)
    base = document.get("integration_readiness", {}).get("agent_base")
    if base is not None:
        base.pop("ref_commit")
        base.pop("ready")
    canonical = json.dumps(document, sort_keys=True, ensure_ascii=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == digest


def test_the_stack_s_secrets_are_generated_per_invocation() -> None:
    first = services_environment(merchant_port=1, payment_port=2, agent_port=3, auth_port=4)
    second = services_environment(merchant_port=1, payment_port=2, agent_port=3, auth_port=4)
    for name in governance_pilot.SERVICE_SECRETS:
        assert first[name] and first[name] != second[name], name
    assert first["MERCHANT_BASE_URL"] == "http://127.0.0.1:1"
    assert first["WEBHOOK_RECEIVER_URL"] == "http://127.0.0.1:3"
    assert first["BETTER_AUTH_URL"] == "http://127.0.0.1:4"


def test_the_merchant_cli_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def run_json(command: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
        commands.append(command)
        return {"run_id": "run-1", "session_token": "session"}

    monkeypatch.setattr(hnp_pilot_live, "_run_json", run_json)
    merchant = CliMerchant(tmp_path, {}, "user-1")
    assert merchant.seed("B2") == "run-1"
    merchant.export_feed("run-1", tmp_path / "feed")
    assert merchant.issue_session("run-1") == "session"
    merchant.update_offer("run-1", "BBGooglePixel8-128GB", 170000000)
    assert commands[0][-6:] == [
        "commerce_lab.db",
        "seed",
        "--actor-id",
        "user-1",
        "--variant",
        "B2",
    ]
    assert commands[1][-4:] == ["--output-dir", str(tmp_path / "feed"), "--run-id", "run-1"]
    assert commands[2][-5:] == ["issue-session", "run-1", "user-1", "--ttl-seconds", "3600"]
    assert commands[3][-6:] == [
        "--run-id",
        "run-1",
        "--offer-id",
        "BBGooglePixel8-128GB",
        "--price-minor",
        "170000000",
    ]


def test_the_summary_adds_up_the_cost_and_its_margin(tmp_path: Path) -> None:
    preparation = prepare(
        DATA / "hnp-pilot-v1.json",
        scenario_ids=["HN10"],
        smoke=True,
        stack=stack_variables(merchant_origin="http://m", auth_origin="http://a"),
        environ={"HNP_PILOT_MAX_COST_USD": "1"},
    )
    meter = CostMeter.from_manifest(preparation.manifest)
    rows = [
        {
            "scenario_id": "HN10",
            "condition": "B2-HNP",
            "layer": "B",
            "cost": meter.charge(
                {"rows": [{"input_tokens": 4000, "output_tokens": 1000}, {"input_tokens": None}]}
            ),
            "metrics": {"model_calls": 2},
        },
        {"scenario_id": "HN10", "condition": "B0-HNP", "layer": "B", "harness_error": "x"},
    ]
    summary = summary_document(
        rows,
        stop={"stopped_by": None},
        preparation=preparation,
        run_metadata={"invocation_id": "hnp-pilot-test"},
        output=tmp_path / "results.jsonl",
        meter=meter,
    )
    cost = summary["cost"]
    assert (cost["model_calls"], cost["unpriced_calls"], cost["rows_without_cost"]) == (2, 1, 1)
    # The row without tokens is the call and its retry: two units of USD 0.01.
    assert (cost["margin_units"], cost["margin_usd"], cost["cap_usd"]) == (2, 0.02, 1.0)
    assert cost["charged_usd"] == pytest.approx(0.02 + 0.003)
    assert summary["episodes_run"] == 2 and "E4" in summary["p_arm"]
    assert summary["summary"]["harness_errors"] == 1


def test_the_episode_s_chain_is_hnp_v1_s_document_of_its_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    def verify_database(database: Path, **options: Any) -> list[dict[str, Any]]:
        calls.append({"database": database, **options})
        return [
            {"purchase_mode": "HP", "task_id": None},
            {"purchase_mode": "HNP", "task_id": "ht_other"},
            {"purchase_mode": "HNP", "task_id": "ht_1", "links": {}},
        ]

    monkeypatch.setattr(verify_evidence, "verify_database", verify_database)
    agent_db, psp_db = tmp_path / "agent.db", tmp_path / "payments.db"
    document = verify_task(agent_db, grant_id="dg_1", task_id="ht_1", psp_database=psp_db)
    assert document == {"purchase_mode": "HNP", "task_id": "ht_1", "links": {}}
    assert calls == [{"database": agent_db, "psp_database": psp_db, "grant_id": "dg_1"}]
    assert verify_task(agent_db, grant_id="dg_1", task_id="ht_9", psp_database=psp_db) is None


def test_the_agent_s_log_tail_hides_what_it_was_started_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HP's AGENT_SECRETS, the merchant session among them (it is issued per episode)."""
    monkeypatch.setattr(hnp_pilot_live.subprocess, "Popen", lambda *args, **kwargs: object())
    monkeypatch.setattr(hnp_pilot_live, "_wait_http", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        hnp_pilot_live,
        "_request_json",
        lambda *args, **kwargs: governance_pilot.HttpResult(200, {"status": "ok"}, 1),
    )
    with (tmp_path / "agent.log").open("w", encoding="utf-8") as log:
        agent = UvicornAgent(port=1, log=log, factory="f", pace=JwksPace())
        agent.start({"MERCHANT_BEARER_TOKEN": "session-1", "PATH": "/bin"})
        agent.start({"MERCHANT_BEARER_TOKEN": "session-1", "AGENT_D1_HMAC_KEY_HEX": "ab"})
    assert agent.secrets == {"session-1", "ab"}
    (tmp_path / "agent.log").write_text("token session-1 in a traceback\n", encoding="utf-8")
    tail = governance_pilot._tail(tmp_path / "agent.log", tuple(agent.secrets))
    assert "session-1" not in tail


def test_every_so_many_agent_starts_the_harness_lets_auth_s_window_lapse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each agent process fetches /api/auth/jwks once; Better Auth refuses a path's 101st request
    while they keep coming less than 60 s apart, so the pace waits 61 s with no agent running."""
    monkeypatch.setattr(hnp_pilot_live.subprocess, "Popen", lambda *args, **kwargs: object())
    monkeypatch.setattr(hnp_pilot_live, "_wait_http", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        hnp_pilot_live,
        "_request_json",
        lambda *args, **kwargs: governance_pilot.HttpResult(200, {"status": "ok"}, 1),
    )
    waits: list[float] = []
    pace = JwksPace(every=2, sleep=waits.append)
    with (tmp_path / "agent.log").open("w", encoding="utf-8") as log:
        for _ in range(5):
            UvicornAgent(port=1, log=log, factory="f", pace=pace).start({})

    assert waits == [61.0, 61.0]
    assert JwksPace().every < 100
