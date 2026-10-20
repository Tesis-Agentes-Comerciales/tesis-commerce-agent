"""HNP-02 monitor projection: versioned dry-run, shared schema and fail-closed reading.

No harness, provider, merchant or network: the run under test is a versioned FakeTransport
dry-run, and every tampered variant lives in a temporary directory.
"""

import importlib.util
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "monitor_projection_hnp02", ROOT / "scripts/export_governance_monitor.py"
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
MONITOR = ROOT / "docs/governance-monitor"
SCHEMA = json.loads((MONITOR / "governance-monitor-3.schema.json").read_text(encoding="utf-8"))
(RUN,) = sorted((MONITOR / "sources/hnp02").iterdir())
# The bare `python -m unittest` route has no third-party packages; `uv run pytest` does.
HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None
Mutation = Callable[[dict[str, Any], list[dict[str, Any]]], None]


def load_run() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary = json.loads((RUN / "summary.json").read_text(encoding="utf-8"))
    lines = (RUN / "traces.jsonl").read_text(encoding="utf-8").splitlines()
    return summary, [json.loads(line) for line in lines]


def write_run(directory: Path, summary: dict[str, Any], traces: list[dict[str, Any]]) -> Path:
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (directory / "traces.jsonl").write_text(
        "".join(json.dumps(t) + "\n" for t in traces), encoding="utf-8"
    )
    return directory


def project(mutate: Mutation | None = None) -> dict[str, Any]:
    summary, traces = load_run()
    if mutate is not None:
        mutate(summary, traces)
    with tempfile.TemporaryDirectory() as temp:
        return module.hnp02(module.read_hnp02_run(write_run(Path(temp), summary, traces)))


def as_provider(summary: dict[str, Any], traces: list[dict[str, Any]]) -> None:
    """The same run relabelled consistently as a provider run, as llm_live would record it."""

    def relabel(actors: list[dict[str, Any]]) -> None:
        for actor in actors:
            if actor["role"] == "validator":
                actor["transport"] = "openai-responses"

    summary["experiment"]["execution_kind"] = "llm_live"
    relabel(summary["actors"])
    cost = summary["cost"]
    cost.update(basis="provider_reported_usage", provider_calls=cost["llm_calls"], fake_calls=0)
    for trace in traces:
        trace["execution_kind"] = "llm_live"
        relabel(trace["actors"])
        for call in trace["calls"]:
            call["transport"] = "openai-responses"


def accepted(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """An arm B trace whose validator call returned a parsed accept."""
    return next(t for t in traces if t["result"]["validator_verdict"] == "accept")


def keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(v) for v in value))
    return set()


def schema_errors(value: Any) -> list[str]:
    from jsonschema import Draft202012Validator

    return [error.message for error in Draft202012Validator(SCHEMA).iter_errors(value)]


class Hnp02ProjectionTests(unittest.TestCase):
    def assertRejected(self, mutate: Mutation, message: str = "HNP-02 run rejected") -> None:
        with self.assertRaisesRegex(ValueError, message):
            project(mutate)

    def test_versioned_dry_run_is_the_bundled_experiment(self):
        bundled = json.loads(module.DATA.read_bytes())
        experiment = module.hnp02(module.read_hnp02_run(RUN))
        self.assertEqual(bundled["experiments"], [experiment])
        for item in experiment["evidence"]:
            self.assertEqual(bundled["sourcePins"][item["artifact"]], item["sha256"])
        # Re-serialised bytes change the pins, never the projected facts.
        rewritten = project()
        self.assertNotEqual(rewritten["evidence"], experiment["evidence"])
        self.assertEqual({**rewritten, "evidence": None}, {**experiment, "evidence": None})

    def test_dry_run_is_labelled_simulated_and_never_an_operation(self):
        e = module.hnp02(module.read_hnp02_run(RUN))
        self.assertEqual((e["executionKind"], e["transport"]), ("llm validator dry-run", "fake"))
        self.assertEqual(
            (e["sink"], e["operationalEffects"], e["commercialPosts"]),
            ("fictitious_in_memory", False, 0),
        )
        self.assertEqual(e["cost"]["basis"], "fake_transport_estimate")
        self.assertEqual(e["cost"]["providerCalls"], 0)
        self.assertEqual(e["cost"]["fakeCalls"], e["cost"]["llmCalls"])
        self.assertEqual([a["transport"] for a in e["actors"]], ["none", "fake"])
        self.assertTrue(any("Transporte simulado" in note for note in e["notes"]))
        self.assertEqual(e["comparison"]["all"]["onlyABlocked"], 0)
        self.assertEqual(len(e["cases"]), len(load_run()[0]["experiment"]["business_cases"]))

    def test_provider_run_is_distinguished_from_the_dry_run(self):
        e = project(as_provider)
        self.assertEqual(
            (e["executionKind"], e["transport"]), ("llm validator provider", "provider")
        )
        self.assertEqual(e["cost"]["basis"], "provider_reported_usage")
        self.assertEqual(e["actors"][1]["transport"], "openai-responses")
        self.assertIn("proveedor", e["label"])
        self.assertFalse(any("Transporte simulado" in note for note in e["notes"]))

    def test_projection_keeps_no_model_prose_prompt_settings_or_environment(self):
        summary, traces = load_run()
        source = json.dumps([summary, traces])
        projected = json.dumps(module.hnp02(module.read_hnp02_run(RUN)), ensure_ascii=False)
        prompt = (
            (ROOT / "src/commerce_agent/hnp_experiment/llm/prompts/validator-v1.md")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        environment = summary["experiment"]["environment"]
        for value in ("Dry-run answer", environment["platform"], ".local/", "cases_path"):
            self.assertIn(value, source)
            self.assertNotIn(value, projected)
        for value in (prompt, environment["python"], "api_key"):
            self.assertNotIn(value, projected)
        experiment = module.hnp02(module.read_hnp02_run(RUN))
        dropped = {"call_id", "reasons", "review", "record_digest", "settings", "environment"}
        self.assertEqual(keys(experiment) & dropped, set())
        counts = [c["sample"]["B"]["validator"]["reasonCount"] for c in experiment["cases"]]
        self.assertTrue(
            all(n is None or isinstance(n, int) for n in counts) and max(n or 0 for n in counts)
        )

    def test_unknown_formats_and_vocabularies_fail_closed(self):
        mutations: dict[str, Mutation] = {
            "summary schema": lambda s, t: s.update(schema="hnp-llm-run/4"),
            "top-level field": lambda s, t: s.update(extra=1),
            "settings field": lambda s, t: s["experiment"]["settings"].update(openai_api_key="x"),
            "execution kind": lambda s, t: s["experiment"].update(execution_kind="llm_replay"),
            "real sink": lambda s, t: s["experiment"].update(sink="merchant"),
            "operational effects": lambda s, t: s["experiment"].update(operational_effects=True),
            "trace schema": lambda s, t: t[0].update(schema_version="hnp-llm-trace/2"),
            "trace field": lambda s, t: t[0].update(prompt="text"),
            "call field": lambda s, t: accepted(t)["calls"][0].update(raw_output="{}"),
            "call status": lambda s, t: accepted(t)["calls"][0].update(status="partial"),
            "admission code": lambda s, t: s["episodes"][0].update(admission_code="APPROVED"),
            "actor kind": lambda s, t: s["actors"][0].update(kind="human"),
            "check result": lambda s, t: accepted(t)["calls"][0]["review"]["checks"].update(
                budget="ok"
            ),
            "boolean as count": lambda s, t: s["arms"]["A"].update(simulated_effects=True),
            "repeated backend failure": lambda s, t: s["episodes"][0].update(
                backend_failures=["budget", "budget"]
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                self.assertRejected(mutate)

    def test_transport_must_match_the_execution_kind(self):
        def provider_actor_in_dry_run(
            summary: dict[str, Any], traces: list[dict[str, Any]]
        ) -> None:
            as_provider(summary, traces)
            summary["experiment"]["execution_kind"] = "llm_dry_run"
            for trace in traces:
                trace["execution_kind"] = "llm_dry_run"

        def fake_basis_in_live_run(summary: dict[str, Any], traces: list[dict[str, Any]]) -> None:
            as_provider(summary, traces)
            summary["cost"]["basis"] = "fake_transport_estimate"

        self.assertRejected(provider_actor_in_dry_run, "transport")
        self.assertRejected(fake_basis_in_live_run, "cost")

    def test_summary_numbers_are_bound_to_the_traces(self):
        def flip_effect(summary: dict[str, Any], traces: list[dict[str, Any]]) -> None:
            row = next(r for r in summary["episodes"] if r["arm"] == "A" and not r["effect"])
            row["effect"] = True

        def drop_trace(summary: dict[str, Any], traces: list[dict[str, Any]]) -> None:
            traces.pop()

        mutations: dict[str, Mutation] = {
            "episode effect": flip_effect,
            "missing trace": drop_trace,
            "arm effects": lambda s, t: s["arms"]["B"].update(simulated_effects=99),
            "captures": lambda s, t: s["arms"]["B"]["validator"]["captures"].update(k=0),
            "wilson rate": lambda s, t: s["arms"]["A"]["false_blocks"].update(rate=0.5),
            "calls": lambda s, t: s["cost"].update(llm_calls=0),
            "mcnemar": lambda s, t: s["comparison"]["all"].update(mcnemar_exact_p=0.01),
            "trace verdict": lambda s, t: accepted(t)["result"].update(validator_verdict="reject"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                self.assertRejected(mutate)

    def test_arm_b_never_admits_what_arm_a_blocked(self):
        def admitted_only_in_b(summary: dict[str, Any], traces: list[dict[str, Any]]) -> None:
            table = summary["comparison"]["unsafe"]
            table.update(both_blocked=table["both_blocked"] - 1, only_a_blocked=1)

        self.assertRejected(admitted_only_in_b, "arm B admitted what arm A blocked")

    def test_secret_like_or_personal_strings_reject_the_whole_run(self):
        def reason(value: str) -> Mutation:
            return lambda s, t: accepted(t)["calls"][0]["review"]["reasons"].append(value)

        for value in (
            "sk-proj-" + "x" * 24,
            "Bearer synthetic-test-only",
            "fixture@example.invalid",
        ):
            with self.subTest(value):
                self.assertRejected(reason(value), "secret-like")

    def test_duplicate_keys_and_non_finite_numbers_are_rejected(self):
        raw = (RUN / "summary.json").read_text(encoding="utf-8")
        documents = {
            "duplicate key": raw.replace('"schema":', '"schema": "x", "schema":', 1),
            "NaN": raw.replace('"rate": 0.0', '"rate": NaN', 1),
        }
        for name, document in documents.items():
            self.assertNotEqual(document, raw)
            with self.subTest(name), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                (directory / "summary.json").write_text(document, encoding="utf-8")
                (directory / "traces.jsonl").write_bytes((RUN / "traces.jsonl").read_bytes())
                with self.assertRaisesRegex(ValueError, "HNP-02 run rejected"):
                    module.hnp02(module.read_hnp02_run(directory))

    def test_exporter_vocabularies_match_the_shared_schema(self):
        defs = SCHEMA["$defs"]
        episode = defs["episode"]["properties"]
        validator = episode["validator"]["properties"]
        pairs = [
            (episode["admissionCode"]["enum"], module.ADMISSION_CODES),
            (defs["checkId"]["enum"], module.CHECK_IDS),
            (defs["caseKind"]["enum"], module.CASE_KINDS),
            (validator["outcome"]["enum"], module.VALIDATOR_OUTCOMES),
            (validator["callStatus"]["enum"], (*module.CALL_STATUSES, None)),
            (validator["nonPassChecks"]["propertyNames"]["enum"], module.BUSINESS_CHECKS),
            (
                defs["experiment"]["properties"]["executionKind"]["enum"],
                [kind for kind, *_ in module.HNP02_KINDS.values()],
            ),
        ]
        for schema_values, exporter_values in pairs:
            with self.subTest(exporter_values[0]):
                self.assertEqual(set(schema_values), set(exporter_values))

    def test_projection_opens_no_socket(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            self.assertEqual(module.hnp02(module.read_hnp02_run(RUN))["transport"], "fake")


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is installed by `uv sync`, not by bare Python")
class SharedSchemaTests(unittest.TestCase):
    def test_schema_is_valid_draft_2020_12(self):
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(SCHEMA)
        self.assertEqual(SCHEMA["properties"]["schema"]["const"], "governance-monitor/3")

    def test_bundled_monitor_and_both_transports_satisfy_the_schema(self):
        bundle = json.loads(module.DATA.read_bytes())
        self.assertEqual(schema_errors(bundle), [])
        self.assertEqual(schema_errors({**bundle, "experiments": [project(as_provider)]}), [])

    def test_schema_rejects_unknown_fields_and_mismatched_transport(self):
        bundle = json.loads(module.DATA.read_bytes())
        experiment = bundle["experiments"][0]
        variants = {
            "fake kind, provider transport": {"transport": "provider"},
            "provider kind, fake transport": {"executionKind": "llm validator provider"},
            "unknown kind": {"executionKind": "live llm"},
            "unknown field": {"rawTraces": []},
            "operational effects": {"operationalEffects": True},
            "provider cost basis in a dry-run": {
                "cost": {**experiment["cost"], "basis": "provider_reported_usage"}
            },
        }
        for name, patch_ in variants.items():
            with self.subTest(name):
                variant = {**experiment, **patch_}
                self.assertNotEqual(schema_errors({**bundle, "experiments": [variant]}), [])
        self.assertNotEqual(schema_errors({**bundle, "schema": "governance-monitor/2"}), [])


if __name__ == "__main__":
    unittest.main()
