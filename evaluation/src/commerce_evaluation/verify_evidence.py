"""Evidence chain verifier and the H2 auditability metric (docs/design/hp-p2-evidence-chain.md).

For every purchase of an agent database, in the order of the coordination contract (point 4):
intent, D1 decision, checkout created, freshness, D2 decision, approval, mandates, receipts and
the merchant's `order_create` webhook. Each link recomputes its digests from what the agent
stored and checks that it references the link before; its status is `ok`, `missing`, `mismatch`
or `not_applicable` (the purchase never reached that step). Whether a step was reached is read
from every trace it leaves, not only from the purchase row: D1 rows, consent, effect or an
`order_create` make their links apply even if another table lost its row.

A purchase with an order or an effect is complete only when its nine links are `ok`; any other
purchase is complete when no link is `missing` or `mismatch`. H2 is the share of purchases with
an order whose chain is complete.

Databases are opened read-only. The output carries identifiers and digests, never a token,
mandate, receipt, idempotency key or buyer data. Signatures are not checked again: the agent
verified them on arrival and their public keys are not stored.
"""

import argparse
import base64
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import jwt
from ap2.sdk.sdjwt import common

from commerce_agent.ap2 import checkout_hash
from commerce_agent.application.ap2_evidence import mandate_reference, receipt_summary, text_digest
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.dispatch_envelope import CheckoutCreationEnvelope
from commerce_agent.governance.evidence import DecisionEvidence, DecisionRecord, EvidenceError
from commerce_agent.governance.models import APPLICABLE_RULES
from commerce_agent.governance.provenance import HP_POLICY_SET_ID
from commerce_agent.persistence import ap2_evidence_sqlite, hp_evidence_sqlite
from commerce_agent.persistence.ap2_evidence_sqlite import Ap2Evidence
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.hp_evidence_sqlite import HpEvidence

SCHEMA_VERSION = "evidence-chain-verification/1"
Status = Literal["ok", "missing", "mismatch", "not_applicable"]
STATUSES: tuple[Status, ...] = ("ok", "missing", "mismatch", "not_applicable")
LINKS = (
    "intent",
    "d1_decision",
    "checkout_created",
    "freshness",
    "d2_decision",
    "approval",
    "mandates",
    "receipts",
    "order_webhook",
)
# Stages in which the buyer was shown the terms: the `present` freshness check ran.
PRESENTED = {"awaiting_confirmation", "approved", "completing", "uncertain", "completed"}
APPROVED = {"approved", "completing", "uncertain", "completed"}
# Tables whose rows show that the purchase reached D1.
D1_TABLES = ("d1_provenance_sources", "governance_evidence", "d1_checkout_operations")
# D2/HP rules (contract, addendum 1): a policy whose artifact lists any of them requires D2.
D2_RULES = frozenset(APPLICABLE_RULES[("D2", "HP")])
# How D2 names the `approve` freshness check it read (governance/authorization.py).
FRESHNESS_SOURCE_ID = "hp-evidence:freshness_check"


@dataclass(frozen=True)
class Link:
    status: Status
    detail: str
    refs: dict[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "refs": self.refs}


def ok(detail: str, **refs: Any) -> Link:
    return Link("ok", detail, refs)


def missing(detail: str, **refs: Any) -> Link:
    return Link("missing", detail, refs)


def mismatch(detail: str, **refs: Any) -> Link:
    return Link("mismatch", detail, refs)


def not_applicable(detail: str, **refs: Any) -> Link:
    return Link("not_applicable", detail, refs)


def read_only(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    # The D1 ledger refuses a connection without foreign keys.
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _row(connection: sqlite3.Connection, query: str, *values: Any) -> dict[str, Any] | None:
    cursor = connection.execute(query, values)
    row = cursor.fetchone()
    return (
        None if row is None else dict(zip((col[0] for col in cursor.description), row, strict=True))
    )


def _b64_json(segment: str) -> Any:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def mandate_claims(mandate: str) -> dict[str, Any]:
    """The mandate's delegate payload with every disclosure resolved, and none left over."""
    token = common.parse_token(mandate)
    disclosures = {
        common.compute_disclosure_digest(item, token.sd_alg): item for item in token.disclosures
    }
    used: set[str] = set()

    def disclosed(digest: object) -> list[Any]:
        if not isinstance(digest, str) or digest not in disclosures or digest in used:
            raise ValueError("Unresolved or repeated disclosure")
        used.add(digest)
        value = _b64_json(disclosures[digest])
        if not isinstance(value, list):
            raise ValueError("Invalid disclosure")
        return value

    [item] = token.payload["delegate_payload"]
    _, claims = disclosed(item["..."])
    claims = dict(claims)
    for digest in claims.pop("_sd", []):
        _, name, value = disclosed(digest)
        claims[name] = value
    if used != set(disclosures):
        raise ValueError("Undisclosed extra data")
    return claims


def _checkout_total(checkout: Mapping[str, Any]) -> int | None:
    totals = [row.get("amount") for row in checkout.get("totals", []) if row.get("type") == "total"]
    return totals[0] if len(totals) == 1 and type(totals[0]) is int else None


def merchant_body_sha256(payload: str) -> str:
    """The merchant signs and sends `order_create` as sorted, compact UTF-8 JSON."""
    value = json.loads(payload)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RunVerification:
    """One purchase's links, in order; each link may use what the ones before it established."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        purchase: dict[str, Any],
        psp: sqlite3.Connection | None,
    ) -> None:
        self.db = connection
        self.psp = psp
        self.purchase = purchase
        self.run_id: str = purchase["run_id"]
        self.checkout_id: str | None = purchase["checkout_id"]
        approvals = _table(connection, "approvals")
        completions = _table(connection, "completion_results")
        self.approval = (
            _row(connection, "SELECT * FROM approvals WHERE run_id = ?", self.run_id)
            if approvals
            else None
        )
        self.completion = (
            _row(connection, "SELECT * FROM completion_results WHERE run_id = ?", self.run_id)
            if completions
            else None
        )
        self.intents: dict[tuple[str, int], str] = {}
        self.decision: DecisionRecord | None = None
        self.policy_set_id: str | None = None
        self.creation: str | None = None
        self.approve_check: HpEvidence | None = None
        self.ap2_chain: tuple[Ap2Evidence, ...] = ()
        self.consent: Ap2Evidence | None = None
        self.effect: Ap2Evidence | None = None
        self._ap2 = self._read_ap2()
        self.d2_committed = self._d2_committed()
        self.has_d1 = any(
            _table(connection, table)
            and connection.execute(
                f"SELECT 1 FROM {table} WHERE run_id = ? LIMIT 1", (self.run_id,)
            ).fetchone()
            is not None
            for table in D1_TABLES
        )
        # The order is anchored by every record that names it: purchases, completion, effect
        # and, through the checkout, the merchant's order_create.
        effect_order = self.effect.body.get("order_id") if self.effect else None
        named = [
            purchase["order_id"],
            self.completion["order_id"] if self.completion else None,
            effect_order if isinstance(effect_order, str) else None,
        ]
        self.webhooks = self._order_events({order for order in named if order is not None})
        if self.webhooks:
            named.append(self.webhooks[0][1])
        self.order_id: str | None = next((order for order in named if order is not None), None)

    @property
    def stage(self) -> str:
        return str(self.purchase["stage"])

    @property
    def reached_selection(self) -> bool:
        return self.purchase["selected_variant_id"] is not None or self.checkout_id is not None

    @property
    def reached_d1(self) -> bool:
        """D1 applies: a product was chosen, D1 left rows, or a later step happened."""
        return self.reached_selection or self.has_d1 or self.approved

    @property
    def completed(self) -> bool:
        """Evidence only a completion leaves: its result, an order, effect or order_create."""
        return (
            self.completion is not None
            or self.order_id is not None
            or self.effect is not None
            or self.stage == "completed"
        )

    @property
    def approved(self) -> bool:
        """Evidence only an approval leaves: the approval, its stage, a D2 allow applied with it,
        consent or a completion."""
        unreadable = self._ap2 is not None and self._ap2.status == "mismatch"
        return (
            self.approval is not None
            or self.stage in APPROVED
            or self.d2_committed
            or self.consent is not None
            or unreadable
            or self.completed
        )

    def _d2_committed(self) -> bool:
        """A D2 allow applied as a transition: it is written with the approval, never alone."""
        if not _table(self.db, "governance_evidence"):
            return False
        try:
            row = self.db.execute(
                "SELECT 1 FROM governance_evidence WHERE run_id = ? AND phase = 'D2' "
                "AND json_extract(body, '$.application.disposition') = 'transition_committed'",
                (self.run_id,),
            ).fetchone()
        except sqlite3.Error:
            return True
        return row is not None

    def _order_events(self, order_ids: set[str]) -> list[tuple[str, str, str]]:
        """order_create events naming one of the run's orders or, in the payload, its checkout."""
        if not _table(self.db, "webhook_events"):
            return []
        rows = self.db.execute(
            "SELECT payload_sha256, order_id, payload FROM webhook_events "
            "WHERE event_type = 'order_create' ORDER BY received_at, rowid"
        ).fetchall()
        found: list[tuple[str, str, str]] = []
        for sha, order_id, payload in rows:
            try:
                event = json.loads(payload).get("data")
                checkout = event.get("checkout_session_id") if isinstance(event, dict) else None
            except (ValueError, AttributeError):
                checkout = None
            if order_id in order_ids or (
                self.checkout_id is not None and checkout == self.checkout_id
            ):
                found.append((sha, order_id, payload))
        return found

    def links(self) -> dict[str, Link]:
        steps: dict[str, Callable[[], Link]] = {
            "intent": self.intent,
            "d1_decision": self.d1_decision,
            "checkout_created": self.checkout_created,
            "freshness": self.check_freshness,
            "d2_decision": self.d2_decision,
            "approval": self.check_approval,
            "mandates": self.mandates,
            "receipts": self.receipts,
            "order_webhook": self.order_webhook,
        }
        result: dict[str, Link] = {}
        for name, step in steps.items():
            try:
                result[name] = step()
            except Exception as error:
                # Stored data that cannot even be parsed was altered or corrupted.
                result[name] = mismatch(f"No se pudo interpretar ({type(error).__name__})")
        return result

    def intent(self) -> Link:
        if not self.reached_d1:
            return not_applicable("La compra no llegó a D1")
        if not _table(self.db, "d1_provenance_sources"):
            return missing("Sin procedencia D1: la intención no quedó registrada")
        rows = self.db.execute(
            "SELECT source_id, version, digest, public_body FROM d1_provenance_sources "
            "WHERE run_id = ? AND kind = 'intent' ORDER BY version",
            (self.run_id,),
        ).fetchall()
        if not rows:
            return missing("La intención no quedó registrada")
        for source_id, version, digest, body in rows:
            if sha256_digest(bytes(body)) != digest:
                return mismatch(
                    "El digest de la intención no coincide con su cuerpo",
                    source_id=source_id,
                    version=version,
                )
            self.intents[(source_id, version)] = digest
        source_id, version, digest = rows[-1][:3]
        return ok(
            "Digest de la intención recalculado",
            source_id=source_id,
            version=version,
            digest=digest,
        )

    def d1_decision(self) -> Link:
        if not self.reached_d1:
            return not_applicable("La compra no llegó a D1")
        if not _table(self.db, "governance_evidence"):
            return missing("Sin ledger D1")
        rows = self.db.execute(
            "SELECT evidence_id, body_digest FROM governance_evidence WHERE run_id = ? "
            "AND phase = 'D1' ORDER BY sequence",
            (self.run_id,),
        ).fetchall()
        if not rows:
            return missing("D1 no registró evidencia para esta compra")
        ledger = SqliteGovernanceEvidenceStore(self.db)
        try:
            # Only D1's records: a later D2 edit does not taint this link.
            records = _ledger_records(ledger, [evidence_id for evidence_id, _ in rows])
        except EvidenceError as error:
            return mismatch(f"El ledger D1 no verifica ({error.code})")
        decisions = [
            record
            for record in records
            if isinstance(record, DecisionRecord) and record.decision.phase == "D1"
        ]
        if not decisions:
            kinds = sorted({record.record_kind for record in records})
            return ok(f"D1 no llegó a decidir ({', '.join(kinds)})", records=len(records))
        operation = (
            _row(
                self.db,
                "SELECT evidence_id FROM d1_checkout_operations WHERE run_id = ?",
                self.run_id,
            )
            if _table(self.db, "d1_checkout_operations")
            else None
        )
        chosen = [
            record
            for record in decisions
            if operation is None or record.evidence_id == operation["evidence_id"]
        ]
        if not chosen:
            return mismatch("La operación de checkout apunta a una decisión que no está")
        decision = chosen[-1]
        intent = decision.policy_input().intent.intent_ref
        if self.intents.get((intent.id, intent.version)) != intent.digest:
            return mismatch(
                "La decisión no referencia la intención registrada",
                intent_ref=intent.id,
            )
        binding = decision.decision.policy_binding
        refs = {
            "decision_id": decision.decision.decision_id,
            "evidence_id": decision.evidence_id,
            "body_digest": dict(rows)[decision.evidence_id],
            "input_digest": decision.decision.input_digest,
            "outcome": decision.decision.outcome,
            "policy_set_id": binding.policy_set_id,
            "policy_version": binding.version,
            "policy_digest": binding.artifact_digest,
        }
        try:
            ledger.replay(decision.evidence_id)
            refs["replay"] = "reproduced"
        except EvidenceError as error:
            if error.code == "GOV_OUTPUT_INVALID":
                return mismatch("Reejecutar la política da otra decisión", **refs)
            refs["replay"] = f"unavailable ({error.code})"
        self.decision = decision
        self.policy_set_id = binding.policy_set_id
        return ok(f"Decisión D1 {decision.decision.outcome} verificada", **refs)

    def checkout_created(self) -> Link:
        decision = self.decision
        operation = (
            _row(self.db, "SELECT * FROM d1_checkout_operations WHERE run_id = ?", self.run_id)
            if _table(self.db, "d1_checkout_operations")
            else None
        )
        if decision is not None and decision.decision.outcome == "deny":
            if self.checkout_id is not None or operation is not None or self.approved:
                # A checkout, its creation or anything after it under a deny is a bypass.
                refs = _operation(operation) if operation is not None else {}
                return mismatch(
                    "D1 denegó pero la compra siguió hacia un checkout",
                    checkout_id=self.checkout_id,
                    **refs,
                )
            return not_applicable("D1 denegó: no se creó checkout")
        if decision is None and self.checkout_id is None and not self.approved:
            return not_applicable("La compra no llegó a crear un checkout")
        if not _table(self.db, "d1_checkout_operations"):
            return missing("Sin operación D1 de creación del checkout")
        if operation is None:
            return missing("D1 no registró la operación de creación")
        if decision is None:
            return mismatch("La operación de creación no tiene una decisión D1 verificada")
        body_digest = self.db.execute(
            "SELECT body_digest FROM governance_evidence WHERE evidence_id = ?",
            (operation["evidence_id"],),
        ).fetchone()
        if (
            operation["evidence_id"] != decision.evidence_id
            or body_digest is None
            or operation["evidence_digest"] != body_digest[0]
            or operation["input_digest"] != decision.decision.input_digest
        ):
            return mismatch("La operación no referencia la decisión D1", **_operation(operation))
        refs = _operation(operation)
        envelope = _row(
            self.db,
            "SELECT canonical_envelope, envelope_digest FROM d1_checkout_envelopes "
            "WHERE operation_id = ?",
            operation["operation_id"],
        )
        if envelope is not None:
            parsed = parse_model(CheckoutCreationEnvelope, bytes(envelope["canonical_envelope"]))
            if (
                parsed.storage_bytes() != bytes(envelope["canonical_envelope"])
                or parsed.digest() != envelope["envelope_digest"]
                or (parsed.evidence_id, parsed.decision_id, parsed.d1_input_digest)
                != (
                    decision.evidence_id,
                    decision.decision.decision_id,
                    decision.decision.input_digest,
                )
                or parsed.evidence_digest != operation["evidence_digest"]
                or parsed.operation_id != operation["operation_id"]
            ):
                return mismatch("El sobre de creación no coincide con la decisión", **refs)
            refs["envelope_digest"] = envelope["envelope_digest"]
        result = _row(
            self.db,
            "SELECT state, checkout_id, code FROM d1_checkout_results WHERE operation_id = ?",
            operation["operation_id"],
        )
        if result is None:
            return missing("La creación no tiene resultado registrado", **refs)
        refs |= {"state": result["state"], "code": result["code"]}
        if result["state"] == "definitive_failure":
            if self.checkout_id is not None:
                return mismatch(
                    "El comercio rechazó la creación pero la compra tiene checkout", **refs
                )
            self.creation = "refused"
            return not_applicable("El comercio rechazó la creación: no hay checkout", **refs)
        if result["state"] != "confirmed":
            return missing("La creación quedó incierta: no se sabe si hay checkout", **refs)
        if result["checkout_id"] != self.checkout_id:
            return mismatch("El checkout creado no es el de la compra", **refs)
        self.creation = "confirmed"
        return ok("Checkout creado por la decisión D1", checkout_id=self.checkout_id, **refs)

    def check_freshness(self) -> Link:
        if self.checkout_id is None:
            if self.approved:
                return mismatch("La compra fue aprobada pero no tiene checkout")
            return not_applicable("La compra no tiene checkout")
        records = (
            hp_evidence_sqlite.read_run(self.db, self.run_id)
            if _table(self.db, "hp_evidence")
            else ()
        )
        checks = [
            record
            for record in records
            if record.record_kind == "freshness_check"
            and record.body.get("checkout_id") == self.checkout_id
        ]
        if not checks and self.stage not in PRESENTED and not self.approved:
            return not_applicable("No consta que la compra mostrara los términos")
        if not _table(self.db, "hp_evidence"):
            return missing("Sin evidencia HP: la frescura no quedó registrada")
        if not hp_evidence_sqlite.chain_is_valid(self.run_id, records):
            return mismatch("La cadena de hp_evidence no verifica")
        present = [check for check in checks if check.body.get("stage") == "present"]
        approve = [check for check in checks if check.body.get("stage") == "approve"]
        if not present:
            return missing("Sin comprobación de frescura al mostrar los términos")
        refs: dict[str, Any] = {"present_digest": present[-1].digest}
        if approve:
            self.approve_check = approve[-1]
            refs |= {
                "approve_digest": approve[-1].digest,
                "approve_decision": approve[-1].body.get("decision"),
            }
        if not self.approved:
            blocked = self.approve_check is not None and refs["approve_decision"] == "block"
            return ok(
                "La frescura bloqueó la aprobación"
                if blocked
                else "Frescura al mostrar registrada",
                **refs,
            )
        if self.approve_check is None:
            return missing("Compra aprobada sin comprobación de frescura al aprobar", **refs)
        if refs["approve_decision"] != "allow":
            return mismatch("Compra aprobada aunque la frescura la bloqueó", **refs)
        return ok("Frescura al mostrar y al aprobar registrada", **refs)

    def _policy(self) -> tuple[str | None, frozenset[str]]:
        """The run's policy set and rule ids, D1's and the `authorization` (D2-HP) section's, from
        the verified binding and every policy artifact stored for the run (D1's episode and each
        later decision's scope)."""
        stored: list[Any] = []
        for table in ("governance_episodes", "governance_decision_scopes"):
            if _table(self.db, table):
                stored += self.db.execute(
                    f"SELECT policy_bytes FROM {table} WHERE run_id = ?", (self.run_id,)
                ).fetchall()
        policy_set_id, rules = self.policy_set_id, set[str]()
        for (raw,) in stored:
            try:
                artifact = json.loads(bytes(raw)) if raw is not None else None
            except ValueError:
                artifact = None
            if not isinstance(artifact, dict):
                continue
            section = artifact.get("authorization")
            for ids in (
                artifact.get("policy_ids"),
                section.get("policy_ids") if isinstance(section, dict) else None,
            ):
                if isinstance(ids, list):
                    rules |= {rule for rule in ids if isinstance(rule, str)}
            if policy_set_id is None and isinstance(artifact.get("policy_set_id"), str):
                policy_set_id = artifact["policy_set_id"]
        return policy_set_id, frozenset(rules)

    def d2_decision(self) -> Link:
        policy_set_id, rules = self._policy()
        required = policy_set_id == HP_POLICY_SET_ID or bool(rules & D2_RULES)
        refs: dict[str, Any] = {
            "policy_set_id": policy_set_id,
            "d2_required": required and self.approved,
        }
        rows = (
            self.db.execute(
                "SELECT evidence_id, record_kind FROM governance_evidence "
                "WHERE run_id = ? AND phase != 'D1' ORDER BY sequence",
                (self.run_id,),
            ).fetchall()
            if _table(self.db, "governance_evidence")
            else []
        )
        if not rows:
            if required and self.approved:
                return missing("La compra aprobada no tiene decisión D2", **refs)
            if required:
                return not_applicable("La compra no llegó a aprobarse: D2 no se evaluó", **refs)
            return not_applicable(f"La política {policy_set_id or '(sin D1)'} no exige D2", **refs)
        ledger = SqliteGovernanceEvidenceStore(self.db)
        try:
            d2 = _ledger_records(ledger, [evidence_id for evidence_id, _ in rows])
        except EvidenceError as error:
            return mismatch(f"El ledger D2 no verifica ({error.code})", **refs)
        decisions = [record for record in d2 if isinstance(record, DecisionRecord)]
        if self.decision is None:
            return mismatch("Hay evidencia D2 sin una decisión D1 verificada", **refs)
        for record in decisions:
            problem = self._d2_links(record)
            if problem is not None:
                return mismatch(problem, evidence_id=record.evidence_id, **refs)
        committed = [
            record
            for record in decisions
            if record.application.disposition == "transition_committed"
        ]
        if len(committed) > 1:
            return mismatch("Más de una autorización D2 aplicada", **refs)
        if self.approved:
            if not committed:
                # A deny, an input D2 rejected or an allow it could not apply: then no approval.
                return mismatch(
                    "La compra siguió aprobada sin una autorización D2 aplicada", **refs
                )
            record = committed[0]
            approval = self.approval
            if approval is not None and approval.get("authorization_evidence_id") != (
                record.evidence_id
            ):
                return mismatch("La aprobación no apunta a la autorización D2", **refs)
            if (
                self.approve_check is None
                or record.authorization_input().freshness.source_offer_ref.digest
                != self.approve_check.digest
            ):
                return mismatch("D2 no autorizó con la frescura de la aprobación", **refs)
        elif not decisions:
            kinds = sorted({record.record_kind for record in d2})
            return ok(f"D2 no llegó a decidir ({', '.join(kinds)})", records=len(d2), **refs)
        else:
            record = decisions[-1]
        data = record.authorization_input()
        refs |= {
            "decision_id": record.decision.decision_id,
            "evidence_id": record.evidence_id,
            "outcome": record.decision.outcome,
            "disposition": record.application.disposition,
            "authorization_id": data.authorization_id,
            "policy_version": record.decision.policy_binding.version,
            "d1_evidence_digest": data.d1.evidence_digest,
            "freshness_digest": data.freshness.source_offer_ref.digest,
        }
        try:
            ledger.replay(record.evidence_id)
            refs["replay"] = "reproduced"
        except EvidenceError as error:
            if error.code == "GOV_OUTPUT_INVALID":
                return mismatch("Reejecutar la política da otra decisión D2", **refs)
            refs["replay"] = f"unavailable ({error.code})"
        return ok(f"Decisión D2 {record.decision.outcome} verificada", **refs)

    def _d2_links(self, record: DecisionRecord) -> str | None:
        """What a D2 decision says about D1, the operation and freshness, against the run's own."""
        d1 = self.decision
        assert d1 is not None
        data = record.authorization_input()
        claimed = data.d1
        if (
            claimed.decision_id,
            claimed.evidence_id,
            claimed.evidence_digest,
            claimed.input_digest,
            claimed.outcome,
            claimed.policy_binding,
        ) != (
            d1.decision.decision_id,
            d1.evidence_id,
            sha256_digest(canonical_bytes(d1)),
            d1.decision.input_digest,
            d1.decision.outcome,
            d1.decision.policy_binding,
        ):
            return "D2 no referencia la decisión D1 de la compra"
        if data.operation_id != d1.application.transition_ref:
            return "D2 no referencia la operación de D1"
        # An allow is evaluated under D1's policy; a deny may name another one (POL-06).
        allow = record.decision.outcome == "allow"
        if allow and record.decision.policy_binding != d1.decision.policy_binding:
            return "D2 autorizó con otra política que D1"
        if (
            record.application.disposition == "transition_committed"
            and record.application.transition_ref != data.authorization_id
        ):
            return "La transición de D2 no es su autorización"
        source = data.freshness.source_offer_ref
        checks = [
            check
            for check in (
                hp_evidence_sqlite.read_run(self.db, self.run_id)
                if _table(self.db, "hp_evidence")
                else ()
            )
            if check.record_kind == "freshness_check"
            and check.sequence == source.version
            and check.digest == source.digest
            and check.body.get("stage") == "approve"
            and check.body.get("checkout_id") == self.checkout_id
        ]
        if source.id != FRESHNESS_SOURCE_ID or not checks:
            return "D2 no referencia una comprobación de frescura al aprobar"
        return None

    def _ap2_records(self) -> Link | None:
        """The problem with the run's ap2_evidence, or None and consent/effect set."""
        return self._ap2

    def _read_ap2(self) -> Link | None:
        if not _table(self.db, "ap2_evidence"):
            return missing("Sin evidencia AP2 (ap2_evidence)")
        try:
            self.ap2_chain = ap2_evidence_sqlite.read_run(self.db, self.run_id)
        except (ValueError, TypeError, IndexError):
            # Records exist but cannot even be parsed: altered or corrupted.
            return mismatch("La evidencia AP2 no se pudo interpretar")
        kinds = {record.record_kind: record for record in self.ap2_chain}
        self.consent, self.effect = kinds.get("consent"), kinds.get("effect")
        return None

    def _chained(self, record: Ap2Evidence) -> bool:
        """The chain verifies up to this record: a later edit does not taint an earlier link."""
        chain = self.ap2_chain
        return ap2_evidence_sqlite.chain_is_valid(self.run_id, chain[: chain.index(record) + 1])

    def check_approval(self) -> Link:
        if not self.approved:
            return not_applicable("La compra no fue aprobada")
        approval = self.approval
        if approval is None:
            return missing("La compra figura aprobada sin aprobación guardada")
        if self.checkout_id is None or approval["checkout_id"] != self.checkout_id:
            return mismatch("La aprobación es de otro checkout")
        problem = self._ap2_records()
        if problem is not None:
            return problem
        consent = self.consent
        if consent is None:
            return missing("La aprobación no dejó registro consent")
        if not self._chained(consent):
            return mismatch("consent no verifica en la cadena de ap2_evidence")
        expected = {
            "run_id": self.run_id,
            "checkout_id": approval["checkout_id"],
            "approved_at": approval["approved_at"],
            "checkout_fingerprint": approval["checkout_fingerprint"],
            "actor_digest": text_digest(approval["actor_id"]),
            "freshness_digest": self.approve_check.digest if self.approve_check else None,
        }
        changed = sorted(
            name for name, value in expected.items() if consent.body.get(name) != value
        )
        refs = {"consent_digest": consent.digest, "freshness_digest": expected["freshness_digest"]}
        if changed:
            return mismatch(f"consent no coincide: {', '.join(changed)}", **refs)
        if expected["freshness_digest"] is None:
            return mismatch("consent no apunta a una comprobación de frescura", **refs)
        return ok("consent enlaza la aprobación con la frescura al aprobar", **refs)

    def mandates(self) -> Link:
        if not self.approved:
            return not_applicable("La compra no fue aprobada")
        approval = self.approval
        if approval is None:
            return missing("Sin aprobación guardada")
        signed, checkout_mandate, payment_mandate = (
            approval["checkout_jwt"],
            approval["checkout_mandate"],
            approval["payment_mandate"],
        )
        if not (signed and checkout_mandate and payment_mandate):
            return missing("La aprobación no tiene los mandatos AP2")
        bound = checkout_hash(signed)
        checkout = jwt.decode(signed, options={"verify_signature": False})["checkout"]
        checkout_claims = mandate_claims(checkout_mandate)
        payment_claims = mandate_claims(payment_mandate)
        amount = payment_claims.get("payment_amount") or {}
        total, currency = _checkout_total(checkout), checkout.get("currency")
        refs = {
            "checkout_hash": bound,
            "checkout_mandate_sd_hash": mandate_reference(checkout_mandate),
            "payment_mandate_sd_hash": mandate_reference(payment_mandate),
        }
        if (
            total is None
            or not isinstance(currency, str)
            or not currency
            or type(amount.get("amount")) is not int
            or not isinstance(amount.get("currency"), str)
        ):
            return mismatch("El checkout o el mandato de pago no tienen total y moneda", **refs)
        if (
            self.checkout_id is None
            or checkout.get("id") != self.checkout_id
            or checkout_claims.get("vct") != "mandate.checkout.1"
            or checkout_claims.get("checkout_jwt") != signed
            or checkout_claims.get("checkout_hash") != bound
            or payment_claims.get("vct") != "mandate.payment.1"
            or payment_claims.get("transaction_id") != bound
            or amount["amount"] != total
            or amount["currency"].upper() != currency.upper()
        ):
            return mismatch("Los mandatos no firman el checkout aprobado", **refs)
        problem = self._ap2_records()
        if problem is not None:
            return problem
        consent = self.consent
        if consent is None:
            return missing("Sin consent: los mandatos no quedan enlazados a la aprobación", **refs)
        if not self._chained(consent):
            return mismatch("consent no verifica en la cadena de ap2_evidence", **refs)
        recorded = {name: consent.body.get(name) for name in refs}
        if recorded != refs:
            return mismatch("Los hashes de consent no coinciden con los mandatos", **refs)
        return ok("Mandatos ligados al checkout firmado y a consent", **refs)

    def receipts(self) -> Link:
        if not self.completed:
            return not_applicable("La compra no llegó a una orden")
        if self.creation == "refused":
            return mismatch("El comercio rechazó la creación pero la compra tiene orden")
        completion, approval = self.completion, self.approval
        if completion is None:
            return missing("La compra tiene orden pero no resultado de completación")
        merchant, provider = completion["checkout_receipt_jwt"], completion["payment_receipt_jwt"]
        if not (merchant and provider):
            return missing("La completación no guardó los dos recibos")
        if approval is None or not (approval["checkout_mandate"] and approval["payment_mandate"]):
            return missing("Sin mandatos a los que refieran los recibos")
        checkout_receipt = receipt_summary(merchant, "order_id")
        payment_receipt = receipt_summary(provider, "payment_id")
        assert checkout_receipt is not None and payment_receipt is not None
        refs = {
            "order_id": completion["order_id"],
            "checkout_receipt_digest": checkout_receipt["digest"],
            "payment_receipt_digest": payment_receipt["digest"],
            "payment_id": payment_receipt["payment_id"],
        }
        if (
            checkout_receipt["status"] != "Success"
            or payment_receipt["status"] != "Success"
            or checkout_receipt["reference"] != mandate_reference(approval["checkout_mandate"])
            or payment_receipt["reference"] != mandate_reference(approval["payment_mandate"])
            or checkout_receipt["order_id"] != completion["order_id"]
            or payment_receipt["payment_id"] is None
            or self.checkout_id is None
            or completion["checkout_id"] != self.checkout_id
            or (self.purchase["order_id"] not in (None, completion["order_id"]))
        ):
            return mismatch("Los recibos no refieren a los mandatos ni a la orden", **refs)
        if self.psp is not None:
            problem = self._psp_payment(approval["payment_mandate"], payment_receipt, provider)
            if problem is not None:
                return mismatch(problem, **refs)
            refs["psp"] = "verified"
        problem = self._ap2_records()
        if problem is not None:
            return problem
        effect = self.effect
        if effect is None:
            return missing("La completación no dejó registro effect", **refs)
        if not self._chained(effect):
            return mismatch("effect no verifica en la cadena de ap2_evidence", **refs)
        expected = {
            "run_id": self.run_id,
            "checkout_id": completion["checkout_id"],
            "order_id": completion["order_id"],
            "checkout_receipt": checkout_receipt,
            "payment_receipt": payment_receipt,
        }
        if effect.body != expected:
            return mismatch("effect no coincide con los recibos guardados", **refs)
        if self.consent is not None and effect.previous_digest != self.consent.digest:
            return mismatch("effect no sigue a consent en la cadena", **refs)
        return ok("Recibos del PSP y del comercio ligados a los mandatos", **refs)

    def _psp_payment(
        self, payment_mandate: str, receipt: Mapping[str, Any], provider_receipt: str
    ) -> str | None:
        assert self.psp is not None
        rows = self.psp.execute(
            "SELECT payment_mandate_hash, outcome, result_json FROM delegations "
            "WHERE checkout_id = ?",
            (self.checkout_id,),
        ).fetchall()
        reference = mandate_reference(payment_mandate)
        payments = [
            json.loads(result)
            for mandate_hash, outcome, result in rows
            if mandate_hash == reference and outcome == "approved" and result is not None
        ]
        if not any(
            payment.get("id") == receipt["payment_id"]
            and payment.get("status") == "approved"
            and payment.get("payment_receipt_jwt") in (None, provider_receipt)
            for payment in payments
        ):
            return "El PSP no registra ese pago para ese mandato"
        return None

    def order_webhook(self) -> Link:
        if self.order_id is None:
            return not_applicable("La compra no llegó a una orden")
        if self.creation == "refused":
            return mismatch("El comercio rechazó la creación pero hay una orden")
        if not _table(self.db, "webhook_events"):
            return missing("Sin eventos del comercio")
        events = self.webhooks
        if not events:
            return missing(
                "El comercio no entregó el order_create de la orden", order_id=self.order_id
            )
        for sha, order_id, payload in events:
            data = json.loads(payload)
            event = data.get("data") or {}
            if (
                merchant_body_sha256(payload) != sha
                or data.get("type") != "order_create"
                or order_id != self.order_id
                or event.get("id") != self.order_id
                or self.checkout_id is None
                or event.get("checkout_session_id") != self.checkout_id
            ):
                return mismatch("El order_create no coincide con la orden", order_id=self.order_id)
        return ok(
            "order_create del comercio recibido y recalculado",
            order_id=self.order_id,
            payload_sha256=events[0][0],
        )


def _ledger_records(
    ledger: SqliteGovernanceEvidenceStore, evidence_ids: Sequence[str]
) -> list[DecisionEvidence]:
    """Each record read and checked by the ledger itself (body digest, scope, input)."""
    records = [ledger.get(evidence_id) for evidence_id in evidence_ids]
    if any(record is None for record in records):
        raise EvidenceError("GOV_INPUT_INVALID")
    return [record for record in records if record is not None]


def _operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": operation["operation_id"],
        "evidence_id": operation["evidence_id"],
        "evidence_digest": operation["evidence_digest"],
    }


def verify_database(
    database: Path, *, run_id: str | None = None, psp_database: Path | None = None
) -> list[dict[str, Any]]:
    """Every purchase of one agent database (or only `run_id`), with its links and `complete`."""
    with closing(read_only(database)) as connection:
        psp = read_only(psp_database) if psp_database is not None else None
        try:
            if not _table(connection, "purchases"):
                return []
            query = "SELECT * FROM purchases" + (" WHERE run_id = ?" if run_id else "")
            cursor = connection.execute(
                f"{query} ORDER BY created_at, run_id", (run_id,) if run_id else ()
            )
            columns = [col[0] for col in cursor.description]
            purchases = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
            return [_run_document(database, connection, purchase, psp) for purchase in purchases]
        finally:
            if psp is not None:
                psp.close()


def _run_document(
    database: Path,
    connection: sqlite3.Connection,
    purchase: dict[str, Any],
    psp: sqlite3.Connection | None,
) -> dict[str, Any]:
    run = RunVerification(connection, purchase, psp)
    links = run.links()
    return {
        "database": database.name,
        "run_id": purchase["run_id"],
        "stage": purchase["stage"],
        # Anchored by purchases, completion, effect or the merchant's order_create.
        "order_id": run.order_id,
        "links": {name: links[name].document() for name in LINKS},
        "complete": is_complete(links, effect=run.order_id is not None or run.effect is not None),
    }


def is_complete(links: Mapping[str, Link], *, effect: bool) -> bool:
    """With an order or effect, the nine links `ok`; otherwise none `missing` or `mismatch`."""
    if set(links) != set(LINKS):
        return False
    accepted = {"ok"} if effect else {"ok", "not_applicable"}
    return all(link.status in accepted for link in links.values())


def h2_summary(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """H2: among purchases that reached an order, the share whose chain verified complete."""
    rows = list(runs)

    def share(selected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        complete = sum(bool(row["complete"]) for row in selected)
        return {
            "n": len(selected),
            "complete": complete,
            "rate": round(complete / len(selected), 4) if selected else None,
        }

    by_link = {
        name: dict.fromkeys(STATUSES, 0)
        | Counter(str(row["links"][name]["status"]) for row in rows)
        for name in LINKS
    }
    return {
        "effects": share([row for row in rows if row["order_id"] is not None]),
        "all_runs": share(rows),
        "by_link": by_link,
    }


def verify(
    databases: Sequence[Path], *, run_id: str | None = None, psp_database: Path | None = None
) -> dict[str, Any]:
    unique: dict[Path, Path] = {}
    for database in databases:
        # The same database named twice (another spelling, a symlink) counts once.
        unique.setdefault(database.resolve(), database)
    runs = [
        run
        for database in unique.values()
        for run in verify_database(database, run_id=run_id, psp_database=psp_database)
    ]
    return {"schema_version": SCHEMA_VERSION, "runs": runs, "h2": h2_summary(runs)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify-evidence",
        description="Verify the decision-consent-effect evidence chain of agent databases.",
    )
    parser.add_argument(
        "--agent-db", type=Path, action="append", required=True, help="Agent SQLite (repeatable)"
    )
    parser.add_argument("--run", help="Verify only this purchase run_id")
    parser.add_argument("--psp-db", type=Path, help="Payment provider SQLite, for the payment")
    parser.add_argument("--output", type=Path, help="Write the JSON here instead of stdout")
    arguments = parser.parse_args(argv)
    for path in [*arguments.agent_db, *([arguments.psp_db] if arguments.psp_db else [])]:
        if not path.is_file():
            parser.error(f"no such database: {path}")
    document = verify(arguments.agent_db, run_id=arguments.run, psp_database=arguments.psp_db)
    if arguments.run is not None and not document["runs"]:
        parser.error(f"no purchase {arguments.run} in the given databases")
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if arguments.output is None:
        sys.stdout.write(text)
    else:
        arguments.output.write_text(text, encoding="utf-8")
    return 0 if all(run["complete"] for run in document["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
