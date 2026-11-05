"""Same manual orchestrator, real services, only ModelServer errors; never inference."""

import hashlib
import json

from commerce_evaluation.exp06 import AttemptBudget
from commerce_evaluation.exp06_execution import run_stack
from tests.integration.exp01_support import ROOT, recorded_case
from tests.support.model_server import ModelServer, failure

PROTOCOL = ROOT / "evaluation/scenarios/exp06-runner-infrastructure.json"


def test_protected_manual_orchestrator_without_provider(tmp_path, monkeypatch):
    model = ModelServer(*(failure(503) for _ in range(6)))
    model.install(monkeypatch)
    budget = AttemptBudget(None, infrastructure_only=True)
    with recorded_case(
        tmp_path,
        "EXP06:runner",
        {
            "transport": "existing ModelServer errors only",
            "real_provider_calls": 0,
            "scenarios": ["UX01", "UX11", "UX12"],
        },
        protocol_path=PROTOCOL,
    ) as record:
        summary = run_stack(tmp_path / "manual", budget, "synthetic-mock-only")
        record["stimulus_verified"] = True
        facts = record["observations"]
        public = tmp_path / "manual/evidence"
        facts["runner_summary"] = summary
        facts["attempt_journal"] = [
            json.loads(line) for line in (public / "attempts.jsonl").read_text().splitlines()
        ]
        facts["frozen_inputs"] = {
            name: json.loads((public / (name + "-frozen.json")).read_text())
            for name in summary["cases"]
        }
        assert len(model.requests) == summary["attempts"] == 6
        assert all(body["service_tier"] == "default" for body in model.bodies())
        assert {row["case_id"] for row in facts["attempt_journal"]} == {"UX01", "UX11", "UX12"}
        assert summary["accounting"]["unknown_attempts"] == 6
        assert summary["accounting"]["reservation_reconciled"]
        assert summary["reserved_or_spent_usd"] == "2.670"
        assert summary["cleanup_verified"] and summary["private_removed"]
        for case in summary["cases"].values():
            assert case["status"] == "not_executed" and case["http_status"] == 502
            assert case["effects_verified_zero"] and case["ledger_complete"]
            assert len(case["model_requests"]) == 1 and case["model_requests"][0]["retries"] == 1
            assert case["psp_after_cleanup"] == case["psp_before"]
        assert max(row["frozen_at_ns"] for row in facts["frozen_inputs"].values()) < min(
            row["at_ns"]
            for row in facts["attempt_journal"]
            if row["phase"] == "reserved_before_send"
        )
        facts["public_hashes"] = {}
        for line in (public / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ", 1)
            assert hashlib.sha256((public / name).read_bytes()).hexdigest() == digest
            facts["public_hashes"][name] = digest
        record["cleanup_verified"] = summary["cleanup_verified"]
