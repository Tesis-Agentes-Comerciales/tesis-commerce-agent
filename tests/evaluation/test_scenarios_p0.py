"""Offline harness tests, not executions of UX03 or observations of Merchant."""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from commerce_evaluation.scenarios_p0 import (
    CELL_EXPECTATIONS,
    OUTCOMES,
    SCENARIO_IDS,
    build_report,
    choose_ux03,
    count_plan,
    eligibility,
    load_json,
    planned_cells,
    report_outcome,
    score_ux03,
    validate_contract,
)

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "evaluation" / "scenarios" / "p0"


class P0ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = load_json(DATA / "matrix.json")
        self.oracle = load_json(DATA / "ux03-oracle.json")

    def test_exact_matrix_and_five_cell_contract(self) -> None:
        validate_contract(self.matrix, self.oracle)
        self.assertEqual(len(SCENARIO_IDS), 33)
        self.assertEqual(len(planned_cells(self.matrix, self.oracle)), 37)

    def test_coverage_references_resolve_without_importing_product(self) -> None:
        for row in self.matrix["scenarios"]:
            for reference in row["coverage"]["references"]:
                path, name = reference.split("::")
                tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
                functions = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                with self.subTest(reference=reference):
                    self.assertIn(name, functions)

    def test_missing_duplicate_and_relabelled_rows_are_rejected(self) -> None:
        for mutation in ("missing", "duplicate", "relabelled"):
            matrix = copy.deepcopy(self.matrix)
            if mutation == "missing":
                matrix["scenarios"].pop()
            elif mutation == "duplicate":
                matrix["scenarios"].append(matrix["scenarios"][0])
            else:
                matrix["scenarios"][0]["id"] = "UX99"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_contract(matrix, self.oracle)

    def test_preregistration_cannot_claim_observed_execution(self) -> None:
        self.matrix["scenarios"][2]["execution"] = {"status": "passed", "evidence": []}
        with self.assertRaises(ValueError):
            validate_contract(self.matrix, self.oracle)

    def test_all_preregistered_decisions(self) -> None:
        for cell in self.oracle["cells"]:
            with self.subTest(cell=cell["id"]):
                self.assertEqual(choose_ux03(cell["a"], cell["b"]), cell["expected"])

    def test_budget_is_inclusive_and_one_minor_unit_over_is_false(self) -> None:
        a = copy.deepcopy(self.oracle["cells"][0]["a"])
        self.assertTrue(eligibility(a))
        a["total_minor"] += 1
        self.assertFalse(eligibility(a))

    def test_priority_is_not_lowest_price(self) -> None:
        cell = self.oracle["cells"][2]
        self.assertLess(cell["b"]["total_minor"], cell["a"]["total_minor"])
        self.assertEqual(choose_ux03(cell["a"], cell["b"]), "A")

    def test_technical_unknown_is_not_false(self) -> None:
        cell = self.oracle["cells"][-1]
        self.assertIsNone(eligibility(cell["a"]))
        self.assertEqual(choose_ux03(cell["a"], cell["b"]), "undetermined")
        # When A is known false, an unknown B must also suspend the decision.
        false_a = self.oracle["cells"][1]["a"]
        self.assertEqual(choose_ux03(false_a, cell["a"]), "undetermined")

    def test_invalid_facts_are_rejected(self) -> None:
        for field, value in (
            ("total_minor", True),
            ("total_minor", -1),
            ("total_minor", 1.5),
            ("budget_minor", False),
            ("condition_met", "true"),
            ("currency", ""),
            ("total_minor", None),
            ("verification", "assumed"),
        ):
            a = copy.deepcopy(self.oracle["cells"][0]["a"])
            a[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                eligibility(a)
        a = copy.deepcopy(self.oracle["cells"][-1]["a"])
        a["total_minor"] = 10
        with self.assertRaises(ValueError):
            eligibility(a)

    def test_currency_mismatch_is_not_implicitly_converted(self) -> None:
        a, b = copy.deepcopy(self.oracle["cells"][2]["a"]), self.oracle["cells"][2]["b"]
        a["currency"] = "COP"
        with self.assertRaises(ValueError):
            choose_ux03(a, b)

    def test_changed_cell_expectation_is_rejected(self) -> None:
        self.oracle["cells"][2]["expected"] = "B"
        with self.assertRaises(ValueError):
            validate_contract(self.matrix, self.oracle)
        self.assertEqual(CELL_EXPECTATIONS["UX03-C"], "A")

    def test_plan_comparison_includes_unexecuted_cells(self) -> None:
        counts = count_plan(["a", "b", "c"], {"a": "passed", "b": "skipped"})
        self.assertEqual(counts["passed"], 1)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["not_executed"], 1)
        self.assertEqual(sum(counts.values()), 3)
        self.assertEqual(
            count_plan(list(OUTCOMES), dict(zip(OUTCOMES, OUTCOMES, strict=True))),
            dict.fromkeys(OUTCOMES, 1),
        )

    def test_invalid_observation_ids_and_outcomes_are_rejected(self) -> None:
        for plan, observations in (
            (["a"], {"other": "passed"}),
            (["a", "a"], {}),
            (["a"], {"a": "unknown"}),
        ):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                count_plan(plan, observations)

    def test_pytest_phase_outcomes_keep_xfail_and_xpass_distinct(self) -> None:
        cases = [
            ("passed", "call", False, False, "passed"),
            ("failed", "setup", False, False, "failed"),
            ("skipped", "setup", False, False, "skipped"),
            ("skipped", "call", True, False, "xfail"),
            ("passed", "call", True, False, "xpass"),
            ("failed", "call", False, True, "xpass"),
            ("passed", "teardown", False, False, None),
        ]
        for outcome, when, wasxfail, strict, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(report_outcome(outcome, when, wasxfail, strict), expected)

    def test_missing_pytest_evidence_cannot_validate_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = build_report(ROOT, Path(directory) / "absent.json")
        self.assertEqual(report["python_tests"]["status"], "not_executed")
        self.assertEqual(report["scenario_executions"]["counts"]["not_executed"], 37)
        self.assertEqual(report["integration"]["status"], "not_executed")
        self.assertIsNone(report["provenance"]["merchant_sha"])
        self.assertIsNone(report["provenance"]["psp_sha"])
        self.assertEqual(len(report["provenance"]["agent_sha"]), 40)

    def test_pytest_recorder_accounts_for_interruptions_and_phase_failures(self) -> None:
        from types import SimpleNamespace

        from commerce_evaluation.p0_pytest_report import OutcomeRecorder

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "outcomes.json"
            recorder = OutcomeRecorder(destination)
            recorder.pytest_collection_finish(
                SimpleNamespace(
                    items=[SimpleNamespace(nodeid=n) for n in ("ok", "error", "pending")]
                )
            )
            for node, outcome, when in (
                ("ok", "passed", "call"),
                ("error", "passed", "call"),
                ("error", "failed", "teardown"),
            ):
                recorder.pytest_runtest_logreport(
                    SimpleNamespace(nodeid=node, outcome=outcome, when=when, longrepr="")
                )
            recorder.pytest_collectreport(SimpleNamespace(failed=True))
            recorder.pytest_sessionfinish(None, 1)
            report = load_json(destination)
        self.assertEqual(report["counts"]["passed"], 1)
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["counts"]["not_executed"], 1)
        self.assertEqual(report["collection_errors"], 1)
        self.assertEqual(report["status"], "incomplete_or_failed")

    def test_pytest_recorder_distinguishes_both_xpass_forms(self) -> None:
        from types import SimpleNamespace

        from commerce_evaluation.p0_pytest_report import OutcomeRecorder

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "outcomes.json"
            recorder = OutcomeRecorder(destination)
            reports = [
                SimpleNamespace(
                    nodeid="xf", outcome="skipped", when="call", wasxfail="reason", longrepr=""
                ),
                SimpleNamespace(
                    nodeid="xp", outcome="passed", when="call", wasxfail="reason", longrepr=""
                ),
                SimpleNamespace(
                    nodeid="strict",
                    outcome="failed",
                    when="call",
                    longrepr="[XPASS(strict)] reason",
                ),
                SimpleNamespace(nodeid="skip", outcome="skipped", when="setup", longrepr=""),
            ]
            for report in reports:
                recorder.pytest_runtest_logreport(report)
            recorder.pytest_sessionfinish(None, 1)
            result = load_json(destination)
        self.assertEqual(result["counts"]["xfail"], 1)
        self.assertEqual(result["counts"]["xpass"], 2)
        self.assertEqual(result["counts"]["skipped"], 1)

    def evidence_files(self, directory: str) -> tuple[Path, Path]:
        # Synthetic reporting fixtures; never observations of Agent/Merchant.
        pytest_path, junit = Path(directory) / "python.json", Path(directory) / "junit.xml"
        counts = dict.fromkeys(OUTCOMES, 0)
        counts["passed"] = 1
        pytest_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "counts": counts,
                    "exit_code": 0,
                    "collected": 1,
                    "deselected": 0,
                    "collection_errors": 0,
                }
            ),
            encoding="utf-8",
        )
        junit.write_text(
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase classname="synthetic" name="one"/></testsuite></testsuites>',
            encoding="utf-8",
        )
        return pytest_path, junit

    def test_green_python_harness_still_leaves_all_scenarios_unexecuted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pytest_path, junit = self.evidence_files(directory)
            report = build_report(ROOT, pytest_path, junit)
        self.assertTrue(report["python_tests"]["artifacts_valid"])
        self.assertEqual(report["python_tests"]["counts"]["passed"], 1)
        self.assertEqual(report["scenario_executions"]["counts"]["not_executed"], 37)
        self.assertEqual(report["scenario_executions"]["counts"]["passed"], 0)
        self.assertEqual(report["integration"]["status"], "not_executed")

    def test_wrong_observed_decisions_fail_and_absence_is_not_passed(self) -> None:
        for cell in self.oracle["cells"]:
            with self.subTest(cell=cell["id"]):
                self.assertEqual(score_ux03(cell, cell["expected"]), "passed")
                self.assertEqual(score_ux03(cell, None), "not_executed")
                for wrong in {"A", "B", "none", "undetermined", "C"} - {cell["expected"]}:
                    self.assertEqual(score_ux03(cell, wrong), "failed")

    def test_altered_facts_cannot_relabel_cell_semantics(self) -> None:
        self.oracle["cells"][0]["b"]["condition_met"] = True
        with self.assertRaises(ValueError):
            validate_contract(self.matrix, self.oracle)

    def test_altered_oracle_cannot_be_used_to_score_a_matching_wrong_answer(self) -> None:
        cell = self.oracle["cells"][2]
        cell["expected"] = "B"
        with self.assertRaises(ValueError):
            score_ux03(cell, "B")

    def test_missing_incomplete_and_malformed_junit_cannot_claim_success(self) -> None:
        for mutation in ("missing", "truncated", "missing_case", "missing_suite"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                pytest_path, junit = self.evidence_files(directory)
                if mutation == "missing":
                    junit.unlink()
                elif mutation == "truncated":
                    junit.write_text("<testsuites>", encoding="utf-8")
                elif mutation == "missing_case":
                    junit.write_text(
                        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
                        "</testsuite></testsuites>",
                        encoding="utf-8",
                    )
                else:
                    junit.write_text("<testsuites/>", encoding="utf-8")
                report = build_report(ROOT, pytest_path, junit)
                self.assertFalse(report["python_tests"]["artifacts_valid"])
                self.assertEqual(report["scenario_executions"]["counts"]["passed"], 0)

    def test_incomplete_and_inconsistent_python_reports_are_rejected(self) -> None:
        for mutation in ("minimal", "negative", "missing_count", "wrong_exit", "omitted_test"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                pytest_path, junit = self.evidence_files(directory)
                value = load_json(pytest_path)
                if mutation == "minimal":
                    value = {"status": "completed", "counts": {"passed": 17}}
                elif mutation == "negative":
                    value["counts"]["failed"] = -1
                elif mutation == "missing_count":
                    del value["counts"]["xpass"]
                elif mutation == "wrong_exit":
                    value["exit_code"] = 1
                else:
                    value["counts"]["passed"] = 2
                    value["collected"] = 2
                pytest_path.write_text(json.dumps(value), encoding="utf-8")
                report = build_report(ROOT, pytest_path, junit)
                self.assertFalse(report["python_tests"]["artifacts_valid"])

    def test_junit_failure_cannot_be_presented_as_python_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pytest_path, junit = self.evidence_files(directory)
            junit.write_text(
                '<testsuite tests="1" failures="1" errors="0" skipped="0">'
                '<testcase name="one"><failure/></testcase></testsuite>',
                encoding="utf-8",
            )
            self.assertFalse(
                build_report(ROOT, pytest_path, junit)["python_tests"]["artifacts_valid"]
            )

    def test_failed_session_is_reported_as_failed_with_valid_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pytest_path, junit = self.evidence_files(directory)
            value = load_json(pytest_path)
            value.update(status="incomplete_or_failed", exit_code=1)
            value["counts"].update(passed=0, failed=1)
            pytest_path.write_text(json.dumps(value), encoding="utf-8")
            junit.write_text(
                '<testsuite tests="1" failures="1" errors="0" skipped="0">'
                '<testcase name="one"><failure/></testcase></testsuite>',
                encoding="utf-8",
            )
            result = build_report(ROOT, pytest_path, junit)["python_tests"]
        self.assertTrue(result["artifacts_valid"])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["counts"]["failed"], 1)

    def test_cli_missing_evidence_exits_nonzero_and_still_writes_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "commerce_evaluation.scenarios_p0",
                    "--root",
                    str(ROOT),
                    "--pytest-report",
                    str(path / "absent.json"),
                    "--junit",
                    str(path / "absent.xml"),
                    "--output",
                    str(path / "report.json"),
                    "--summary",
                    str(path / "summary.md"),
                ],
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                load_json(path / "report.json")["python_tests"]["status"], "not_executed"
            )
            self.assertIn("not_executed", (path / "summary.md").read_text(encoding="utf-8"))

    def test_report_write_error_is_not_swallowed(self) -> None:
        from types import SimpleNamespace

        from commerce_evaluation.p0_pytest_report import OutcomeRecorder

        with tempfile.TemporaryDirectory() as directory:
            # A directory is not a writable report file on any supported OS.
            recorder = OutcomeRecorder(Path(directory))
            recorder.pytest_collection_finish(SimpleNamespace(items=[]))
            with self.assertRaises(OSError):
                recorder.pytest_sessionfinish(None, 0)

    def test_mixed_pytest_outcomes_remain_distinct_in_valid_junit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pytest_path, junit = self.evidence_files(directory)
            value = load_json(pytest_path)
            value.update(status="incomplete_or_failed", exit_code=1, collected=7)
            value["counts"] = {
                "passed": 1,
                "failed": 1,
                "skipped": 1,
                "xfail": 1,
                "xpass": 2,
                "not_executed": 1,
            }
            pytest_path.write_text(json.dumps(value), encoding="utf-8")
            junit.write_text(
                '<testsuite tests="6" failures="2" errors="0" skipped="2">'
                '<testcase name="pass"/><testcase name="fail"><failure/></testcase>'
                '<testcase name="skip"><skipped/></testcase>'
                '<testcase name="xfail"><skipped/></testcase>'
                '<testcase name="xpass"/><testcase name="strict"><failure/></testcase>'
                "</testsuite>",
                encoding="utf-8",
            )
            result = build_report(ROOT, pytest_path, junit)["python_tests"]
        self.assertTrue(result["artifacts_valid"])
        self.assertEqual(result["counts"], value["counts"])

    def test_junit_teardown_errors_and_duplicate_cases_do_not_hide_failures(self) -> None:
        for cases in (
            '<testcase name="one"><error/></testcase>',
            '<testcase name="one"><failure/></testcase><testcase name="one"><error/></testcase>',
        ):
            with self.subTest(cases=cases), tempfile.TemporaryDirectory() as directory:
                pytest_path, junit = self.evidence_files(directory)
                value = load_json(pytest_path)
                value.update(status="incomplete_or_failed", exit_code=1)
                value["counts"].update(passed=0, failed=1)
                pytest_path.write_text(json.dumps(value), encoding="utf-8")
                failures = int("<failure" in cases)
                junit.write_text(
                    f'<testsuite tests="{1 if failures else 2}" failures="{failures}" '
                    f'errors="1" skipped="0">{cases}</testsuite>',
                    encoding="utf-8",
                )
                result = build_report(ROOT, pytest_path, junit)["python_tests"]
                self.assertTrue(result["artifacts_valid"])
                self.assertEqual(result["counts"]["failed"], 1)

    def test_cli_output_write_error_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pytest_path, junit = self.evidence_files(directory)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "commerce_evaluation.scenarios_p0",
                    "--root",
                    str(ROOT),
                    "--pytest-report",
                    str(pytest_path),
                    "--junit",
                    str(junit),
                    "--output",
                    directory,
                    "--summary",
                    str(Path(directory) / "summary.md"),
                ],
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(directory) / "summary.md").exists())

    def test_junit_skip_cannot_be_presented_as_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pytest_path, junit = self.evidence_files(directory)
            junit.write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="1">'
                '<testcase name="one"><skipped/></testcase></testsuite>',
                encoding="utf-8",
            )
            result = build_report(ROOT, pytest_path, junit)["python_tests"]
            self.assertFalse(result["artifacts_valid"])

    def test_junit_skip_cannot_be_presented_as_xpass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pytest_path, junit = self.evidence_files(directory)
            value = load_json(pytest_path)
            value["counts"].update(passed=0, xpass=1)
            pytest_path.write_text(json.dumps(value), encoding="utf-8")
            junit.write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="1">'
                '<testcase name="one"><skipped/></testcase></testsuite>',
                encoding="utf-8",
            )
            self.assertFalse(
                build_report(ROOT, pytest_path, junit)["python_tests"]["artifacts_valid"]
            )

    def test_junit_header_requires_exact_recorded_subtest_reports(self) -> None:
        for recorded, declared, expected in (
            (2, 3, True),
            (1, 3, False),
            (2, 2, False),
            (0, 3, False),
        ):
            with (
                self.subTest(recorded=recorded, declared=declared),
                tempfile.TemporaryDirectory() as directory,
            ):
                pytest_path, junit = self.evidence_files(directory)
                value = load_json(pytest_path)
                value["junit_subtests"] = {"passed": recorded, "failed": 0, "skipped": 0}
                pytest_path.write_text(json.dumps(value), encoding="utf-8")
                junit.write_text(
                    f'<testsuite tests="{declared}" failures="0" errors="0" skipped="0">'
                    '<testcase classname="synthetic" name="one"/></testsuite>',
                    encoding="utf-8",
                )
                result = build_report(ROOT, pytest_path, junit)["python_tests"]
                self.assertEqual(result["artifacts_valid"], expected)

    def test_subtest_counts_do_not_excuse_an_omitted_testcase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pytest_path, junit = self.evidence_files(directory)
            value = load_json(pytest_path)
            value["junit_subtests"] = {"passed": 2, "failed": 0, "skipped": 0}
            value.update(collected=2)
            value["counts"]["passed"] = 2
            pytest_path.write_text(json.dumps(value), encoding="utf-8")
            junit.write_text(
                '<testsuite tests="3" failures="0" errors="0" skipped="0">'
                '<testcase name="one"/></testsuite>',
                encoding="utf-8",
            )
            self.assertFalse(
                build_report(ROOT, pytest_path, junit)["python_tests"]["artifacts_valid"]
            )

    def test_invalid_subtest_counts_are_rejected(self) -> None:
        for subtests in (
            {"passed": -1, "failed": 0, "skipped": 0},
            {"passed": True, "failed": 0, "skipped": 0},
            {"passed": 1},
            None,
        ):
            with self.subTest(subtests=subtests), tempfile.TemporaryDirectory() as directory:
                pytest_path, junit = self.evidence_files(directory)
                value = load_json(pytest_path)
                value["junit_subtests"] = subtests
                pytest_path.write_text(json.dumps(value), encoding="utf-8")
                self.assertFalse(
                    build_report(ROOT, pytest_path, junit)["python_tests"]["artifacts_valid"]
                )

    def test_pr_and_actions_commits_are_distinct_and_checkout_is_verified(self) -> None:
        from unittest.mock import patch

        sha = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        with tempfile.TemporaryDirectory() as directory:
            pytest_path, junit = self.evidence_files(directory)
            with patch.dict(os.environ, {"P0_PR_HEAD_SHA": "a" * 40, "P0_ACTIONS_SHA": sha}):
                provenance = build_report(ROOT, pytest_path, junit)["provenance"]
            self.assertEqual(provenance["pr_head_sha"], "a" * 40)
            self.assertEqual(provenance["actions_sha"], sha)
            self.assertEqual(provenance["checked_out_sha"], sha)
            with (
                patch.dict(os.environ, {"P0_ACTIONS_SHA": "b" * 40}),
                self.assertRaises(ValueError),
            ):
                build_report(ROOT, pytest_path, junit)

    @unittest.skipUnless(
        importlib.util.find_spec("pytest"), "requires this checkout's locked pytest"
    )
    def test_real_pytest_reconciles_passing_failed_and_skipped_subtests(self) -> None:
        for mode in ("pass", "fail", "skip"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                source = (
                    "import unittest\n"
                    "class Example(unittest.TestCase):\n"
                    "    def test_values(self):\n"
                    "        for value in (1, 0):\n"
                    "            with self.subTest(value=value):\n"
                )
                if mode == "skip":
                    source += "                if value == 0: self.skipTest('synthetic skip')\n"
                source += (
                    "                self.assertGreaterEqual(value, 0)\n"
                    if mode == "pass"
                    else "                self.assertGreater(value, 0)\n"
                )
                (folder / "test_example.py").write_text(source, encoding="utf-8")
                (folder / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
                python_report, junit = folder / "python.json", folder / "junit.xml"
                env = {
                    **os.environ,
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "PYTHONPATH": str(ROOT / "evaluation" / "src"),
                }
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "pytest",
                        "-q",
                        "-c",
                        str(folder / "pytest.ini"),
                        "--confcutdir",
                        str(folder),
                        str(folder / "test_example.py"),
                        "-p",
                        "commerce_evaluation.p0_pytest_report",
                        "--p0-report=" + str(python_report),
                        "--junitxml=" + str(junit),
                    ],
                    cwd=folder,
                    env=env,
                    capture_output=True,
                )
                self.assertEqual(
                    result.returncode,
                    1 if mode == "fail" else 0,
                    result.stdout.decode(errors="replace"),
                )
                report = build_report(ROOT, python_report, junit)["python_tests"]
                self.assertTrue(report["artifacts_valid"])
                self.assertEqual(sum(report["junit_subtests"].values()), 2)
                self.assertEqual(
                    report["counts"][{"pass": "passed", "fail": "failed", "skip": "skipped"}[mode]],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
