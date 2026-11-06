"""Offline validation and counts for the P0 evidence register."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile

from .ct19_hnp import verify_control
from .exp01 import native_inf31_verified
from .scenarios_p0 import planned_cells

P0 = Path("evaluation/scenarios/p0")
ACTION_RUN = "https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/"
STATUS_COVERAGE = {
    "passed": "bounded",
    "partial": "partial",
    "failed": "negative",
    "blocked": "incomplete",
    "harness_error": "incomplete",
    "not_executed": "none",
    "not_applicable": "none",
}
RUN_KINDS = {"measurement", "harness_error", "reporting_error", "infrastructure", "blocked"}
CT19_INDEX = "../../results/ct19-hp/runs.json"
CT19_RUN = "42033289049"
CT19_FIXTURE = "41967635493"
INF33_SOURCE = {
    "path": "docs/design/p0-model-failures.md",
    "section": "4. Celdas y oráculos",
    "sha256": "0b70704c51f6b27ceb73f1e5f7a8be9d16cac16d08cf41e7969d6c1957492e1c",
}
# A local run versioned in Git has no Actions run or ZIP: its evidence is the frozen protocol, the
# run's summary and JUnit, and the SHA256SUMS ledger of its results directory.
LOCAL_RUN_KINDS = {"measurement", "harness_error"}
LOCAL_RUN_KEYS = {
    "experiment",
    "kind",
    "protocol",
    "protocol_sha256",
    "checksum_index",
    "summary",
    "summary_sha256",
    "junit",
    "junit_sha256",
    "junit_counts",
    "sources",
    "note",
}
LOCAL_CELL_KEYS = {
    "status",
    "coverage",
    "scope",
    "limitation",
    "local_run",
    "cases",
    "prior_attempts",
}
# A protocol may also freeze CI cells: pytest node ids run once without network, whose JUnit sits
# in the same ledger (p0-ux08-ux10.md §5.1). Its run then names that JUnit, and its cells may list
# them in `ci_cases`.
LOCAL_RUN_CI_KEYS = {"ci_junit", "ci_junit_sha256", "ci_junit_counts"}


def require(condition: object, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load(path: Path) -> Any:
    return json.loads(path.read_bytes(), object_pairs_hook=unique_object)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def preregistration_digest(path: Path) -> str:
    # The two P0 input files have LF Git blobs but Windows may expand their checkout.
    return digest(path.read_bytes().replace(b"\r\n", b"\n"))


def checksums(data: bytes) -> dict[str, str]:
    entries = {}
    for line in data.decode("utf-8").splitlines():
        expected, name = line.split("  ", 1)
        require(name not in entries and re.fullmatch(r"[a-f0-9]{64}", expected), name)
        entries[name] = expected
    return entries


def repo_path(root: Path, ref: str, prefix: str) -> Path:
    path = (root / ref).resolve()
    require(path.is_relative_to(root / prefix), f"Invalid reference: {ref}")
    return path


def junit_counts(data: bytes) -> dict[str, int]:
    nodes = list(ElementTree.fromstring(data).iter("testcase"))
    counts = {
        "failed": sum(node.find("failure") is not None for node in nodes),
        "error": sum(node.find("error") is not None for node in nodes),
        "skipped": sum(node.find("skipped") is not None for node in nodes),
    }
    counts["passed"] = len(nodes) - sum(counts.values())
    return counts


def validate_run(
    root: Path, run_id: str, row: dict[str, Any], matrix_sha: str, oracle_sha: str
) -> tuple[dict[str, Any], dict[str, Any], int]:
    require(run_id.isdecimal() and row["actions"] == ACTION_RUN + run_id, "Invalid Actions run")
    require(row["kind"] in RUN_KINDS, f"Unknown run kind: {run_id}")
    protocol_path = repo_path(root, row["protocol"], "evaluation/scenarios")
    archive = repo_path(root, row["archive"], "evaluation/results")
    ledger = repo_path(root, row["checksum_index"], "evaluation/results")
    require(archive.name == f"{run_id}.zip", f"Wrong archive for run {run_id}")
    ledger_entries = checksums(ledger.read_bytes())
    require(
        ledger_entries.get(f"runs/{run_id}.zip") == row["sha256"]
        and digest(archive.read_bytes()) == row["sha256"],
        f"Archive hash mismatch: {run_id}",
    )
    protocol = load(protocol_path)
    require(
        digest(protocol_path.read_bytes()) == row["protocol_sha256"]
        and protocol["matrix_sha256"] == matrix_sha
        and protocol["ux03_oracle_sha256"] == oracle_sha,
        f"Frozen protocol mismatch: {run_id}",
    )
    with ZipFile(archive) as bundle:
        require(bundle.testzip() is None, f"Corrupt ZIP: {run_id}")
        manifest = json.loads(bundle.read("exp01-manifest.json"))
        names = set(bundle.namelist())
        inner = checksums(bundle.read("exp01-SHA256SUMS")) if "exp01-SHA256SUMS" in names else {}
        for name, expected in inner.items():
            require(digest(bundle.read(name)) == expected, f"Internal hash: {run_id}/{name}")
        junit = junit_counts(bundle.read("exp01-junit.xml")) if "exp01-junit.xml" in names else None
        report = (
            json.loads(bundle.read("exp01-report.json")) if "exp01-report.json" in names else {}
        )
        if protocol.get("schema") == "inf31-hp-reassessment/1":
            require(
                row["experiment"] == "EXP-08" and row["conclusion"] == "success",
                f"Invalid INF31 measurement provenance: {run_id}",
            )
            validate_inf31_archive(bundle, inner, protocol, manifest, report, run_id)
        if protocol.get("schema") == "ct19-hnp-protocol/1":
            require(
                row["experiment"] == "EXP-09" and row["conclusion"] == "success",
                f"Invalid CT19 HNP measurement provenance: {run_id}",
            )
            validate_ct19_hnp_archive(bundle, inner, protocol, manifest, report, run_id)
    require(
        junit == row["junit"] and report.get("validated") == row["gate"],
        f"JUnit or evidence gate mismatch: {run_id}",
    )
    if report and junit is not None:
        require(report["junit"] == junit, f"Archived JUnit/report mismatch: {run_id}")
    require(
        manifest["protocol_sha256"] == row["protocol_sha256"]
        and manifest["status"] == row["preflight"]
        and re.fullmatch(r"[0-9a-f]{40}", row["sources"]["agent"]),
        f"Manifest mismatch: {run_id}",
    )
    if manifest["status"] == "verified":
        sources = manifest["sources"]
        require(
            all(sources[key]["clean"] is True for key in ("agent", "merchant", "payment"))
            and row["sources"] == {key: source["sha"] for key, source in sources.items()}
            and row["sources"]["merchant"] == protocol["merchant_sha"]
            and row["sources"]["payment"] == protocol["payment_sha"],
            f"Source SHA or cleanliness mismatch: {run_id}",
        )
    else:
        require(
            row["kind"] == "blocked"
            and row["sources"]["merchant"] is None
            and row["sources"]["payment"] is None,
            f"Unverified sources: {run_id}",
        )
    return protocol, report, len(inner)


def validate_ct19_hnp_archive(
    bundle: ZipFile, hashes: dict[str, str], protocol: dict, manifest: dict, report: dict, run: str
) -> None:
    cases = protocol["cases"]
    tests = list(ElementTree.fromstring(bundle.read("exp01-junit.xml")).iter("testcase"))
    module = protocol["test_module"].removesuffix(".py::").replace("/", ".")
    recorder = json.loads(bundle.read("exp01-outcomes.json"))
    require(
        len(tests) == len(cases) == 3
        and {(test.get("classname"), test.get("name")) for test in tests}
        == {(module, case["test"]) for case in cases.values()}
        and all(len(test) == 0 for test in tests)
        and recorder == report["recorder"]
        and recorder["collected"] == recorder["counts"]["passed"] == len(tests)
        and recorder["status"] == "completed"
        and recorder["exit_code"] == recorder["deselected"] == recorder["collection_errors"] == 0
        and report["manifest"] == manifest
        and manifest["sources"]["agent"]["product_tree"] == protocol["agent_product_tree"]
        and report["validated"] is True
        and report["issues"] == []
        and report["attributed_scenarios"] == {"CT19": "passed"}
        and report["non_scenario_controls"]
        == {"CT19-PRESENT": "passed", "CT19-HNP-STOCK": "passed"}
        and set(report["controls"]) == set(cases)
        and {"exp01-manifest.json", "exp01-junit.xml", "exp01-report.json", "exp01-outcomes.json"}
        <= hashes.keys()
        and len(bundle.namelist()) == len(set(bundle.namelist()))
        and all(
            name == "exp01-SHA256SUMS"
            or name in hashes
            or digest(bundle.read(name)) in hashes.values()
            for name in bundle.namelist()
        ),
        f"CT19 HNP required evidence mismatch: {run}",
    )
    seen = set()
    for name in hashes:
        if not name.endswith("/exp01-frozen.json"):
            continue
        frozen = json.loads(bundle.read(name))
        case_id = frozen["case_id"]
        control = report["controls"][case_id]
        observed = name.replace("exp01-frozen.json", "exp01-observed.json")
        facts = control["observations"]
        inputs = frozen["inputs"]
        audit = facts["creates"][0]["audit"]
        require(
            case_id not in seen
            and hashes[name] == control["input_sha256"]
            and observed in hashes
            and json.loads(bundle.read(observed)) == control
            and frozen["oracle"] == cases[case_id]
            and frozen["protocol_sha256"] == manifest["protocol_sha256"]
            and frozen["frozen_at_ns"] < control["started_at_ns"] < facts["timeline"][0]
            and frozen["semantic_evaluation"] is False
            and inputs["condition"] == "B2-HNP"
            and inputs["offer_id"] == facts["sku"]
            and inputs["catalog_sha256"] == facts["restored_catalog_sha256"]
            and all(inputs[key] == audit[key] for key in ("run_id", "actor_id"))
            and control["status"] == "passed"
            and control["stimulus_verified"] is True
            and control["cleanup_verified"] is True
            and verify_control(inputs["control"], facts)
            and inputs["control"] == case_id.rsplit(":", 1)[-1],
            f"CT19 HNP frozen input or control mismatch: {run}/{case_id}",
        )
        seen.add(case_id)
    require(seen == cases.keys(), f"CT19 HNP frozen input missing: {run}")


def validate_inf31_archive(
    bundle: ZipFile, hashes: dict[str, str], protocol: dict, manifest: dict, report: dict, run: str
) -> None:
    tests = list(ElementTree.fromstring(bundle.read("exp01-junit.xml")).iter("testcase"))
    recorder = json.loads(bundle.read("exp01-outcomes.json"))
    module = protocol["test_module"].removesuffix(".py::").replace("/", ".")
    cases = protocol["cases"]
    require(
        len(tests) == len(cases) == 6
        and {(test.get("classname"), test.get("name")) for test in tests}
        == {(module, case["test"]) for case in cases.values()}
        and all(len(test) == 0 for test in tests)
        and recorder == report["recorder"]
        and recorder["collected"] == recorder["counts"]["passed"] == len(tests)
        and recorder["status"] == "completed"
        and recorder["exit_code"] == recorder["deselected"] == recorder["collection_errors"] == 0
        and all(
            recorder["counts"][outcome] == 0
            for outcome in ("failed", "skipped", "xfail", "xpass", "not_executed")
        )
        and report["manifest"] == manifest
        and report["validated"] is True
        and report["issues"] == []
        and report["measured_modes"] == {"INF31": ["HP"]}
        and set(report["controls"]) == set(cases)
        and {"exp01-manifest.json", "exp01-junit.xml", "exp01-report.json", "exp01-outcomes.json"}
        <= hashes.keys(),
        f"INF31 required evidence mismatch: {run}",
    )
    seen = set()
    for name in hashes:
        if not name.endswith("/exp01-frozen.json"):
            continue
        frozen = json.loads(bundle.read(name))
        case_id = frozen["case_id"]
        control = report["controls"][case_id]
        observed = name.replace("exp01-frozen.json", "exp01-observed.json")
        feed = name.replace("exp01-frozen.json", "feed/products.jsonl")
        require(
            case_id not in seen
            and hashes[name] == control["input_sha256"]
            and observed in hashes
            and json.loads(bundle.read(observed)) == control
            and frozen["oracle"] == cases[case_id]
            and frozen["protocol_sha256"] == manifest["protocol_sha256"]
            and frozen["frozen_at_ns"] < control["started_at_ns"]
            and frozen["semantic_evaluation"] is False
            and frozen["inputs"]["feed_sha256"] == hashes.get(feed)
            and control["status"] == "passed"
            and control["cleanup_verified"] is True,
            f"INF31 frozen input or control mismatch: {run}/{case_id}",
        )
        seen.add(case_id)
    require(
        seen == cases.keys()
        and native_inf31_verified(report["controls"]["INF31:merchant_503"]["observations"]),
        f"INF31 native outage or frozen input missing: {run}",
    )


def validate_ct19_archive(root: Path, run: str, row: dict, protocol_path: Path) -> tuple[dict, int]:
    archive_ref = f"runs/{run}/original.zip"
    require(
        row["url"] == ACTION_RUN + run and row["original_zip"] == archive_ref,
        f"CT19 archive reference mismatch: {run}",
    )
    archive = root / "evaluation/results/ct19-hp" / archive_ref
    data = archive.read_bytes()
    require(
        len(data) == row["bytes"] and digest(data) == row["zip_sha256"], f"CT19 ZIP hash: {run}"
    )
    require(
        digest(protocol_path.read_bytes()) == row["protocol_sha256"],
        f"CT19 protocol hash: {run}",
    )
    protocol = load(protocol_path)
    with ZipFile(archive) as bundle:
        names = bundle.namelist()
        require(
            len(names) == len(set(names)) and bundle.testzip() is None, f"Corrupt CT19 ZIP: {run}"
        )
        inner = checksums(bundle.read("exp01-SHA256SUMS"))
        require(len(inner) == row["entries_verified"], f"CT19 hash count: {run}")
        for name, expected in inner.items():
            require(digest(bundle.read(name)) == expected, f"CT19 internal hash: {run}/{name}")
        for name, field in (
            ("exp01-manifest.json", "manifest_sha256"),
            ("exp01-junit.xml", "junit_sha256"),
            ("exp01-report.json", "report_sha256"),
        ):
            require(inner.get(name) == row[field], f"CT19 index hash: {run}/{name}")
        manifest = json.loads(bundle.read("exp01-manifest.json"))
        report = json.loads(bundle.read("exp01-report.json"))
        junit = bundle.read("exp01-junit.xml")
        tests = [
            (node.get("classname"), node.get("name"))
            for node in ElementTree.fromstring(junit).iter("testcase")
        ]
        module = protocol["test_module"].removesuffix(".py::").replace("/", ".")
        require(
            sorted(tests) == sorted((module, case["test"]) for case in protocol["cases"].values())
            and junit_counts(junit) == report["junit"] == row["junit"],
            f"CT19 JUnit mismatch: {run}",
        )
        require(
            report["manifest"] == manifest
            and manifest["status"] == "verified"
            and manifest["sources"] == row["sources"]
            and manifest["protocol_sha256"] == row["protocol_sha256"]
            and manifest["inputs_sha256"]
            == {
                "evaluation/scenarios/p0/matrix.json": protocol["matrix_sha256"],
                "evaluation/scenarios/p0/ux03-oracle.json": protocol["ux03_oracle_sha256"],
            }
            and set(manifest["sources"]) == {"agent", "merchant", "payment"}
            and all(
                source["clean"] is True
                and re.fullmatch(r"[a-f0-9]{40}", source["sha"])
                and re.fullmatch(r"[a-f0-9]{64}", source["uv_lock_sha256"])
                for source in manifest["sources"].values()
            )
            and manifest["sources"]["merchant"]["sha"] == protocol["merchant_sha"]
            and manifest["sources"]["payment"]["sha"] == protocol["payment_sha"],
            f"CT19 source manifest mismatch: {run}",
        )
        require(
            set(report["controls"]) == set(protocol["cases"])
            and report["validated"] is row["validated"]
            and report["control_counts"] == row["control_counts"]
            and report["issues"] == row["issues"]
            and report["stack_shutdown"] == row["shutdown"],
            f"CT19 outcome mismatch: {run}",
        )
        if report["validated"]:
            frozen_cases = set()
            for name in inner:
                if not name.endswith("/exp01-frozen.json"):
                    continue
                frozen = json.loads(bundle.read(name))
                case = frozen["case_id"]
                control = report["controls"][case]
                observed_name = name.replace("/exp01-frozen.json", "/exp01-observed.json")
                require(
                    case not in frozen_cases
                    and inner[name] == control["input_sha256"]
                    and observed_name in inner
                    and json.loads(bundle.read(observed_name)) == control
                    and frozen["protocol_sha256"] == row["protocol_sha256"]
                    and frozen["oracle"] == protocol["cases"][case]
                    and frozen["frozen_at_ns"] < control["started_at_ns"]
                    and frozen["semantic_evaluation"] is False
                    and control["status"] == "passed"
                    and control["cleanup_verified"] is True,
                    f"CT19 frozen evidence mismatch: {run}/{case}",
                )
                frozen_cases.add(case)
            require(
                frozen_cases == protocol["cases"].keys(), f"Missing CT19 frozen evidence: {run}"
            )
    return report, len(inner)


def validate_ct19(root: Path, cell: dict, matrix_sha: str, oracle_sha: str) -> tuple[int, int]:
    require(
        cell["evidence"] == CT19_INDEX
        and str(cell["run_id"]) == CT19_RUN
        and cell["case"] == "CT19:HP:removed"
        and cell["status"] == "passed"
        and cell["scope"].strip(),
        "Invalid CT19 HP attribution",
    )
    index = load(root / P0 / CT19_INDEX)
    fixture_runs = {"41967362290", CT19_FIXTURE}
    hp_runs = {"41968306631", "41968564766", CT19_RUN}
    require(set(index) == fixture_runs | hp_runs, "CT19 run history incomplete")
    fixture_path = root / "evaluation/scenarios/ct19-fixture.json"
    hp_path = root / "evaluation/scenarios/ct19-hp.json"
    for path in (fixture_path, hp_path):
        protocol = load(path)
        require(
            protocol["matrix_sha256"] == matrix_sha
            and protocol["ux03_oracle_sha256"] == oracle_sha,
            "CT19 prereregistration mismatch",
        )
    fixture = index[CT19_FIXTURE]
    require(
        load(hp_path)["fixture_prerequisite"]
        == {
            "run_id": int(CT19_FIXTURE),
            "agent_sha": fixture["sources"]["agent"]["sha"],
            "zip_sha256": fixture["zip_sha256"],
            "protocol_sha256": fixture["protocol_sha256"],
            "validated": True,
        }
        and fixture["validated"] is True,
        "CT19 fixture prerequisite mismatch",
    )
    verified = {
        run: validate_ct19_archive(root, run, row, fixture_path if run in fixture_runs else hp_path)
        for run, row in index.items()
    }
    report = verified[CT19_RUN][0]
    require(
        cell["agent_sha"] == index[CT19_RUN]["sources"]["agent"]["sha"]
        and report["validated"] is True
        and not report["issues"]
        and report["junit"] == {"passed": 2, "failed": 0, "error": 0, "skipped": 0}
        and report["attribution_status"] == "verified"
        and report["measured_modes"] == {"CT19": ["HP"], "CT19-STOCK": ["HP"]}
        and report["attributed_scenarios"] == {"CT19": "passed"}
        and report["non_scenario_controls"] == {"CT19-STOCK": "passed"}
        and report["stack_shutdown"]
        and all(
            end["postgres_removed"] and end["proxy_closed"] for end in report["stack_shutdown"]
        ),
        "CT19 terminal attribution or cleanup mismatch",
    )
    for case, status, code in (
        ("CT19:HP:removed", 404, "invalid_item"),
        ("CT19:HP:stock_zero", 409, "out_of_stock"),
    ):
        control = report["controls"][case]
        facts = control["observations"]
        offer_id = facts["catalog_before"]["row"]["offer_id"]
        changed = facts["catalog_changed"]["row"]
        calls = [
            c
            for c in facts["acp_calls"]
            if c["method"] == "POST" and c["path"] == "/checkout_sessions"
        ]
        require(
            control["stimulus_verified"] is True
            and len(calls) == facts["create_posts"] == 1
            and calls[0]["status"] == status
            and calls[0]["fault"] == "pass"
            and calls[0]["response"]["detail"]["code"] == facts["agent_result"] == code
            and calls[0]["request"]["line_items"] == [{"id": offer_id}]
            and facts["pre_mutation_agent"]["d1"] == [["decision_record", "allow"]]
            and facts["d1_observed_at_ns"] < facts["mutation_verified_at_ns"]
            and facts["agent_after"]["purchase"] == [["failed", offer_id, None, None, code]]
            and facts["agent_after"]["dispatch"]
            == [["definitive_failure", None, "VERIFIED_NOT_CREATED"]]
            and (
                changed is None
                if status == 404
                else changed["offer_id"] == offer_id
                and changed["snapshot"]["available_quantity"] == 0
            )
            and all(n == 0 for n in facts["agent_effect_counts"].values())
            and not facts["after_cleanup"]["checkouts"]
            and facts["psp_before"] == facts["psp_after"] == facts["psp_after_cleanup"]
            and facts["catalog_restored"]["row"] == facts["catalog_before"]["row"],
            f"CT19 native observation mismatch: {case}",
        )
    return len(verified), sum(count for _, count in verified.values())


def validate_not_applicable(root: Path, key: tuple[str, str], cell: dict) -> None:
    require(key == ("INF33", "HP"), "Unauthorized not_applicable pair")
    require(
        set(cell) == {"status", "justification", "source"}
        and isinstance(cell["justification"], str)
        and cell["justification"].strip(),
        "not_applicable requires justification and no execution evidence",
    )
    require(cell["source"] == INF33_SOURCE, "Unapproved not_applicable source")
    path = root / INF33_SOURCE["path"]
    require(preregistration_digest(path) == INF33_SOURCE["sha256"], "not_applicable source hash")
    section = path.read_text(encoding="utf-8").split("\n## 4. Celdas y oráculos\n", 1)[1]
    require(
        "**INF33-HP no aplica:** en HP no hay revisor LLM; la aprobación la hace el comprador."
        in section.split("\n## ", 1)[0],
        "Source does not declare INF33 HP not applicable",
    )


def junit_outcome(node: ElementTree.Element) -> str:
    for outcome, tag in (("failed", "failure"), ("error", "error"), ("skipped", "skipped")):
        if node.find(tag) is not None:
            return outcome
    return "passed"


def junit_test(node: ElementTree.Element) -> str:
    """The pytest node id of a JUnit test case of a module-level test function."""
    return node.get("classname", "").replace(".", "/") + ".py::" + node.get("name", "")


def ci_outcomes(
    data: bytes, cells: dict[str, Any], row: dict[str, Any], key: str
) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for node in ElementTree.fromstring(data).iter("testcase"):
        test = junit_test(node)
        require(test not in outcomes, f"Local CI JUnit mismatch: {key}/{test}")
        outcomes[test] = junit_outcome(node)
    listed = [test for cell in cells.values() for test in cell["tests"]]
    require(
        len(listed) == len(set(listed))
        and set(outcomes) == set(listed)
        and junit_counts(data) == row["ci_junit_counts"]
        and row["ci_junit_counts"]["skipped"] == 0,
        f"Local CI JUnit mismatch: {key}",
    )
    return outcomes


def validate_local_run(
    root: Path, key: str, row: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], set[str], dict[str, str]]:
    root = root.resolve()
    require(
        set(row) - LOCAL_RUN_CI_KEYS == LOCAL_RUN_KEYS and row["kind"] in LOCAL_RUN_KINDS,
        f"Invalid local run: {key}",
    )
    protocol_path = repo_path(root, row["protocol"], "evaluation/scenarios")
    require(
        preregistration_digest(protocol_path) == row["protocol_sha256"],
        f"Local protocol hash: {key}",
    )
    protocol = load(protocol_path)
    ci_cells = protocol.get("ci_cells", {})
    require(
        set(row) == LOCAL_RUN_KEYS | (LOCAL_RUN_CI_KEYS if ci_cells else set()),
        f"Invalid local run: {key}",
    )
    ledger = repo_path(root, row["checksum_index"], "evaluation/results")
    require(ledger.name == "SHA256SUMS", f"Invalid checksum index: {key}")
    entries = checksums(ledger.read_bytes())
    for name, expected in entries.items():
        path = (ledger.parent / name).resolve()
        require(
            path.is_relative_to(ledger.parent) and digest(path.read_bytes()) == expected,
            f"Local checksum: {key}/{name}",
        )
    require(
        entries.get(row["summary"]) == row["summary_sha256"]
        and entries.get(row["junit"]) == row["junit_sha256"],
        f"Local evidence hash: {key}",
    )
    ci: dict[str, str] = {}
    if ci_cells:
        require(
            entries.get(row["ci_junit"]) == row["ci_junit_sha256"], f"Local evidence hash: {key}"
        )
        ci = ci_outcomes((ledger.parent / row["ci_junit"]).read_bytes(), ci_cells, row, key)
    summary = json.loads(
        (ledger.parent / row["summary"]).read_bytes(), object_pairs_hook=unique_object
    )
    junit = (ledger.parent / row["junit"]).read_bytes()
    spec, checkouts = protocol["spec"], summary["checkouts"]
    require(
        summary["protocol"] == {"path": row["protocol"], "sha256": row["protocol_sha256"]}
        and summary["spec"] == {**spec, "matches_protocol": True}
        and preregistration_digest(repo_path(root, spec["path"], "docs/design")) == spec["sha256"]
        and set(checkouts) == {"agent", "merchant", "payment"}
        and all(checkout["dirty"] is False for checkout in checkouts.values())
        and row["sources"] == {name: checkout["head"] for name, checkout in checkouts.items()}
        and re.fullmatch(r"[0-9a-f]{40}", row["sources"]["agent"])
        and row["sources"]["merchant"] == protocol["merchant_sha"]
        and row["sources"]["payment"] == protocol["payment_sha"]
        and summary["checkouts_match_protocol"] == {"merchant": True, "payment": True}
        and summary["model"]["real_provider_calls"] == 0,
        f"Local run provenance mismatch: {key}",
    )
    if row["kind"] == "measurement":
        require(
            summary["execution_order"]
            == {"observed": protocol["execution_order"], "matches_protocol": True},
            f"Local execution order mismatch: {key}",
        )
    cases, repetitions = protocol["cases"], range(1, protocol["repetitions"] + 1)
    outcomes: dict[str, str] = {}
    for node in ElementTree.fromstring(junit).iter("testcase"):
        parameter = node.get("name", "").partition("[")[2].removesuffix("]")
        require(parameter not in outcomes, f"Duplicate JUnit case: {key}/{parameter}")
        outcomes[parameter] = junit_outcome(node)
    cells = summary["cells"]
    require(
        set(cells) == set(cases)
        and all(
            (cells[case_id]["matrix_id"], cells[case_id]["mode"])
            == (case["matrix_id"], case["mode"])
            and [run["status"] for run in cells[case_id]["runs"]]
            == [outcomes.get(f"{case_id}-r{n}") for n in repetitions]
            for case_id, case in cases.items()
        )
        and set(outcomes) == {f"{case_id}-r{n}" for case_id in cases for n in repetitions}
        and junit_counts(junit) == row["junit_counts"]
        and row["junit_counts"]["skipped"] == 0
        and summary["totals"]
        == {
            "cases": len(outcomes),
            **{outcome: row["junit_counts"][outcome] for outcome in ("passed", "failed", "error")},
        },
        f"Local JUnit/summary mismatch: {key}",
    )
    return protocol, summary, {f"{row['checksum_index']}:{name}" for name in entries}, ci


def matrix_case(case_id: str, case: dict[str, Any]) -> bool:
    # A spec names a matrix cell `<scenario>-<mode>` or a variant of it, lettered (INF33-HNP-a) or
    # named (UX09-HP-race, UX09-HNP-sel-kill); the `-control` cases and CTRL-* back the
    # measurement but are not matrix cells.
    pattern = rf"{case['matrix_id']}-{case['mode']}(-[a-z]+)*"
    return (
        case["matrix_id"] is not None
        and re.fullmatch(pattern, case_id) is not None
        and not case_id.endswith("-control")
    )


def validate_local_cell(
    key: tuple[str, str],
    cell: dict[str, Any],
    local_runs: dict[str, Any],
    verified_local: dict[str, tuple[dict[str, Any], dict[str, Any], set[str], dict[str, str]]],
) -> None:
    scenario, mode = key
    require(
        set(cell) - {"ci_cases"} == LOCAL_CELL_KEYS
        and cell["status"] == "passed"
        and cell["coverage"] == STATUS_COVERAGE["passed"]
        and cell["scope"].strip()
        and cell["limitation"].strip(),
        f"Inconsistent local evidence: {scenario}/{mode}",
    )
    run = cell["local_run"]
    require(
        run in verified_local and local_runs[run]["kind"] == "measurement",
        f"No attributable local evidence: {scenario}/{mode}",
    )
    protocol, summary, _, ci = verified_local[run]
    cases, ci_cells = protocol["cases"], protocol.get("ci_cells", {})
    attributed = sorted(
        case_id
        for case_id, case in cases.items()
        if (case["matrix_id"], case["mode"]) == key and matrix_case(case_id, case)
    )
    controls = sorted(
        case_id
        for case_id, case in cases.items()
        if case["mode"] == mode and not matrix_case(case_id, case)
    )
    ci_attributed = sorted(
        cell_id
        for cell_id, ci_cell in ci_cells.items()
        if (ci_cell["matrix_id"], ci_cell["mode"]) == key and matrix_case(cell_id, ci_cell)
    )
    ci_controls = sorted(
        cell_id
        for cell_id, ci_cell in ci_cells.items()
        if ci_cell["mode"] == mode and not matrix_case(cell_id, ci_cell)
    )
    # Each kind of evidence brings its own positive control: live cells the mode's live controls,
    # CI cells the mode's CI controls.
    require(
        (attributed or ci_attributed)
        and (controls or not attributed)
        and (ci_controls or not ci_attributed)
        and cell["cases"] == attributed
        and cell.get("ci_cases", []) == ci_attributed,
        f"No attributable local evidence: {scenario}/{mode}",
    )
    for cell_id in ci_attributed + ci_controls:
        require(
            all(ci[test] == "passed" for test in ci_cells[cell_id]["tests"]),
            f"Not passed in CI: {cell_id} for {scenario}/{mode}",
        )
    # Every cell of the pair and every positive control of its mode passed every repetition.
    full = f"{protocol['repetitions']}/{protocol['repetitions']}"
    for case_id in attributed + controls:
        result = summary["cells"][case_id]
        require(
            result["repetitions_passed"] == full
            and result["oracles"]
            and set(result["oracles"].values()) == {full}
            and [
                (rep["repetition"], rep["status"], rep["failed_oracles"]) for rep in result["runs"]
            ]
            == [(n, "passed", []) for n in range(1, protocol["repetitions"] + 1)],
            f"Not passed in every repetition: {case_id} for {scenario}/{mode}",
        )
    prior_attempts = cell["prior_attempts"]
    require(
        len(set(prior_attempts)) == len(prior_attempts),
        f"Duplicate prior attempt: {scenario}/{mode}",
    )
    for prior in prior_attempts:
        require(
            prior != run
            and prior in verified_local
            and local_runs[prior]["experiment"] == local_runs[run]["experiment"]
            and set(attributed) <= set(verified_local[prior][0]["cases"]),
            f"Unrelated prior attempt: {scenario}/{mode}",
        )


def validate(root: Path, observed: dict[str, Any] | None = None) -> dict[str, Any]:
    root = root.resolve()
    matrix_path = root / P0 / "matrix.json"
    oracle_path = root / P0 / "ux03-oracle.json"
    matrix, oracle = load(matrix_path), load(oracle_path)
    data: dict[str, Any] = (
        load(root / P0 / "matrix-observed.json") if observed is None else observed
    )
    matrix_sha = preregistration_digest(matrix_path)
    oracle_sha = preregistration_digest(oracle_path)
    require(
        data["schema_version"] == 1
        and data["preregistration"] == matrix_path.name
        and data["preregistration_sha256"] == matrix_sha,
        "Preregistration mismatch",
    )
    plan = {(row["id"], mode) for row in matrix["scenarios"] for mode in row["modes"]}
    preregistered_cells = planned_cells(matrix, oracle)
    runs = data["runs"]
    verified = {
        run: validate_run(root, run, row, matrix_sha, oracle_sha) for run, row in runs.items()
    }
    local_runs = data.get("local_runs", {})
    verified_local = {run: validate_local_run(root, run, row) for run, row in local_runs.items()}
    statuses = dict.fromkeys(plan, "not_executed")
    ct19_runs, ct19_hashes = 0, 0
    for scenario, row in data["observations"].items():
        require(row["modes"], f"Missing modes: {scenario}")
        for mode, cell in row["modes"].items():
            key = (scenario, mode)
            require(key in plan, f"Mode outside P0: {scenario}/{mode}")
            if cell == {"status": "not_executed"}:
                continue
            status = cell["status"]
            if status == "not_applicable":
                validate_not_applicable(root, key, cell)
                require(
                    not any(
                        (case["scenario"], case["mode"]) == key
                        for protocol, _, _ in verified.values()
                        for case in protocol["cases"].values()
                    )
                    and not any(
                        (case["matrix_id"], case["mode"]) == key
                        for protocol, *_ in verified_local.values()
                        for case in [
                            *protocol["cases"].values(),
                            *protocol.get("ci_cells", {}).values(),
                        ]
                    ),
                    "not_applicable cannot hide a registered execution",
                )
                statuses[key] = status
                continue
            if key == ("CT19", "HP"):
                ct19_runs, ct19_hashes = validate_ct19(root, cell, matrix_sha, oracle_sha)
                statuses[key] = status
                continue
            if "local_run" in cell:
                validate_local_cell(key, cell, local_runs, verified_local)
                statuses[key] = status
                continue
            require(
                STATUS_COVERAGE.get(status) == cell["coverage"]
                and cell["scope"].strip()
                and cell["limitation"].strip(),
                f"Inconsistent status/scope: {scenario}/{mode}",
            )
            run = str(cell.get("run_id"))
            require(run in verified, f"No attributable evidence: {scenario}/{mode}")
            protocol, report, hashes = verified[run]
            if protocol.get("schema") == "inf31-hp-reassessment/1":
                history = cell.get("historical_observation", {})
                original = str(protocol["historical"]["run_id"])
                require(
                    key == ("INF31", "HP")
                    and status == "passed"
                    and isinstance(history, dict)
                    and str(history.get("run_id")) == original
                    and history.get("status") == protocol["historical"]["status"] == "blocked"
                    and history.get("coverage") == "incomplete"
                    and history.get("scope", "").strip()
                    and history.get("limitation", "").strip()
                    and original in verified
                    and runs[original]["sha256"] == protocol["historical"]["archive_sha256"]
                    and runs[original]["protocol_sha256"]
                    == protocol["historical"]["protocol_sha256"]
                    and verified[original][1]["scenarios"]["INF31"] == "error"
                    and verified[original][1]["validated"] is False,
                    "Missing or incompatible historical INF31 observation",
                )
                attempts = history.get("prior_attempts", [])
                require(
                    bool(attempts)
                    and len(attempts) == len(set(attempts))
                    and all(
                        str(attempt) in verified
                        and str(attempt) != original
                        and runs[str(attempt)]["experiment"] == runs[original]["experiment"]
                        for attempt in attempts
                    ),
                    "Invalid prior attempts for the historical INF31 observation",
                )
            case_ids = [
                case_id
                for case_id, case in protocol["cases"].items()
                if case["scenario"] == scenario and case["mode"] == mode
            ]
            require(
                runs[run]["kind"] == "measurement" and hashes and case_ids,
                f"No attributable evidence: {scenario}/{mode}",
            )
            require(
                report["attribution_status"] == "verified"
                and all(
                    report["controls"].get(case_id, {}).get("cleanup_verified") is True
                    for case_id in case_ids
                )
                and report["stack_shutdown"]
                and all(
                    end["postgres_removed"] and end["proxy_closed"]
                    for end in report["stack_shutdown"]
                ),
                f"Missing measured mode or cleanup: {scenario}/{mode}",
            )
            raw_status = report["scenarios"][scenario]
            accepted = {
                "passed": {"passed"},
                "partial": {"passed"},
                "failed": {"failed"},
                "blocked": {"blocked", "error"},
                "harness_error": {"error"},
                "not_executed": {"not_executed"},
            }[status]
            require(raw_status in accepted, f"Outcome mismatch: {scenario}/{mode}")
            if status in {"passed", "partial", "failed"}:
                require(
                    report["attributed_scenarios"].get(scenario) == raw_status,
                    f"Scenario lacks verified attribution: {scenario}/{mode}",
                )
            require(
                len(set(cell["prior_attempts"])) == len(cell["prior_attempts"]),
                f"Duplicate prior attempt: {scenario}/{mode}",
            )
            for attempt in cell["prior_attempts"]:
                prior = str(attempt)
                require(
                    prior != run
                    and runs[prior]["experiment"] == runs[run]["experiment"]
                    and any(
                        case["scenario"] == scenario and case["mode"] == mode
                        for case in verified[prior][0]["cases"].values()
                    ),
                    f"Unrelated prior attempt: {scenario}/{mode}",
                )
            statuses[key] = status
    by_status = Counter(statuses.values())
    return {
        "runs": len(runs) + ct19_runs,
        "internal_hashes": sum(info[2] for info in verified.values()) + ct19_hashes,
        "local_runs": len(local_runs),
        "local_hashes": len(set().union(*(info[2] for info in verified_local.values()))),
        "denominators": {
            "scenarios": len(matrix["scenarios"]),
            "scenario_modes": len(plan),
            "measurable_scenario_modes": len(plan) - by_status["not_applicable"],
            "preregistered_cells": len(preregistered_cells),
        },
        "scenario_modes": {status: by_status[status] for status in STATUS_COVERAGE},
        "distinct_scenarios_by_status": {
            status: len({scenario for (scenario, _), value in statuses.items() if value == status})
            for status in STATUS_COVERAGE
        },
        "not_executed": sorted(
            f"{scenario}/{mode}"
            for (scenario, mode), value in statuses.items()
            if value == "not_executed"
        ),
        "not_applicable": sorted(
            f"{scenario}/{mode}"
            for (scenario, mode), value in statuses.items()
            if value == "not_applicable"
        ),
        "scenarios_without_execution": sorted(
            {scenario for scenario, _ in plan}
            - {
                scenario
                for (scenario, _), status in statuses.items()
                if status not in {"not_executed", "not_applicable"}
            }
        ),
    }


if __name__ == "__main__":
    print(json.dumps(validate(Path.cwd()), indent=2))
