"""Read-only projection of pinned historical artifacts. No application imports or network."""

import argparse
import hashlib
import json
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
    return {
        "id": "hp-uncertain" if uncertain else "hp-allowed",
        "label": "HP · respuesta perdida" if uncertain else "HP · permitido",
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
        "enforce": phase(
            None, guard["decision_id"], guard["evidence_id"], guard["evidence_digest"]
        ),
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


def project(values: dict[str, Any], selected: dict[str, Any], entries: int) -> dict[str, Any]:
    """Emit the monitor schema the UI parses directly; no browser-side translation."""
    return {
        "schema": "governance-monitor/2",
        "status": "PARTIAL",
        "sourcePins": PINS,
        "hnpLedgerEntriesVerified": entries,
        "scenarios": [
            hp(values["agent-received.json"]),
            denied(values["http-deny.json"]),
            hnp(selected),
            hp(values["agent-lost.json"], True),
        ],
    }


def render(bundle: dict[str, Any]) -> bytes:
    return json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"


def export(hp_root: Path, hnp_root: Path, verify_only: bool = False) -> dict[str, Any]:
    values = {
        name: read_pinned(hp_root / path, name)
        for name, path in {
            "agent-received.json": "prior/agent-received.json",
            "agent-lost.json": "prior/agent-lost.json",
            "http-deny.json": "http-deny.json",
        }.items()
    }
    results = read_pinned(hnp_root / "results.json", "results.json")
    read_pinned(hnp_root / "summary.json", "summary.json")
    entries = verify_hnp(results, hnp_root / "evidence.jsonl")
    selected = next(
        x
        for x in results
        if x["record"]["experiment_id"] == "validator-false-accept"
        and x["record"]["condition"] == "B"
    )
    bundle = project(values, selected, entries)
    output = render(bundle)
    if verify_only:
        if DATA.read_bytes() != output:
            raise ValueError("Bundled projection differs from historical sources")
    else:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        SOURCES.mkdir(parents=True, exist_ok=True)
        for name, path in {
            "agent-received.json": "prior/agent-received.json",
            "agent-lost.json": "prior/agent-lost.json",
            "http-deny.json": "http-deny.json",
        }.items():
            (SOURCES / name).write_bytes((hp_root / path).read_bytes())
        (SOURCES / "hnp-false-accept-selected.json").write_bytes(canonical(selected) + b"\n")
        (SOURCES / "hnp-evidence.jsonl").write_bytes((hnp_root / "evidence.jsonl").read_bytes())
        DATA.write_bytes(output)
        report = {
            "sourcePins": PINS,
            "hnpLedgerEntriesVerified": entries,
            "hnpResultsVerified": len(results),
            "projection_sha256": hashlib.sha256(output).hexdigest(),
            "execution": "artifact verification and projection only; no harness execution",
            "cryptography": "digest consistency, not a signature or human authorization",
        }
        (SOURCES.parent / "provenance.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
    return bundle


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hp-root", required=True, type=Path)
    parser.add_argument("--hnp-root", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    result = export(args.hp_root, args.hnp_root, args.verify_only)
    print(
        json.dumps(
            {
                "scenarios": len(result["scenarios"]),
                "status": result["status"],
                "hnp_entries": result["hnpLedgerEntriesVerified"],
                "new_commercial_calls": 0,
            }
        )
    )
