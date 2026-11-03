"""Regressions of evidence ordering and the P2 access gate; no commercial observations."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.support.p2_case_evidence import InsufficientFixtureData, verified_case

ROOT = Path(__file__).resolve().parents[2]


class P2EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, scenario: str, **facts: Any) -> None:
        self.rows.append({"scenario": scenario, **facts})

    def test_success_is_emitted_only_after_verified_cleanup(self) -> None:
        facts = {"phase": "exercise", "cleanup_verified": False}
        with verified_case(self.record, "P2-PROV-02", facts):
            self.assertEqual(self.rows, [])
            facts["phase"] = "restoration"
            self.assertEqual(self.rows, [])
            facts.update(phase="verified", cleanup_verified=True)
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(self.rows[0]["status"], "passed")
        self.assertTrue(self.rows[0]["cleanup_verified"])

    def test_restore_or_cancel_failure_never_emits_passed(self) -> None:
        for case, phase in (("P2-PROV-02", "restoration"), ("P2-PROV-03", "cancellation")):
            self.rows.clear()
            facts = {"phase": "exercise", "cleanup_verified": False}
            with (
                self.subTest(case=case),
                self.assertRaises(AssertionError),
                verified_case(self.record, case, facts),
            ):
                # The exercise succeeds, but cleanup verification fails.
                facts["phase"] = phase
                raise AssertionError("synthetic cleanup failure")
            self.assertEqual([row["status"] for row in self.rows], ["failed"])
            self.assertEqual(self.rows[0]["phase"], phase)
            self.assertFalse(self.rows[0]["cleanup_verified"])
            self.assertNotIn("synthetic cleanup failure", str(self.rows))

    def test_verification_failure_after_cancel_response_is_still_failed(self) -> None:
        facts = {"phase": "verify_cancellation", "cleanup_verified": False}
        with self.assertRaises(AssertionError), verified_case(self.record, "P2-PROV-03", facts):
            # HTTP success alone does not prove the persisted cancellation/effects.
            facts["cancel_http"] = 200
            raise AssertionError()
        self.assertEqual(self.rows[0]["status"], "failed")

    def test_data_precondition_is_blocked_and_propagates(self) -> None:
        with (
            self.assertRaises(InsufficientFixtureData),
            verified_case(self.record, "P2-PROV-01", {"phase": "preconditions"}),
        ):
            raise InsufficientFixtureData("B", 0)
        self.assertEqual(self.rows[0]["status"], "blocked")
        self.assertEqual(self.rows[0]["failure_kind"], "data")
        self.assertEqual(self.rows[0]["reason"], "insufficient_fixture_data")
        self.assertEqual(self.rows[0]["matching_variants"], 0)

    def test_exercise_failure_is_not_erased_by_successful_cleanup(self) -> None:
        facts = {"phase": "exercise", "cleanup_verified": False}
        with self.assertRaises(RuntimeError), verified_case(self.record, "P2-PROV-03", facts):
            try:
                raise RuntimeError()
            finally:
                facts["cleanup_verified"] = True
        self.assertEqual(self.rows[0]["status"], "failed")
        self.assertTrue(self.rows[0]["cleanup_verified"])

    def test_missing_cleanup_verification_cannot_pass(self) -> None:
        with (
            self.assertRaises(AssertionError),
            verified_case(self.record, "P2-PROV-03", {"cleanup_verified": False}),
        ):
            pass
        self.assertEqual(self.rows[0]["status"], "failed")

    def test_evidence_write_error_propagates(self) -> None:
        def broken_record(*args: Any, **kwargs: Any) -> None:
            raise OSError()

        with (
            self.assertRaises(OSError),
            verified_case(broken_record, "P2-PROV-03", {"cleanup_verified": True}),
        ):
            pass

    def test_secret_value_is_only_passed_to_the_two_checkouts(self) -> None:
        text = (ROOT / ".github/workflows/nightly.yml").read_text(encoding="utf-8")
        self.assertEqual(text.count("token: $" + "{{ secrets.MERCHANT_REPO_TOKEN }}"), 2)
        self.assertNotIn("MERCHANT_REPO_TOKEN:", text)
        self.assertIn(
            "SIBLING_TOKEN_CONFIGURED: $" + "{{ secrets.MERCHANT_REPO_TOKEN != '' }}", text
        )
        self.assertNotIn("pull_request_target", text)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "requires POSIX runner")
    def test_actual_workflow_gate_fails_missing_manual_p2_but_preserves_schedule(self) -> None:
        lines = (ROOT / ".github/workflows/nightly.yml").read_text(encoding="utf-8").splitlines()
        start = lines.index("      - name: Check sibling read token")
        begin = next(i for i in range(start, len(lines)) if lines[i] == "        run: |") + 1
        body = []
        for line in lines[begin:]:
            if not line.startswith("          "):
                break
            body.append(line[10:])
        self.assertTrue(body)
        for configured, required, expected in (
            ("true", "true", 0),
            ("false", "true", 1),
            ("false", "false", 0),
        ):
            with (
                self.subTest(configured=configured, required=required),
                tempfile.TemporaryDirectory() as directory,
            ):
                output = Path(directory) / "output"
                result = subprocess.run(
                    ["bash", "-c", "\n".join(body)],
                    env={
                        **os.environ,
                        "SIBLING_TOKEN_CONFIGURED": configured,
                        "P2_REQUIRED": required,
                        "GITHUB_OUTPUT": str(output),
                    },
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, expected)
                self.assertIn("available=" + configured, output.read_text())
                if required == "true" and configured == "false":
                    self.assertIn("::error", result.stdout)


if __name__ == "__main__":
    unittest.main()
