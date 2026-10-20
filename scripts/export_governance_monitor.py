"""Read-only projection of pinned historical artifacts and HNP-02 run directories.

No application imports, harness execution, model calls or network.
"""

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "ui/src/features/governance-monitor/evidence.json"
SOURCES = ROOT / "docs/governance-monitor/sources"
AGENT_COMMIT = "83510f8dd5fbe5026d909df2b4949802b8b2aa1a"
HNP_COMMIT = "a633a043337286406f9a3e859809556ce46bcba1"
PINS = {
    "agent-received.json": "b89c3252739dd947323106ffe8e36f44ed4a840564d33a2be18087ded7c34cfe",
    "agent-lost.json": "30bdb6a2e544d38eb1d8870e886d539e4fdaf774f1a6e08f57fdf266f4b66ce7",
    "http-deny.json": "b287bbf5d4386a36627f7c1b988bfa477d1268cf961c32b2c7933aa39d8bef03",
    "results.json": "9194ec79aab8c74b31648655744f325991a20fd12045a10dff66f4a0401c4f33",
    "evidence.jsonl": "203961b809fd1c1bf7cb441d746ff7d689eb348c9cbabfd15447d13e37eff75d",
    "summary.json": "030118bdd5ce423ad4d40719e4b787c1a057831a90a7284907e985289d961455",
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(domain: str, value: Any) -> str:
    return "sha256:" + hashlib.sha256(domain.encode("ascii") + b"\0" + canonical(value)).hexdigest()


def read_pinned(path: Path, name: str) -> Any:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PINS[name]:
        raise ValueError(f"Historical artifact hash mismatch: {name}")
    return json.loads(raw)


def verify_hnp(results: list[dict[str, Any]], ledger_path: Path) -> int:
    raw = ledger_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PINS["evidence.jsonl"]:
        raise ValueError("HNP ledger hash mismatch")
    entries = [json.loads(line) for line in raw.splitlines()]
    previous = None
    records: dict[str, dict[str, Any]] = {}
    ledger_records: dict[str, dict[str, Any]] = {}
    receipts: set[str] = set()
    for number, entry in enumerate(entries, 1):
        body = {key: value for key, value in entry.items() if key != "entry_digest"}
        if (
            entry["sequence"] != number
            or entry["previous_digest"] != previous
            or entry["entry_digest"] != digest("hnp-evidence-entry/v1", body)
        ):
            raise ValueError("HNP evidence chain mismatch")
        previous = entry["entry_digest"]
        payload = entry["payload"]
        if payload.get("event_kind") == "simulated_effect":
            record = ledger_records.get(payload["record_digest"])
            result_digest = digest("hnp-review-record/v1", record) if record is not None else None
            if (
                record is None
                or record["admission"]["status"] != "simulated_admitted"
                or record["proposal"]["proposal_id"] != payload["proposal_id"]
                or result_digest in receipts
            ):
                raise ValueError("Unbound HNP receipt")
            assert result_digest is not None
            receipts.add(result_digest)
        else:
            records[digest("hnp-review-record/v1", payload)] = payload
            ledger_records[entry["entry_digest"]] = payload
    for result in results:
        if (
            result["record_digest"] != digest("hnp-review-record/v1", result["record"])
            or records.get(result["record_digest"]) != result["record"]
        ):
            raise ValueError("HNP result not bound to ledger")
        if result["effect_occurred"] != (result["record_digest"] in receipts):
            raise ValueError("HNP effect/receipt mismatch")
    return len(entries)


def evidence(name: str, pointer: str, commit: str, record_digest: str | None) -> dict[str, Any]:
    return {
        "artifact": name,
        "sha256": PINS[name],
        "pointer": pointer,
        "codeCommit": commit,
        "recordDigest": record_digest,
    }


def phase(
    outcome: str | None,
    decision_id: str,
    evidence_id: str,
    evidence_digest: str,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "decisionId": decision_id,
        "evidenceId": evidence_id,
        "digest": evidence_digest,
        "reason": reason,
    }


def hp_label(enforce_outcome: str | None, uncertain: bool) -> str:
    """A commercial POST is not a permission; only an explicit applied allow reads as one."""
    if uncertain:
        return "HP · respuesta perdida"
    return "HP · permitido por D1" if enforce_outcome == "allow" else "HP · creación observada"


def hp(value: dict[str, Any], uncertain: bool = False) -> dict[str, Any]:
    guard = value["guard"]
    shadow = guard["shadow_binding"]
    proof = value["recovered"]["proof"]
    creation = proof["creation"]
    response = creation["original_response"]
    assert value["post_commercial"] == 1 and value["recovered"]["result"] == "found"
    assert (value["initial"]["result"] == "found") != uncertain
    assert shadow["governance_mode"] == "shadow" and shadow["dispatch_capability"] is False
    total = next(x["amount"] for x in response["totals"] if x["type"] == "total")
    name = "agent-lost.json" if uncertain else "agent-received.json"
    # The envelope keeps D1 references but not its verdict, so the applied outcome stays unknown.
    enforce = phase(None, guard["decision_id"], guard["evidence_id"], guard["evidence_digest"])
    return {
        "id": "hp-uncertain" if uncertain else "hp-created",
        "label": hp_label(enforce["outcome"], uncertain),
        "mode": "HP",
        "executionKind": "replay",
        "scope": "D17 HTTP/PG real histórico; monitor sin conexión comercial",
        "intent": None,
        "constraints": None,
        "proposal": guard["request"]["offer_id"],
        "proposedAction": f"{guard['method']} {guard['path']}",
        "runId": guard["run_id"],
        "operationId": guard["operation_id"],
        "requestId": creation["request_id"],
        "shadow": phase(
            shadow["outcome"],
            shadow["decision_id"],
            shadow["evidence_id"],
            shadow["evidence_digest"],
        ),
        "enforce": enforce,
        "validator": None,
        "backend": {
            "code": value["recovered"]["reason"],
            "failures": [],
            "executedPolicyIds": None,
        },
        "execution": "started",
        "creation": "uncertain" if uncertain else "confirmed",
        "recovery": "confirmed",
        "sourceKind": "historical_http_postgresql",
        "budgetLimitMinor": None,
        "commercialPosts": value["post_commercial"],
        "effectOccurred": True,
        "totalMinor": total,
        "currency": response["currency"],
        "notes": [
            "Texto original de intención y razones D1 no conservados en este artefacto.",
            "El envelope contiene referencias de D1 aplicada; no conserva el veredicto textual. "
            "Se muestra no disponible.",
            "Q shadow observa y no autoriza despacho.",
            "Confirmado significa creación histórica, nunca compra/pago completado.",
            "Timeout inicial y found posterior son observaciones distintas; no hubo reenvío."
            if uncertain
            else "La prueba histórica found confirma un checkout not_ready_for_payment; "
            "no habilita continuación.",
        ],
        "evidence": [
            evidence(
                name,
                "/guard; /initial; /recovered; /post_commercial",
                AGENT_COMMIT,
                guard["evidence_digest"],
            )
        ],
    }


def denied(value: dict[str, Any]) -> dict[str, Any]:
    applied = value["initial_evidence"]["applied"][0]
    shadow = value["initial_evidence"]["shadow"][0]
    assert applied[2:5] == ["deny", "enforce", "blocked"] and shadow[2:4] == ["allow", "shadow"]
    assert value["merchant_d17_posts"] == value["transport_d17_posts"] == 0
    assert value["effects_before"] == value["effects_after"] and value["guard"] is None
    return {
        "id": "hp-denied",
        "label": "HP · bloqueado por D1",
        "mode": "HP",
        "executionKind": "replay",
        "scope": "D1 enforce real con política deny temporal; P HTTP/PG histórico",
        "intent": None,
        "constraints": None,
        "proposal": None,
        "proposedAction": None,
        "runId": None,
        "operationId": None,
        "requestId": None,
        "shadow": phase(shadow[2], shadow[1], shadow[0], shadow[4]),
        "enforce": phase(applied[2], applied[1], applied[0], applied[5]),
        "validator": None,
        "backend": {"code": applied[4], "failures": [], "executedPolicyIds": None},
        "execution": "blocked",
        "creation": "not_applicable",
        "recovery": "not_applicable",
        "sourceKind": "historical_http_postgresql",
        "budgetLimitMinor": None,
        "commercialPosts": 0,
        "effectOccurred": False,
        "totalMinor": None,
        "currency": None,
        "notes": [
            "Q allow no vence D1 deny. No hubo guard ni claim.",
            "Código de razón, intención, producto y run_id no conservados: no disponible.",
            "Cero POST en registros independientes HTTPX y Merchant; contadores sin efectos.",
        ],
        "evidence": [
            evidence(
                "http-deny.json",
                "/initial_evidence/applied/0; /initial_evidence/shadow/0; "
                "/merchant_d17_posts; /transport_d17_posts",
                AGENT_COMMIT,
                applied[5],
            )
        ],
    }


def hnp(result: dict[str, Any]) -> dict[str, Any]:
    record = result["record"]
    context, proposal, assessment = (
        record["original_context"],
        record["proposal"],
        record["assessment"],
    )
    admission = result["effective_admission"]
    assert record["execution_kind"] == "scripted_offline" and record["executed_policy_ids"] == []
    assert (
        assessment["verdict"] == "accept"
        and admission["code"] == "BACKEND_DENIED"
        and admission["backend_failures"] == ["budget"]
    )
    assert not result["effect_occurred"]
    return {
        "id": "hnp-false-accept",
        "label": "HNP · accept erróneo contenido",
        "mode": "HNP",
        "executionKind": "scripted offline",
        "scope": "HNP-01, condición B; comprador y validador programados; sink ficticio",
        "intent": None,
        "constraints": json.dumps(context["intent"]["hard"], ensure_ascii=False, sort_keys=True),
        "proposal": proposal["draft"]["variant_id"],
        "proposedAction": proposal["draft"]["action"],
        "runId": context["episode_ref"]["id"],
        "operationId": None,
        "requestId": None,
        "shadow": None,
        "enforce": None,
        "validator": {
            "verdict": assessment["verdict"],
            "adapterId": assessment["validator_adapter_id"],
            "checks": [{"id": x["check_id"], "result": x["result"]} for x in assessment["checks"]],
        },
        "backend": {
            "code": admission["code"],
            "failures": admission["backend_failures"],
            "executedPolicyIds": record["executed_policy_ids"],
        },
        "execution": "blocked",
        "creation": "not_applicable",
        "recovery": "not_applicable",
        "sourceKind": "scripted_offline",
        "budgetLimitMinor": context["intent"]["hard"]["max_total_amount_minor"],
        "commercialPosts": 0,
        "effectOccurred": result["effect_occurred"],
        "totalMinor": context["terms"]["total_minor"],
        "currency": context["terms"]["currency"],
        "notes": [
            "Accept guionado e incorrecto; no inferencia LLM.",
            f"Total fixture {context['terms']['total_minor']} > límite fixture "
            f"{context['intent']['hard']['max_total_amount_minor']} unidades menores.",
            "Backend experimental ejecutó sus controles, no un PDP POL-03-10 ni D2/D3 operativo.",
            "No hubo efecto ni recibo ficticio; no es prueba de independencia estadística "
            "o mejora por dos LLM.",
        ],
        "evidence": [
            evidence(
                "results.json",
                "/record[experiment_id=validator-false-accept, condition=B]",
                HNP_COMMIT,
                result["record_digest"],
            ),
            evidence(
                "evidence.jsonl",
                "cadena completa: 65 entradas; ReviewRecord enlazado por digest",
                HNP_COMMIT,
                result["record_digest"],
            ),
        ],
    }


# HNP-02: one LLM-validator run (summary.json + traces.jsonl) projected as an experiment. The
# readers take exact key sets and closed vocabularies copied from the run's own contracts: an
# unknown field, kind, transport, status or code is an unreviewed format and fails the export
# instead of being dropped. Nothing here imports the harness that produced the run.
HNP02_SUMMARY = "hnp-llm-run/3-draft"
HNP02_TRACE = "hnp-llm-trace/1"
# Source execution kind -> monitor kind, transport, validator actor transport, cost basis.
HNP02_KINDS = {
    "llm_dry_run": ("llm validator dry-run", "fake", "fake", "fake_transport_estimate"),
    "llm_live": (
        "llm validator provider",
        "provider",
        "openai-responses",
        "provider_reported_usage",
    ),
}
BUSINESS_CHECKS = (
    "selection",
    "merchant",
    "budget",
    "offer",
    "consent",
    "validity",
    "revocation",
    "usage",
    "constraints",
    "scope",
)
CHECK_IDS = (*BUSINESS_CHECKS, "binding")
CHECK_RESULTS = ("pass", "fail", "unresolved")
CASE_KINDS = ("admissible", "changed_after_review", "invalid", "not_reviewable")
INFRASTRUCTURE_CODES = ("BUYER_UNAVAILABLE", "CONTEXT_UNAVAILABLE", "BACKEND_UNAVAILABLE")
INFRASTRUCTURE_DELIVERY = ("EVIDENCE_UNAVAILABLE", "SINK_UNAVAILABLE", "RECEIPT_UNAVAILABLE")
ADMISSION_CODES = (
    "SIMULATED_ONLY",
    "BUYER_INVALID",
    "VALIDATOR_UNAVAILABLE",
    "VALIDATOR_INVALID",
    "VALIDATOR_REJECTED",
    "BACKEND_DENIED",
    "CONTEXT_CHANGED",
    *INFRASTRUCTURE_CODES,
    *INFRASTRUCTURE_DELIVERY,
)
DELIVERY_CODES = ("NONE", "EMITTED", *INFRASTRUCTURE_DELIVERY)
ASSESSMENT_STATUSES = ("not_called", "admitted", "invalid", "timeout", "unavailable")
VERDICTS = ("accept", "reject", "abstain")
VALIDATOR_OUTCOMES = (*VERDICTS, "invalid", "timeout", "unavailable", "not_called")
REVIEWED_OUTCOMES = (*VERDICTS, "invalid", "timeout")
CALL_STATUSES = (
    "ok",
    "refusal",
    "incomplete",
    "schema_invalid",
    "inconsistent",
    "timeout",
    "transport_error",
)
INVALID_CALLS = ("refusal", "incomplete", "schema_invalid", "inconsistent")
LATENCY_COMPONENTS = ("resolver", "buyer", "validator", "backend", "evidence", "sink", "total")
DEVIATION_FIELDS = ("oracle", "admission_code", "effect", "backend_failures", "validator_verdict")
PAIR_OUTCOME = "simulated effect admitted (1) or blocked (0), per pair"
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
PROMPT = re.compile(r"[a-z]+-v[0-9]+")
ERROR_KIND = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
DECIMAL = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,12})?")
PREFIXED_SHA = re.compile(r"sha256:([a-f0-9]{64})")
# A secret-like or personal string anywhere in a run makes it unfit for a versioned projection.
SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-|ghp_|github_pat_|AIza)[A-Za-z0-9_-]{16,}"
    r"|Bearer\s+\S+|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.IGNORECASE,
)

RUN_KEYS = (
    "schema",
    "experiment",
    "actors",
    "arms",
    "comparison",
    "cost",
    "deviations",
    "episodes",
)
RUN_EXPERIMENT_KEYS = (
    "id",
    "execution_kind",
    "sink",
    "operational_effects",
    "seed",
    "repetitions",
    "business_cases",
    "excluded_cases",
    "phase",
    "created_at",
    "settings",
    "prompts",
    "cost_estimate",
    "corpus_sha256",
    "implementation_sha256",
    "environment",
)
# Read only to reject unknown fields (a key, say); settings are never projected wholesale.
SETTINGS_KEYS = (
    "validator_model",
    "buyer_model",
    "temperature",
    "reasoning_effort",
    "seed",
    "timeout_seconds",
    "max_output_tokens",
    "repetitions",
    "validator_input_usd_per_mtok",
    "validator_output_usd_per_mtok",
    "buyer_input_usd_per_mtok",
    "buyer_output_usd_per_mtok",
    "max_cost_usd",
    "validator_prompt",
    "buyer_prompt",
    "cases_path",
    "oracle_path",
    "output_dir",
    "live",
)
ACTOR_KEYS = (
    "role",
    "kind",
    "adapter_id",
    "transport",
    "model",
    "prompt_version",
    "prompt_sha256",
    "seed",
    "temperature",
    "reasoning_effort",
    "max_output_tokens",
    "timeout_seconds",
    "input_usd_per_mtok",
    "output_usd_per_mtok",
)
ARM_KEYS = (
    "episodes",
    "scored_episodes",
    "infrastructure_errors",
    "case_mix",
    "simulated_effects",
    "escapes",
    "false_blocks",
    "backend_blocks_on_invalid",
    "episode_latency_ms",
)
VALIDATOR_KEYS = (
    "reviewed",
    "outcomes",
    "captures",
    "captures_with_expected_checks",
    "unique_captures",
    "misses_contained_by_backend",
    "false_rejections",
    "blocks_on_admissible_by_outcome",
    "abstentions",
    "invalid_outputs",
    "timeouts",
)
WILSON_KEYS = ("k", "n", "rate", "low", "high")
LATENCY_KEYS = ("n", "median_ms", "p95_ms", "max_ms")
PAIR_KEYS = (
    "pairs",
    "both_admitted",
    "both_blocked",
    "only_b_blocked",
    "only_a_blocked",
    "mcnemar_exact_p",
)
COST_KEYS = (
    "basis",
    "llm_calls",
    "provider_calls",
    "fake_calls",
    "calls_by_status",
    "calls_with_unknown_usage",
    "pending_calls",
    "tokens",
    "cost_usd",
    "call_latency_ms",
)
TOKEN_KEYS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")
DEVIATION_KEYS = ("episode_id", "case_id", "condition", "field", "expected", "observed")
ROW_KEYS = (
    "episode_id",
    "pair_id",
    "order_index",
    "case_id",
    "case_kind",
    "repetition",
    "arm",
    "admission_code",
    "effect",
    "validator_outcome",
    "backend_failures",
    "outcome_class",
    "latency_ms",
    "llm_calls",
    "tokens",
    "cost_usd",
)
TRACE_KEYS = (
    "schema_version",
    "experiment_id",
    "execution_kind",
    "episode_id",
    "pair_id",
    "order_index",
    "arm",
    "case_id",
    "repetition",
    "actors",
    "calls",
    "pending_calls",
    "latency_ms",
    "tokens",
    "cost_usd",
    "result",
)
CALL_KEYS = (
    "call_id",
    "role",
    "model",
    "transport",
    "latency_ms",
    "status",
    "usage",
    "cost_usd",
    "error_kind",
    "review",
)
RESULT_KEYS = (
    "admission_code",
    "effect",
    "delivery_code",
    "assessment_status",
    "validator_verdict",
    "backend_failures",
    "outcome_class",
    "record_digest",
)

HNP02_SCOPE = (
    "HNP-02 Fase 1 · brazo A: payload guionado + controles backend; brazo B: lo mismo + "
    "validador LLM · sink ficticio en memoria"
)
HNP02_SAMPLE_RULE = (
    "Muestra: la repetición 1 de cada caso, ambos brazos, fijada antes de mirar resultados. "
    "Los agregados por caso cubren todas las repeticiones."
)
HNP02_LABELS = {
    "fake": "HNP-02 · validador LLM · dry-run",
    "provider": "HNP-02 · validador LLM · proveedor",
}
HNP02_NOTES = {
    "fake": (
        "Transporte simulado (FakeTransport): respuestas pseudoaleatorias sembradas; ninguna "
        "llamada a un proveedor.",
        "Tokens y costo son estimaciones del transporte simulado a precios configurados; no es "
        "gasto real.",
    ),
    "provider": (
        "Proveedor real: tokens y costo según el usage reportado; una corrida, no una medida "
        "general del modelo.",
    ),
}
HNP02_LIMITS = (
    "Fase 1: comprador guionado; solo el validador es un LLM. El backend siempre decide: B admite "
    "un subconjunto de lo que admite A, así que cero escapes en B no es un hallazgo.",
    "Repeticiones de un mismo caso correlacionadas: Wilson y McNemar por episodio sobreestiman la "
    "precisión; ver el agregado por caso.",
    "Fallos de infraestructura contados aparte y excluidos de denominadores y pares.",
    "Sin razones del modelo ni texto libre: solo códigos de checks, estados y conteos. Sin prompts "
    "ni claves.",
)


def expect(condition: bool, problem: str) -> None:
    if not condition:
        raise ValueError(f"HNP-02 run rejected: {problem}")


def fields(value: Any, keys: tuple[str, ...], where: str) -> dict[str, Any]:
    expect(
        isinstance(value, dict) and set(value) == set(keys), f"unknown or missing {where} fields"
    )
    return value


def items(value: Any, where: str) -> list[Any]:
    expect(isinstance(value, list), f"{where} is not a list")
    return value


def count(value: Any, where: str) -> int:
    expect(type(value) is int and value >= 0, f"invalid count in {where}")
    return value


def measure(value: Any, where: str) -> float:
    expect(
        type(value) in (int, float) and math.isfinite(value) and value >= 0,
        f"invalid measure in {where}",
    )
    return value


def text(value: Any, where: str) -> str:
    expect(isinstance(value, str) and 0 < len(value) <= 5000, f"invalid text in {where}")
    return value


def choice(value: Any, choices: tuple[str, ...], where: str) -> str:
    expect(isinstance(value, str) and value in choices, f"unknown value in {where}")
    return value


def matching(value: Any, pattern: re.Pattern[str], where: str) -> str:
    expect(isinstance(value, str) and pattern.fullmatch(value) is not None, f"malformed {where}")
    return value


def bare_sha(value: Any, where: str) -> str:
    match = PREFIXED_SHA.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"HNP-02 run rejected: malformed digest in {where}")
    return match[1]


def tally(value: Any, keys: tuple[str, ...], where: str) -> dict[str, int]:
    """A counter whose absent keys are zero: only known keys and only counts."""
    expect(isinstance(value, dict) and set(value) <= set(keys), f"unknown key in {where}")
    return {key: count(value.get(key, 0), where) for key in keys}


def strict_json(document: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        expect(len(result) == len(pairs), "duplicate JSON key")
        return result

    def constant(name: str) -> Any:
        raise ValueError(f"HNP-02 run rejected: non-finite number {name}")

    return json.loads(document, object_pairs_hook=unique, parse_constant=constant)


def read_hnp02_run(directory: Path) -> dict[str, Any]:
    """Raw parts of one run directory plus the SHA-256 of each file the projection cites."""
    files = {name: (directory / name).read_bytes() for name in ("summary.json", "traces.jsonl")}
    texts = {name: raw.decode("utf-8") for name, raw in files.items()}
    expect(not any(SECRET.search(t) for t in texts.values()), "secret-like or personal string")
    lines = texts["traces.jsonl"].split("\n")
    expect(lines[-1] == "" and all(lines[:-1]), "traces are not one JSON object per line")
    return {
        "summary": strict_json(texts["summary.json"]),
        "traces": [strict_json(line) for line in lines[:-1]],
        "sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    }


def wilson(value: Any, where: str, k: int | None = None, n: int | None = None) -> dict[str, Any]:
    w = fields(value, WILSON_KEYS, where)
    wk, wn = count(w["k"], where), count(w["n"], where)
    expect(wk <= wn and k in (None, wk) and n in (None, wn), f"{where} differs from the episodes")
    bounds = (w["rate"], w["low"], w["high"])
    if wn == 0:
        expect(bounds == (None, None, None), f"{where} has an interval without trials")
    else:
        rate, low, high = (measure(b, where) for b in bounds)
        expect(
            math.isclose(rate, wk / wn, abs_tol=1e-12) and low <= rate <= high <= 1,
            f"{where} has an inconsistent interval",
        )
    return {"k": wk, "n": wn, "rate": w["rate"], "low": w["low"], "high": w["high"]}


def latency(value: Any, where: str, samples: int) -> dict[str, Any] | None:
    if value is None:
        expect(samples == 0, f"{where} missing")
        return None
    lat = fields(value, LATENCY_KEYS, where)
    median, p95, top = (measure(lat[k], where) for k in LATENCY_KEYS[1:])
    expect(count(lat["n"], where) == samples and median <= p95 <= top, f"{where} inconsistent")
    return {"n": samples, "medianMs": median, "p95Ms": p95, "maxMs": top}


def mcnemar(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pairs, as the run reports it."""
    n = b + c
    return (
        1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n)
    )


def hnp02_actor(value: Any, validator_transport: str, prompt: dict[str, str]) -> dict[str, Any]:
    a = fields(value, ACTOR_KEYS, "actor")
    role = choice(a["role"], ("buyer", "validator"), "actor role")
    kind = choice(a["kind"], ("scripted", "llm"), "actor kind")
    transport = choice(a["transport"], ("none", "fake", "openai-responses"), "actor transport")
    model = None if a["model"] is None else matching(a["model"], MODEL, "model")
    version = (
        None if a["prompt_version"] is None else matching(a["prompt_version"], PROMPT, "prompt")
    )
    sha = None if a["prompt_sha256"] is None else bare_sha(a["prompt_sha256"], "prompt hash")
    effort = a["reasoning_effort"]
    if effort is not None:
        choice(effort, ("minimal", "low", "medium", "high"), "reasoning effort")
    for key in ("seed", "max_output_tokens"):
        if a[key] is not None:
            count(a[key], f"actor {key}")
    for key in ("temperature", "timeout_seconds"):
        if a[key] is not None:
            measure(a[key], f"actor {key}")
    if role == "buyer":
        # Phase 1: the buyer replays scripted payloads, so no model, prompt or transport exists.
        expect(
            kind == "scripted" and transport == "none" and model is None and version is None,
            "buyer actor is not the scripted Phase 1 buyer",
        )
    else:
        expect(
            kind == "llm"
            and transport == validator_transport
            and model is not None
            and (version, sha) == (prompt["name"], bare_sha(prompt["sha256"], "prompt")),
            "validator actor transport, model or prompt differs from the run",
        )
        for price in (a["input_usd_per_mtok"], a["output_usd_per_mtok"]):
            matching(price, DECIMAL, "validator price")
    return {
        "role": role,
        "kind": kind,
        "adapterId": matching(a["adapter_id"], IDENT, "adapter id"),
        "transport": transport,
        "model": model,
        "promptVersion": version,
        "promptSha256": sha,
        "reasoningEffort": effort,
        "temperature": a["temperature"],
        "maxOutputTokens": a["max_output_tokens"],
        "timeoutSeconds": a["timeout_seconds"],
    }


def hnp02_row(value: Any) -> dict[str, Any]:
    r = fields(value, ROW_KEYS, "episode")
    where = "episode"
    for key in ("episode_id", "pair_id", "case_id"):
        matching(r[key], IDENT, f"{where} {key}")
    for key in ("order_index", "repetition", "llm_calls", "tokens"):
        count(r[key], f"{where} {key}")
    for key in ("latency_ms", "cost_usd"):
        measure(r[key], f"{where} {key}")
    choice(r["case_kind"], CASE_KINDS, where)
    choice(r["arm"], ("A", "B"), where)
    choice(r["admission_code"], ADMISSION_CODES, where)
    choice(r["outcome_class"], ("experimental", "infrastructure"), where)
    failures = items(r["backend_failures"], where)
    for check in failures:
        choice(check, CHECK_IDS, where)
    expect(len(set(failures)) == len(failures), f"repeated backend failure in {where}")
    expect(type(r["effect"]) is bool, "episode effect is not a boolean")
    if r["arm"] == "A":
        expect(r["validator_outcome"] is None, "validator outcome in arm A")
    else:
        choice(r["validator_outcome"], VALIDATOR_OUTCOMES, where)
    expect(
        not r["effect"] or (r["admission_code"] == "SIMULATED_ONLY" and not r["backend_failures"]),
        "simulated effect without admission",
    )
    expect(
        r["arm"] == "A" or not r["effect"] or r["validator_outcome"] == "accept",
        "arm B effect without the validator's accept",
    )
    return r


def hnp02_call(value: Any, validator: dict[str, Any]) -> dict[str, Any]:
    c = fields(value, CALL_KEYS, "call")
    expect(
        c["role"] == "validator"
        and c["model"] == validator["model"]
        and c["transport"] == validator["transport"],
        "call from an undeclared actor",
    )
    text(c["call_id"], "call id")
    measure(c["latency_ms"], "call latency")
    status = choice(c["status"], CALL_STATUSES, "call status")
    if c["usage"] is None:
        expect(c["cost_usd"] is None, "call cost without usage")
    else:
        for key in TOKEN_KEYS:
            count(fields(c["usage"], TOKEN_KEYS, "call usage")[key], "call usage")
        measure(c["cost_usd"], "call cost")
    if c["error_kind"] is not None:
        matching(c["error_kind"], ERROR_KIND, "error kind")
    review = c["review"]
    if review is not None:
        fields(review, ("decision", "checks", "reasons"), "review")
        choice(review["decision"], VERDICTS, "declared decision")
        checks = fields(review["checks"], BUSINESS_CHECKS, "review checks")
        for result in checks.values():
            choice(result, CHECK_RESULTS, "check result")
        # Model prose: only its count is ever projected.
        for reason in items(review["reasons"], "reasons"):
            expect(isinstance(reason, str), "reason is not text")
    expect(status != "ok" or review is not None, "accepted call without a parsed review")
    return c


def hnp02_trace(value: Any, experiment_id: str, kind: str, actors: list[Any]) -> dict[str, Any]:
    t = fields(value, TRACE_KEYS, "trace")
    expect(
        t["schema_version"] == HNP02_TRACE
        and t["experiment_id"] == experiment_id
        and t["execution_kind"] == kind,
        "trace from another schema or run",
    )
    arm = choice(t["arm"], ("A", "B"), "trace arm")
    declared = items(t["actors"], "trace actors")
    roles = [a.get("role") if isinstance(a, dict) else None for a in declared]
    expect(
        all(a in actors for a in declared)
        and roles == (["buyer"] if arm == "A" else ["buyer", "validator"]),
        "trace actors differ from the summary",
    )
    validator = next((a for a in actors if a["role"] == "validator"), None)
    calls = [hnp02_call(c, validator or {}) for c in items(t["calls"], "calls")]
    pending = count(t["pending_calls"], "pending calls")
    timings = t["latency_ms"]
    expect(
        isinstance(timings, dict)
        and "total" in timings
        and set(timings) <= set(LATENCY_COMPONENTS),
        "unknown latency component",
    )
    for elapsed in timings.values():
        measure(elapsed, "latency")
    tokens = fields(t["tokens"], TOKEN_KEYS, "trace tokens")
    known = [c["usage"] for c in calls if c["usage"] is not None]
    expect(
        all(count(tokens[k], "tokens") == sum(u[k] for u in known) for k in TOKEN_KEYS)
        and math.isclose(
            measure(t["cost_usd"], "trace cost"),
            sum(c["cost_usd"] or 0.0 for c in calls),
            abs_tol=1e-9,
        ),
        "trace totals differ from its calls",
    )
    result = fields(t["result"], RESULT_KEYS, "trace result")
    code = choice(result["admission_code"], ADMISSION_CODES, "admission code")
    delivery = choice(result["delivery_code"], DELIVERY_CODES, "delivery code")
    status = choice(result["assessment_status"], ASSESSMENT_STATUSES, "assessment status")
    verdict = result["validator_verdict"]
    bare_sha(result["record_digest"], "record digest")
    for check in items(result["backend_failures"], "backend failures"):
        choice(check, CHECK_IDS, "backend failure")
    infrastructure = (
        status == "unavailable"
        or code in INFRASTRUCTURE_CODES
        or delivery in INFRASTRUCTURE_DELIVERY
    )
    expect(
        result["effect"] is (delivery in ("EMITTED", "RECEIPT_UNAVAILABLE"))
        and result["outcome_class"] == ("infrastructure" if infrastructure else "experimental")
        and (verdict is None) == (status != "admitted")
        and (verdict is None or verdict in VERDICTS),
        "trace result is internally inconsistent",
    )
    if arm == "A":
        expect(status == "not_called" and not calls and not pending, "validator activity in arm A")
    else:
        expect(len(calls) + pending == (0 if status == "not_called" else 1), "unaccounted call")
    if calls:
        call = calls[0]
        expect(
            (
                status != "admitted"
                or (call["status"] == "ok" and call["review"]["decision"] == verdict)
            )
            and (status != "invalid" or call["status"] in INVALID_CALLS)
            and (status != "unavailable" or call["status"] == "transport_error"),
            "call status differs from the assessment",
        )
    return t


def bind(row: dict[str, Any], trace: dict[str, Any]) -> None:
    """Every summary episode must be exactly what its trace line recorded."""
    result, usage = trace["result"], trace["tokens"]
    status = result["assessment_status"]
    outcome = None if row["arm"] == "A" else result["validator_verdict"] or status
    keys = ("pair_id", "order_index", "case_id", "repetition", "arm")
    expect(
        all(row[k] == trace[k] for k in keys)
        and row["admission_code"] == result["admission_code"]
        and row["effect"] is result["effect"]
        and row["backend_failures"] == result["backend_failures"]
        and row["outcome_class"] == result["outcome_class"]
        and row["validator_outcome"] == outcome
        and row["llm_calls"] == len(trace["calls"])
        and row["tokens"] == usage["input_tokens"] + usage["output_tokens"]
        and math.isclose(row["cost_usd"], round(trace["cost_usd"], 8), abs_tol=1e-12)
        and math.isclose(row["latency_ms"], trace["latency_ms"]["total"], abs_tol=1e-9),
        f"episode {row['episode_id']} differs from its trace",
    )


def hnp02_arm(value: Any, rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    a = fields(value, (*ARM_KEYS, "validator") if arm == "B" else ARM_KEYS, f"arm {arm}")
    mine = [r for r in rows if r["arm"] == arm]
    scored = [r for r in mine if r["outcome_class"] == "experimental"]
    admissible = [r for r in scored if r["case_kind"] == "admissible"]
    others = [r for r in scored if r["case_kind"] != "admissible"]
    mix = Counter(r["case_kind"] for r in scored)
    where = f"arm {arm}"
    expect(
        count(a["episodes"], where) == len(mine)
        and count(a["scored_episodes"], where) == len(scored)
        and count(a["infrastructure_errors"], where) == len(mine) - len(scored)
        and tally(a["case_mix"], CASE_KINDS, where) == {k: mix[k] for k in CASE_KINDS}
        and count(a["simulated_effects"], where) == sum(r["effect"] for r in scored)
        and count(a["backend_blocks_on_invalid"], where)
        == sum(bool(r["backend_failures"]) for r in scored if r["case_kind"] == "invalid"),
        f"{where} totals differ from its episodes",
    )
    # Which cases are unsafe is the oracle's call; the episodes only bound it.
    escapes = wilson(a["escapes"], f"{where} escapes")
    expect(
        escapes["n"] <= len(others) and escapes["k"] <= sum(r["effect"] for r in others),
        f"{where} escapes exceed the non-admissible episodes",
    )
    blocked = sum(not r["effect"] for r in admissible)
    return {
        "episodes": len(mine),
        "scoredEpisodes": len(scored),
        "infrastructureErrors": len(mine) - len(scored),
        "caseMix": {k: mix[k] for k in CASE_KINDS},
        "simulatedEffects": a["simulated_effects"],
        "escapes": escapes,
        "falseBlocks": wilson(a["false_blocks"], f"{where} false blocks", blocked, len(admissible)),
        "backendBlocksOnInvalid": a["backend_blocks_on_invalid"],
        "episodeLatencyMs": latency(a["episode_latency_ms"], f"{where} latency", len(scored)),
    }


def hnp02_validator(value: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    v = fields(value, VALIDATOR_KEYS, "validator metrics")
    scored = [r for r in rows if r["arm"] == "B" and r["outcome_class"] == "experimental"]
    reviewed = [r for r in scored if r["validator_outcome"] != "not_called"]
    invalid = [r for r in reviewed if r["case_kind"] == "invalid"]
    valid = [r for r in reviewed if r["case_kind"] in ("admissible", "changed_after_review")]
    captures = [r for r in invalid if r["validator_outcome"] == "reject"]
    outcomes = Counter(r["validator_outcome"] for r in reviewed)
    blocks = Counter(
        r["validator_outcome"] for r in scored if r["case_kind"] == "admissible" and not r["effect"]
    )
    where = "validator metrics"
    expect(
        count(v["reviewed"], where) == len(reviewed)
        and tally(v["outcomes"], VALIDATOR_OUTCOMES, where)
        == {o: outcomes[o] for o in VALIDATOR_OUTCOMES}
        and tally(v["blocks_on_admissible_by_outcome"], VALIDATOR_OUTCOMES, where)
        == {o: blocks[o] for o in VALIDATOR_OUTCOMES}
        and count(v["unique_captures"], where) == sum(not r["backend_failures"] for r in captures)
        and count(v["misses_contained_by_backend"], where)
        == sum(r["validator_outcome"] == "accept" and not r["effect"] for r in invalid),
        f"{where} differ from the episodes",
    )
    # Which checks the oracle expected is not in the episodes; only its bounds are.
    expected = wilson(v["captures_with_expected_checks"], "expected checks", n=len(invalid))
    expect(expected["k"] <= len(captures), "captures with expected checks exceed captures")
    rejected = sum(r["validator_outcome"] in ("reject", "abstain") for r in valid)

    def share(key: str, outcome: str) -> dict[str, Any]:
        return wilson(v[key], key, outcomes[outcome], len(reviewed))

    return {
        "reviewed": len(reviewed),
        "outcomes": {o: outcomes[o] for o in REVIEWED_OUTCOMES},
        "captures": wilson(v["captures"], "captures", len(captures), len(invalid)),
        "capturesWithExpectedChecks": expected,
        "uniqueCaptures": v["unique_captures"],
        "missesContainedByBackend": v["misses_contained_by_backend"],
        "falseRejections": wilson(v["false_rejections"], "false rejections", rejected, len(valid)),
        "abstentions": share("abstentions", "abstain"),
        "invalidOutputs": share("invalid_outputs", "invalid"),
        "timeouts": share("timeouts", "timeout"),
    }


def pair_table(value: Any, where: str, pairs: list[dict[str, Any]] | None) -> dict[str, Any]:
    t = fields(value, PAIR_KEYS, where)
    total, both_admitted, both_blocked, only_b, only_a = (count(t[k], where) for k in PAIR_KEYS[:5])
    p = measure(t["mcnemar_exact_p"], where)
    expect(
        total == both_admitted + both_blocked + only_b + only_a
        and math.isclose(p, mcnemar(only_b, only_a), abs_tol=1e-12),
        f"{where}: inconsistent pair table",
    )
    # The validator can only block: a pair that A blocked and B admitted is impossible.
    expect(only_a == 0, f"{where}: arm B admitted what arm A blocked")
    if pairs is not None:
        a = [pair["A"]["effect"] for pair in pairs]
        b = [pair["B"]["effect"] for pair in pairs]
        expect(
            [total, both_admitted, both_blocked, only_b]
            == [
                len(pairs),
                sum(x and y for x, y in zip(a, b, strict=True)),
                sum(not x and not y for x, y in zip(a, b, strict=True)),
                sum(x and not y for x, y in zip(a, b, strict=True)),
            ],
            f"{where} differ from the episodes",
        )
    return {
        "pairs": total,
        "bothAdmitted": both_admitted,
        "bothBlocked": both_blocked,
        "onlyBBlocked": only_b,
        "onlyABlocked": only_a,
        "mcnemarExactP": p,
    }


def hnp02_comparison(value: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    c = fields(value, ("outcome", "excluded_pairs", "all", "admissible", "unsafe"), "comparison")
    expect(c["outcome"] == PAIR_OUTCOME, "unknown paired outcome")
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        pairs.setdefault(row["pair_id"], {})[row["arm"]] = row
    usable = [
        p for p in pairs.values() if all(x["outcome_class"] == "experimental" for x in p.values())
    ]
    excluded = fields(c["excluded_pairs"], ("incomplete", "infrastructure"), "excluded pairs")
    expect(
        count(excluded["incomplete"], "excluded pairs") == 0
        and count(excluded["infrastructure"], "excluded pairs") == len(pairs) - len(usable),
        "excluded pairs differ from the episodes",
    )
    unsafe = pair_table(c["unsafe"], "unsafe pairs", None)
    expect(
        unsafe["pairs"] <= sum(p["A"]["case_kind"] != "admissible" for p in usable),
        "unsafe pairs exceed the non-admissible pairs",
    )
    admissible = [p for p in usable if p["A"]["case_kind"] == "admissible"]
    return {
        "excludedPairs": {"incomplete": 0, "infrastructure": len(pairs) - len(usable)},
        "all": pair_table(c["all"], "all pairs", usable),
        "admissible": pair_table(c["admissible"], "admissible pairs", admissible),
        "unsafe": unsafe,
    }


def hnp02_cost(
    value: Any,
    traces: list[dict[str, Any]],
    basis: str,
    estimate: dict[str, Any],
    validator: dict[str, Any],
) -> dict[str, Any]:
    c = fields(value, COST_KEYS, "cost")
    calls = [call for t in traces for call in t["calls"]]
    statuses = Counter(call["status"] for call in calls)
    known = [call["usage"] for call in calls if call["usage"] is not None]
    tokens = fields(c["tokens"], TOKEN_KEYS, "cost tokens")
    total = round(sum(call["cost_usd"] or 0.0 for call in calls), 8)
    expect(
        c["basis"] == basis
        and count(c["llm_calls"], "cost") == len(calls)
        and count(c["provider_calls"], "cost")
        == sum(call["transport"] == "openai-responses" for call in calls)
        and count(c["fake_calls"], "cost") == sum(call["transport"] == "fake" for call in calls)
        and tally(c["calls_by_status"], CALL_STATUSES, "calls by status")
        == {s: statuses[s] for s in CALL_STATUSES}
        and count(c["calls_with_unknown_usage"], "cost") == len(calls) - len(known)
        and count(c["pending_calls"], "cost") == sum(t["pending_calls"] for t in traces)
        and all(count(tokens[k], "cost") == sum(u[k] for u in known) for k in TOKEN_KEYS)
        and math.isclose(measure(c["cost_usd"], "cost"), total, abs_tol=1e-9),
        "cost differs from the traced calls",
    )
    return {
        "basis": basis,
        "llmCalls": len(calls),
        "providerCalls": c["provider_calls"],
        "fakeCalls": c["fake_calls"],
        "callsByStatus": {s: statuses[s] for s in CALL_STATUSES},
        "callsWithUnknownUsage": c["calls_with_unknown_usage"],
        "pendingCalls": c["pending_calls"],
        "tokens": {
            "input": tokens["input_tokens"],
            "output": tokens["output_tokens"],
            "cachedInput": tokens["cached_input_tokens"],
            "reasoning": tokens["reasoning_tokens"],
        },
        "costUsd": c["cost_usd"],
        "callLatencyMs": latency(c["call_latency_ms"], "call latency", len(calls)),
        "estimateUsd": {
            "min": matching(estimate["min_usd"], DECIMAL, "estimate"),
            "max": matching(estimate["max_usd"], DECIMAL, "estimate"),
        },
        "pricesUsdPerMtok": {
            "input": validator["input_usd_per_mtok"],
            "output": validator["output_usd_per_mtok"],
        },
    }


def hnp02_episode(row: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    """Codes, states and counts of one episode; the model's prose never leaves the trace.

    A parsed answer always carries all ten business checks, so listing only the ones it did not
    pass is lossless; None means there was no parsed answer at all.
    """
    validator = None
    if row["arm"] == "B":
        call = trace["calls"][0] if trace["calls"] else None
        review = None if call is None else call["review"]
        usage = None if call is None else call["usage"]
        validator = {
            "outcome": row["validator_outcome"],
            "callStatus": None if call is None else call["status"],
            "errorKind": None if call is None else call["error_kind"],
            "declaredDecision": None if review is None else review["decision"],
            "nonPassChecks": None
            if review is None
            else {k: v for k in BUSINESS_CHECKS if (v := review["checks"][k]) != "pass"},
            "reasonCount": None if review is None else len(review["reasons"]),
            "latencyMs": None if call is None else call["latency_ms"],
            "inputTokens": None if usage is None else usage["input_tokens"],
            "outputTokens": None if usage is None else usage["output_tokens"],
            "costUsd": None if call is None else call["cost_usd"],
        }
    return {
        "episodeId": row["episode_id"],
        "admissionCode": row["admission_code"],
        "effect": row["effect"],
        "outcomeClass": row["outcome_class"],
        "backendFailures": list(row["backend_failures"]),
        "latencyMs": row["latency_ms"],
        "validator": validator,
    }


def hnp02_case(
    case_id: str, rows: list[dict[str, Any]], traces: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    mine = [r for r in rows if r["case_id"] == case_id]
    kinds = {r["case_kind"] for r in mine}
    expect(len(kinds) == 1, f"case {case_id} changes kind between episodes")

    def arm(name: str) -> dict[str, int]:
        own = [r for r in mine if r["arm"] == name]
        return {
            "episodes": len(own),
            "infrastructure": sum(r["outcome_class"] == "infrastructure" for r in own),
            "effects": sum(r["effect"] for r in own),
        }

    outcomes = Counter(r["validator_outcome"] for r in mine if r["arm"] == "B")
    sample = {r["arm"]: r for r in mine if r["repetition"] == 1}
    return {
        "caseId": case_id,
        "caseKind": kinds.pop(),
        "arms": {"A": arm("A"), "B": arm("B")},
        # Only outcomes that occurred, in a fixed order; absent ones are zero.
        "validatorOutcomes": {o: outcomes[o] for o in VALIDATOR_OUTCOMES if outcomes[o]},
        "sample": {
            "pairId": sample["A"]["pair_id"],
            "A": hnp02_episode(sample["A"], traces[sample["A"]["episode_id"]]),
            "B": hnp02_episode(sample["B"], traces[sample["B"]["episode_id"]]),
        },
    }


def hnp02(run: dict[str, Any]) -> dict[str, Any]:
    """Project one verified HNP-02 run. Binds every summary number it shows to the traces."""
    summary = fields(run["summary"], RUN_KEYS, "summary")
    expect(summary["schema"] == HNP02_SUMMARY, "unknown summary schema")
    meta = fields(summary["experiment"], RUN_EXPERIMENT_KEYS, "experiment")
    source_kind = choice(meta["execution_kind"], tuple(HNP02_KINDS), "execution kind")
    kind, transport, validator_transport, basis = HNP02_KINDS[source_kind]
    experiment_id = matching(meta["id"], IDENT, "experiment id")
    expect(
        meta["sink"] == "fictitious_in_memory" and meta["operational_effects"] is False,
        "the sink is not fictitious",
    )
    seed, repetitions = count(meta["seed"], "seed"), count(meta["repetitions"], "repetitions")
    cases = [matching(c, IDENT, "case id") for c in items(meta["business_cases"], "cases")]
    excluded = [matching(c, IDENT, "case id") for c in items(meta["excluded_cases"], "cases")]
    expect(
        repetitions >= 1 and len(set(cases)) == len(cases) > 0 and not set(cases) & set(excluded),
        "invalid plan",
    )
    created = text(meta["created_at"], "created_at")
    expect(datetime.fromisoformat(created).tzinfo is not None, "created_at without time zone")
    settings = fields(meta["settings"], SETTINGS_KEYS, "settings")
    prompt = fields(
        fields(meta["prompts"], ("validator",), "prompts")["validator"],
        ("name", "sha256"),
        "prompt",
    )
    estimate = fields(
        meta["cost_estimate"], ("calls", "input_tokens", "min_usd", "max_usd"), "estimate"
    )
    corpus = fields(meta["corpus_sha256"], ("cases", "oracle"), "corpus")
    implementation = meta["implementation_sha256"]
    expect(
        isinstance(implementation, dict) and bool(implementation), "missing implementation hashes"
    )
    for path, sha in implementation.items():
        text(path, "implementation path")
        bare_sha(sha, "implementation hash")
    fields(meta["environment"], ("platform", "python"), "environment")
    expect(
        (settings["seed"], settings["repetitions"], settings["validator_prompt"])
        == (seed, repetitions, prompt["name"]),
        "settings differ from the experiment",
    )

    raw_actors = items(summary["actors"], "actors")
    actors = [hnp02_actor(a, validator_transport, prompt) for a in raw_actors]
    expect([a["role"] for a in actors] == ["buyer", "validator"], "unexpected actors")
    validator = raw_actors[1]
    expect(settings["validator_model"] == validator["model"], "settings differ from the validator")

    rows = [hnp02_row(r) for r in items(summary["episodes"], "episodes")]
    traces: dict[str, dict[str, Any]] = {}
    for line in run["traces"]:
        trace = hnp02_trace(line, experiment_id, source_kind, raw_actors)
        expect(trace["episode_id"] not in traces, "duplicate trace")
        traces[trace["episode_id"]] = trace
    expect(
        len(rows) == len(traces) and {r["episode_id"] for r in rows} == set(traces),
        "traces and summary episodes differ",
    )
    for row in rows:
        bind(row, traces[row["episode_id"]])
        pair = f"{row['case_id']}:r{row['repetition']}"
        expect(
            (row["pair_id"], row["episode_id"]) == (pair, f"{pair}:{row['arm']}"),
            "episode ids do not name their pair",
        )
    plan = sorted((c, r, arm) for c in cases for r in range(1, repetitions + 1) for arm in "AB")
    expect(
        sorted((r["case_id"], r["repetition"], r["arm"]) for r in rows) == plan
        and sorted(r["order_index"] for r in rows) == list(range(len(rows))),
        "episodes are not the complete paired plan",
    )

    deviations = fields(
        summary["deviations"], ("harness", "infrastructure", "validator"), "deviations"
    )
    for group in deviations.values():
        for deviation in items(group, "deviations"):
            d = fields(deviation, DEVIATION_KEYS, "deviation")
            expect(
                isinstance(d["episode_id"], str)
                and d["episode_id"] in traces
                and d["condition"] in ("A", "B")
                and d["field"] in DEVIATION_FIELDS
                and isinstance(d["expected"], str)
                and isinstance(d["observed"], str),
                "malformed deviation",
            )
    raw_arms = fields(summary["arms"], ("A", "B"), "arms")
    arms = {arm: hnp02_arm(raw_arms[arm], rows, arm) for arm in ("A", "B")}
    ordered = [traces[r["episode_id"]] for r in sorted(rows, key=lambda r: r["order_index"])]
    return {
        "id": experiment_id,
        "label": HNP02_LABELS[transport],
        "mode": "HNP",
        "executionKind": kind,
        "transport": transport,
        "scope": HNP02_SCOPE,
        "phase": text(meta["phase"], "phase"),
        "sink": "fictitious_in_memory",
        "operationalEffects": False,
        "commercialPosts": 0,
        "createdAt": created,
        "seed": seed,
        "repetitions": repetitions,
        "excludedCases": excluded,
        "actors": actors,
        "arms": arms,
        "validator": hnp02_validator(raw_arms["B"]["validator"], rows),
        "comparison": hnp02_comparison(summary["comparison"], rows),
        "cost": hnp02_cost(summary["cost"], ordered, basis, estimate, validator),
        "deviations": {group: len(values) for group, values in deviations.items()},
        "sampleRule": HNP02_SAMPLE_RULE,
        "cases": [hnp02_case(c, rows, traces) for c in cases],
        "implementationSha256": hashlib.sha256(canonical(implementation)).hexdigest(),
        "corpusSha256": {
            "cases": bare_sha(corpus["cases"], "corpus"),
            "oracle": bare_sha(corpus["oracle"], "corpus"),
        },
        "notes": [*HNP02_NOTES[transport], *HNP02_LIMITS],
        "evidence": [
            {
                "artifact": f"{experiment_id}/summary.json",
                "sha256": run["sha256"]["summary.json"],
                "pointer": "/experiment sin settings ni environment; /actors; /arms; /comparison; "
                "/cost; /deviations (conteos); /episodes (agregados por caso)",
            },
            {
                "artifact": f"{experiment_id}/traces.jsonl",
                "sha256": run["sha256"]["traces.jsonl"],
                "pointer": "una línea por episodio; muestra de la repetición 1; sin call_id, "
                "razones ni record_digest",
            },
        ],
    }


def project(
    values: dict[str, Any],
    selected: dict[str, Any],
    entries: int,
    experiments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Emit the monitor schema the UI parses directly; no browser-side translation."""
    pins = dict(PINS)
    for experiment in experiments or []:
        for item in experiment["evidence"]:
            expect(item["artifact"] not in pins, "experiment artifact already pinned")
            pins[item["artifact"]] = item["sha256"]
    return {
        "schema": "governance-monitor/3",
        "status": "PARTIAL",
        "sourcePins": pins,
        "hnpLedgerEntriesVerified": entries,
        "scenarios": [
            hp(values["agent-received.json"]),
            denied(values["http-deny.json"]),
            hnp(selected),
            hp(values["agent-lost.json"], True),
        ],
        "experiments": list(experiments or []),
    }


def render(bundle: dict[str, Any]) -> bytes:
    return json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"


def runs_report(experiments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "experimentId": e["id"],
            "executionKind": e["executionKind"],
            "transport": e["transport"],
            "episodes": e["arms"]["A"]["episodes"] + e["arms"]["B"]["episodes"],
            "sha256": {item["artifact"]: item["sha256"] for item in e["evidence"]},
        }
        for e in experiments
    ]


def publish(output: bytes, verify_only: bool) -> None:
    if verify_only:
        if DATA.read_bytes() != output:
            raise ValueError("Bundled projection differs from historical sources")
    else:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        DATA.write_bytes(output)


def write_provenance(report: dict[str, Any]) -> None:
    # Bytes, not text mode: text mode wrote CRLF on Windows, so the file depended on the platform.
    (SOURCES.parent / "provenance.json").write_bytes(
        (json.dumps(report, indent=2) + "\n").encode("utf-8")
    )


HP_SOURCES = {
    "agent-received.json": "prior/agent-received.json",
    "agent-lost.json": "prior/agent-lost.json",
    "http-deny.json": "http-deny.json",
}


def export(
    hp_root: Path, hnp_root: Path, hnp02_runs: list[Path], verify_only: bool = False
) -> dict[str, Any]:
    """From the pinned originals: re-copies HP/HNP-01 sources and rewrites provenance."""
    values = {name: read_pinned(hp_root / path, name) for name, path in HP_SOURCES.items()}
    results = read_pinned(hnp_root / "results.json", "results.json")
    read_pinned(hnp_root / "summary.json", "summary.json")
    entries = verify_hnp(results, hnp_root / "evidence.jsonl")
    selected = next(
        x
        for x in results
        if x["record"]["experiment_id"] == "validator-false-accept"
        and x["record"]["condition"] == "B"
    )
    experiments = [hnp02(read_hnp02_run(path)) for path in hnp02_runs]
    bundle = project(values, selected, entries, experiments)
    output = render(bundle)
    publish(output, verify_only)
    if not verify_only:
        SOURCES.mkdir(parents=True, exist_ok=True)
        for name, path in HP_SOURCES.items():
            (SOURCES / name).write_bytes((hp_root / path).read_bytes())
        (SOURCES / "hnp-false-accept-selected.json").write_bytes(canonical(selected) + b"\n")
        (SOURCES / "hnp-evidence.jsonl").write_bytes((hnp_root / "evidence.jsonl").read_bytes())
        write_provenance(
            {
                "sourcePins": PINS,
                "hnpLedgerEntriesVerified": entries,
                "hnpResultsVerified": len(results),
                "hnp02Runs": runs_report(experiments),
                "projection_sha256": hashlib.sha256(output).hexdigest(),
                "execution": "artifact verification and projection only; no harness execution",
                "cryptography": "digest consistency, not a signature or human authorization",
            }
        )
    return bundle


def export_versioned(hnp02_runs: list[Path], verify_only: bool = False) -> dict[str, Any]:
    """From the pinned copies in docs/governance-monitor/sources, without the originals.

    The closed-export counts in provenance.json came from the originals and are kept as-is.
    """
    values = {name: read_pinned(SOURCES / name, name) for name in HP_SOURCES}
    selected = json.loads((SOURCES / "hnp-false-accept-selected.json").read_text(encoding="utf-8"))
    entries = verify_hnp([selected], SOURCES / "hnp-evidence.jsonl")
    experiments = [hnp02(read_hnp02_run(path)) for path in hnp02_runs]
    bundle = project(values, selected, entries, experiments)
    output = render(bundle)
    publish(output, verify_only)
    if not verify_only:
        report = json.loads((SOURCES.parent / "provenance.json").read_text(encoding="utf-8"))
        report.update(
            hnp02Runs=runs_report(experiments),
            projection_sha256=hashlib.sha256(output).hexdigest(),
        )
        write_provenance(report)
    return bundle


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    origin = parser.add_mutually_exclusive_group(required=True)
    origin.add_argument("--hp-root", type=Path, help="HP originals; requires --hnp-root")
    origin.add_argument(
        "--versioned-sources",
        action="store_true",
        help="rebuild HP/HNP-01 from the pinned copies in docs/governance-monitor/sources",
    )
    parser.add_argument("--hnp-root", type=Path, help="HNP-01 originals")
    parser.add_argument(
        "--hnp02-run",
        type=Path,
        action="append",
        default=[],
        help="HNP-02 run directory with summary.json and traces.jsonl (repeatable)",
    )
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.versioned_sources:
        result = export_versioned(args.hnp02_run, args.verify_only)
    elif args.hnp_root is None:
        parser.error("--hp-root requires --hnp-root")
    else:
        result = export(args.hp_root, args.hnp_root, args.hnp02_run, args.verify_only)
    print(
        json.dumps(
            {
                "scenarios": len(result["scenarios"]),
                "experiments": len(result["experiments"]),
                "status": result["status"],
                "hnp_entries": result["hnpLedgerEntriesVerified"],
                "new_commercial_calls": 0,
                "model_calls": 0,
            }
        )
    )
