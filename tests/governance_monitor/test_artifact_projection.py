"""Safe artifact checks only; no harness, merchant, databases or model execution."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "monitor_projection", ROOT / "scripts/export_governance_monitor.py"
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
SOURCES = ROOT / "docs/governance-monitor/sources"


class ArtifactProjectionTests(unittest.TestCase):
    def test_selected_result_is_bound_to_original_65_entry_ledger(self):
        result = json.loads((SOURCES / "hnp-false-accept-selected.json").read_text())
        self.assertEqual(module.verify_hnp([result], SOURCES / "hnp-evidence.jsonl"), 65)
        self.assertEqual(result["record"]["assessment"]["verdict"], "accept")
        self.assertEqual(result["effective_admission"]["code"], "BACKEND_DENIED")
        self.assertFalse(result["effect_occurred"])

    def test_result_tamper_is_not_accepted_even_with_original_ledger(self):
        result = json.loads((SOURCES / "hnp-false-accept-selected.json").read_text())
        result["record"]["assessment"]["verdict"] = "reject"
        with self.assertRaisesRegex(ValueError, "not bound"):
            module.verify_hnp([result], SOURCES / "hnp-evidence.jsonl")

    def test_corrupt_ledger_fails_at_file_pin_before_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "changed.jsonl"
            ledger.write_bytes((SOURCES / "hnp-evidence.jsonl").read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                module.verify_hnp([], ledger)

    def test_historical_hp_source_is_pinned(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "changed.json"
            source.write_bytes(b"{}")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                module.read_pinned(source, "agent-received.json")

    def test_versioned_sources_reproduce_the_bundled_v3_projection(self):
        values = {
            name: module.read_pinned(SOURCES / name, name)
            for name in ("agent-received.json", "agent-lost.json", "http-deny.json")
        }
        selected = json.loads((SOURCES / "hnp-false-accept-selected.json").read_text())
        entries = module.verify_hnp([selected], SOURCES / "hnp-evidence.jsonl")
        runs = sorted((SOURCES / "hnp02").iterdir())
        experiments = [module.hnp02(module.read_hnp02_run(run)) for run in runs]
        output = module.render(module.project(values, selected, entries, experiments))
        self.assertEqual(output, module.DATA.read_bytes())
        self.assertEqual(json.loads(output)["schema"], "governance-monitor/3")
        module.export_versioned(runs, verify_only=True)

    def test_provenance_pins_the_bundled_projection(self):
        provenance = json.loads((SOURCES.parent / "provenance.json").read_text())
        digest = hashlib.sha256(module.DATA.read_bytes()).hexdigest()
        self.assertEqual(provenance["projection_sha256"], digest)

    def test_hp_label_claims_permission_only_from_an_explicit_allow(self):
        value = module.read_pinned(SOURCES / "agent-received.json", "agent-received.json")
        projected = module.hp(value)
        self.assertIsNone(projected["enforce"]["outcome"])
        self.assertEqual(projected["label"], "HP · creación observada")
        self.assertIn("permitido", module.hp_label("allow", False))
        for outcome in (None, "deny"):
            self.assertNotIn("permitido", module.hp_label(outcome, False))

    def test_projection_does_not_execute_network(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            value = module.read_pinned(SOURCES / "http-deny.json", "http-deny.json")
            projected = module.denied(value)
            self.assertEqual(projected["execution"], "blocked")
            self.assertEqual(projected["commercialPosts"], 0)


if __name__ == "__main__":
    unittest.main()
