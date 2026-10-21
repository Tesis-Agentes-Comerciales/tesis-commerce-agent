"""HNP profile of `verify-evidence` (docs/design/hnp-governed.md §9.2).

One document per HNP task, found by its grant: a task without a purchase row (expired, revoked or
stopped before the claim) is verified by grant and task. Its seventeen links come in the order of
§9.2; the common ones keep the P2 name and logic (`intent`, `d1_decision`, `checkout_created`,
`order_webhook`) and the HP-only `approval` and `mandates` are `not_applicable`. Each link
recomputes digests from what the agent stored, re-verifies the open and closed mandates with the
public keys the ledger kept, and reads the delegation chain only up to its own event, so a later
edit does not taint an earlier link. Whether a step applies is read from the evidence it leaves:
a step nobody reached is `not_applicable`, which also ends a chain at the PDP that denied or at
the terminal (revocation, kill switch, escalation).

Not re-verified: the merchant's Checkout JWT signature and receipt signatures (their keys are
not stored), and the `merchant_refs`/`owner_ref` pseudonyms (they need the vault key).
`ApplicationFailure.observed_state_version` is read after the terminal's writes, so it is not
the version a lost CAS saw: no link reads it. The receipt JWTs of a completed HNP run arrive with
HNP-09b (UoW-3 writes `completion_results`): until then that link is `missing`, by design.
"""

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
from ap2.sdk.sdjwt import chain, common
from jwcrypto.jwk import JWK

from commerce_agent.ap2 import checkout_hash
from commerce_agent.ap2_hnp import MandateTrust, verify_closed_proof, verify_open_proof
from commerce_agent.delegation.models import (
    EXECUTOR_REF,
    OPERATOR_REF,
    DelegationEvent,
    chain_is_valid,
    token_digest,
)
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evidence import DecisionRecord, EvidenceError
from commerce_agent.governance.hnp_models import ConsentScope, parse_consent_scope, scope_digest
from commerce_agent.governance.models import HNP_POLICY_SET_ID
from commerce_agent.persistence import hp_evidence_sqlite
from commerce_agent.persistence.delegation_sqlite import read_events, utc_moment
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.hp_evidence_sqlite import HpEvidence

from .verify_evidence import (
    FRESHNESS_SOURCE_ID,
    STATUSES,
    Link,
    RunVerification,
    _ledger_records,
    _row,
    _table,
    mandate_claims,
    mismatch,
    missing,
    not_applicable,
    ok,
    share,
)

HNP_LINKS = (
    "intent",
    "delegation_consent",
    "open_mandates",
    "task_activation",
    "d1_decision",
    "checkout_created",
    "freshness",
    "d2_decision",
    "reservation",
    "closed_mandates",
    "d3_decision",
    "receipts",
    "order_webhook",
    "consumption",
    "revocation",
    "kill_switch",
    "escalation",
)
HP_ONLY = ("approval", "mandates")
OPEN = ("open_checkout", "open_payment")
CLOSED = ("closed_checkout", "closed_payment")
TERMINALS = ("revocation", "kill_switch", "escalation")
ENDED = frozenset({"escalated", "failed", "canceled"})
# Where the kill switch can stop a run after the `close` freshness check (§8.1).
AFTER_CLOSE = frozenset({"uow1", "uow2"})


def _thumbprint(jwk: Mapping[str, Any]) -> str:
    return JWK.from_json(json.dumps(dict(jwk))).thumbprint()


class HnpRunVerification(RunVerification):
    """One HNP task. The P2 links it reuses read its purchase, when `start` created one."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        task: dict[str, Any],
        psp: sqlite3.Connection | None,
    ) -> None:
        self.task = task
        grant_id = task["grant_id"]
        self.grant = _row(
            connection, "SELECT * FROM delegation_grants WHERE grant_id = ?", grant_id
        )
        self.proposal = (
            _row(
                connection,
                "SELECT * FROM delegation_proposals WHERE proposal_id = ?",
                self.grant["proposal_id"],
            )
            if self.grant
            else None
        )
        self.events = read_events(connection, grant_id)
        self.stored = {
            row["kind"]: row
            for row in _rows(
                connection, "SELECT * FROM delegation_mandates WHERE grant_id = ?", grant_id
            )
        }
        self.reservation = _row(
            connection,
            "SELECT * FROM grant_reservations WHERE grant_id = ? ORDER BY created_at DESC",
            grant_id,
        )
        self.outbox = (
            _row(
                connection,
                "SELECT * FROM dispatch_outbox WHERE reservation_id = ?",
                self.reservation["reservation_id"],
            )
            if self.reservation
            else None
        )
        purchase = (
            _row(
                connection,
                "SELECT * FROM purchases WHERE run_id = ? AND purchase_mode = 'HNP'",
                task["run_id"],
            )
            if task["run_id"] is not None and _table(connection, "purchases")
            else None
        )
        super().__init__(
            connection,
            purchase
            or {
                "run_id": task["run_id"],
                "checkout_id": self.reservation["checkout_id"] if self.reservation else None,
                "stage": None,
                "order_id": None,
                "selected_variant_id": None,
            },
            psp,
        )
        # The order is also anchored by the outbox row UoW-3 completed.
        outbox_order = self.outbox["order_id"] if self.outbox else None
        if self.order_id is None and outbox_order is not None:
            self.webhooks = self._order_events({outbox_order})
            self.order_id = outbox_order
        self.close_check: HpEvidence | None = None
        self.d2_record: DecisionRecord | None = None
        self.checkout_jwt: str | None = None

    # What the run reached

    @property
    def approved(self) -> bool:
        """HNP has no approval: anything past the checkout (a reservation, a D2 allow applied,
        an outbox row or a purchase handed to the dispatch)."""
        return (
            self.reservation is not None
            or self.d2_committed
            or self.outbox is not None
            or self.purchase["stage"] in ("completing", "completed")
        )

    def _of(self, *kinds: str) -> list[DelegationEvent]:
        return [event for event in self.events if event.event_type in kinds]

    def _linked(self, event: DelegationEvent) -> bool:
        """The delegation chain verifies up to this event."""
        return chain_is_valid(self.task["grant_id"], self.events[: self.events.index(event) + 1])

    def _scope(self) -> ConsentScope:
        assert self.grant is not None
        return parse_consent_scope(str(self.grant["scope_json"]).encode())

    def _phase(self, phase: str) -> list[tuple[str, str]]:
        if self.run_id is None or not _table(self.db, "governance_evidence"):
            return []
        return self.db.execute(
            "SELECT evidence_id, record_kind FROM governance_evidence WHERE run_id = ? "
            "AND phase = ? ORDER BY sequence",
            (self.run_id, phase),
        ).fetchall()

    def _open(self) -> tuple[dict[str, Any], MandateTrust]:
        """The open checkout payload, its signature checked with the issuer key the ledger
        stored, and the trust it names: that issuer, its `cnf` Agent Key and its merchant."""
        issuer = json.loads(self.stored["open_checkout"]["public_jwk_json"])
        key = JWK.from_json(json.dumps(issuer))
        token = common.parse_token(self.stored["open_checkout"]["token"])
        payload = chain.verify_chain([token], lambda token: key)[0]
        merchant = payload["constraints"][0]["allowed"][0]["website"]
        return payload, MandateTrust(issuer, payload["cnf"]["jwk"], merchant)

    def links(self) -> dict[str, Link]:
        steps: dict[str, Callable[[], Link]] = {
            "intent": self.hnp_intent,
            "delegation_consent": self.delegation_consent,
            "open_mandates": self.open_mandates,
            "task_activation": self.task_activation,
            "d1_decision": self.hnp_d1_decision,
            "checkout_created": self.checkout_created,
            "freshness": self.close_freshness,
            "d2_decision": self.hnp_d2_decision,
            "reservation": self.reserved,
            "closed_mandates": self.closed_mandates,
            "d3_decision": self.d3_decision,
            "receipts": self.hnp_receipts,
            "order_webhook": self.order_webhook,
            "consumption": self.consumption,
            "revocation": self.revocation,
            "kill_switch": self.kill_switch,
            "escalation": self.escalation,
        }
        result: dict[str, Link] = {}
        for name, step in steps.items():
            try:
                result[name] = step()
            except Exception as error:
                result[name] = mismatch(f"No se pudo interpretar ({type(error).__name__})")
        for name in HP_ONLY:
            result[name] = not_applicable("Eslabón HP: en HNP no hay aprobación humana")
        return result

    # 1-4: intent, consent, open mandates, activation

    def hnp_intent(self) -> Link:
        found = self.intent()  # P2: also keeps the digests D1 must reference
        if not self.reached_d1:
            return found
        assert self.grant is not None
        derived = PurchaseIntent(
            hard=HardConstraints(
                currency="COP", max_total_amount_minor=self._scope().scope.total_max_minor
            )
        )
        if str(self.grant["intent_json"]).encode() != canonical_bytes(derived):
            return mismatch("La intención del grant no es la derivada de su alcance")
        refs = {"grant_intent_digest": sha256_digest(canonical_bytes(derived)), **found.refs}
        detail = "Intención derivada del grant y registrada por D1"
        return Link(found.status, detail if found.status == "ok" else found.detail, refs)

    def delegation_consent(self) -> Link:
        grant, proposal = self.grant, self.proposal
        if grant is None or proposal is None:
            return missing("Sin el grant o la propuesta de la tarea")
        first = self.events[0] if self.events else None
        if first is None or first.event_type != "consented":
            return missing("La cadena del grant no empieza con el consentimiento")
        if not self._linked(first):
            return mismatch("delegation_events no verifica hasta el consentimiento")
        scope = parse_consent_scope(str(proposal["scope_json"]).encode())
        digest, body = scope_digest(scope), first.body
        refs = {"proposal_id": proposal["proposal_id"], "scope_digest": digest}
        presented, consented = utc_moment(body["presented_at"]), utc_moment(body["consented_at"])
        if not (
            digest == proposal["scope_digest"] == grant["scope_digest"] == body["scope_digest"]
            and grant["scope_json"] == proposal["scope_json"]
            and grant["proposal_id"] == proposal["proposal_id"] == body["proposal_id"]
            and proposal["grant_id"] == grant["grant_id"]
            and (body["presented_at"], body["consented_at"])
            == (proposal["presented_at"], grant["consented_at"])
            and presented <= consented < utc_moment(proposal["expires_at"])
            and consented < utc_moment(scope.expires_at)
            and grant["expires_at"] == scope.expires_at
            and (body["task_id"], body["grant_version"], body["authority_epoch"])
            == (self.task["task_id"], 1, 1)
            and first.actor_kind == "user"
            and first.actor_ref == first.on_behalf_of == scope.owner_ref
        ):
            return mismatch("El consentimiento no coincide con la propuesta y el grant", **refs)
        refs |= {"consented_at": body["consented_at"], "event_digest": first.digest}
        return ok("Consentimiento sobre el alcance recalculado", **refs)

    def open_mandates(self) -> Link:
        assert self.grant is not None
        issued = self._of("mandates_issued")
        if not issued or any(kind not in self.stored for kind in OPEN):
            return missing("Sin los mandatos abiertos o su evento")
        if not self._linked(issued[0]):
            return mismatch("delegation_events no verifica hasta mandates_issued")
        body, kid = issued[0].body, self.grant["issuer_kid"]
        listed = {entry["kind"]: entry for entry in body["mandates"]}
        checkout, payment = (self.stored[kind] for kind in OPEN)
        for row in (checkout, payment):
            entry, token = listed.get(row["kind"], {}), row["token"]
            if not (
                token_digest(token) == row["token_digest"] == entry.get("token_digest")
                and common.compute_issuer_jwt_hash(common.parse_token(token))
                == row["link_hash"]
                == entry.get("link_hash")
                and kid
                == entry.get("issuer_kid")
                == _thumbprint(json.loads(row["public_jwk_json"]))
            ):
                return mismatch(f"{row['kind']} no coincide con su digest, hash o emisor")
        refs = {"open_checkout_link_hash": checkout["link_hash"], "issuer_kid": kid}
        if (body["cnf_kid"], body["exp"], body["payment_reference"]) != (
            self.grant["agent_key_kid"],
            self.grant["expires_at"],
            checkout["link_hash"],
        ):
            return mismatch("mandates_issued no coincide con el grant", **refs)
        open_checkout, trust = self._open()
        if _thumbprint(trust.agent_key_jwk) != self.grant["agent_key_kid"]:
            return mismatch("cnf no es la Agent Key del grant", **refs)
        scope = self._scope()
        proof = verify_open_proof(
            checkout["token"],
            payment["token"],
            trust=trust,
            variant_ids=scope.scope.variant_ids,
            total_max_minor=scope.scope.total_max_minor,
            expires_at=utc_moment(scope.expires_at),
            now=utc_moment(self.grant["consented_at"]),
        )
        refs |= {"iat": open_checkout["iat"], "merchant_issuer": trust.merchant_issuer}
        if proof.status != "valid":
            return mismatch(f"Los mandatos abiertos no verifican ({proof.error})", **refs)
        return ok("Mandatos abiertos firmados por el emisor sobre el alcance", **refs)

    def task_activation(self) -> Link:
        claims = self._of("task_claimed")
        if not claims:
            if self.task["run_id"] is not None:
                return missing("La tarea tiene ejecución pero no task_claimed")
            return not_applicable("La tarea nunca se activó")
        refs = {"run_id": self.task["run_id"], "claims": len(claims)}
        for count, event in enumerate(claims, start=1):
            if not self._linked(event):
                return mismatch("delegation_events no verifica hasta task_claimed", **refs)
            if not (
                event.actor_kind == "service"
                and event.actor_ref == EXECUTOR_REF
                and event.body["trigger"] == "service_token"
                and event.body["claim_count"] == count
                and event.run_id == event.body["run_id"] == self.task["run_id"]
                and event.task_id == self.task["task_id"]
            ):
                return mismatch(
                    "task_claimed no activa esta tarea con el token de servicio", **refs
                )
        return ok("Tarea activada por el servicio con su run_id", **refs)

    # 5-11: D1, checkout, freshness, D2, reservation, closed mandates, D3

    def hnp_d1_decision(self) -> Link:
        found = self.d1_decision()
        decision = self.decision
        if found.status != "ok" or decision is None:
            return found
        intent, scope = decision.policy_input().intent, self._scope().scope
        if (
            decision.decision.purchase_mode != "HNP"
            or decision.decision.policy_binding.policy_set_id != HNP_POLICY_SET_ID
            or (intent.currency, intent.max_total_minor) != ("COP", scope.total_max_minor)
            or (
                decision.decision.outcome == "allow"
                and decision.policy_input().selected_variant_id not in scope.variant_ids
            )
        ):
            return mismatch("La decisión D1 no es la de este grant (HNP)", **found.refs)
        return found

    def close_freshness(self) -> Link:
        if self.checkout_id is None:
            if self.approved:
                return mismatch("La ejecución siguió sin checkout")
            return not_applicable("La ejecución no tiene checkout")
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
            and record.body.get("stage") == "close"
        ]
        stopped = any(
            event.body.get("stage") in AFTER_CLOSE for event in self._of("kill_switch_blocked")
        )
        if not (checks or self.approved or self._phase("D2") or stopped):
            return not_applicable("La ejecución no llegó a la frescura de cierre")
        if not checks:
            return missing("Sin comprobación de frescura `close` del checkout")
        check = checks[-1]
        if not hp_evidence_sqlite.chain_is_valid(self.run_id, records[: records.index(check) + 1]):
            return mismatch("La cadena de hp_evidence no verifica")
        self.close_check = check
        decision = check.body.get("decision")
        refs = {"close_digest": check.digest, "sequence": check.sequence, "decision": decision}
        if decision == "allow":
            return ok("Frescura `close` del checkout registrada", **refs)
        if self.approved:
            return mismatch("La ejecución siguió aunque la frescura `close` bloqueó", **refs)
        return ok("La frescura `close` detuvo la ejecución", **refs)

    def hnp_d2_decision(self) -> Link:
        rows = self._phase("D2")
        allowed = self.close_check is not None and self.close_check.body.get("decision") == "allow"
        if not rows:
            if self.approved or allowed:
                return missing("La ejecución pasó la frescura sin decisión D2")
            return not_applicable("La ejecución no llegó a D2")
        ledger = SqliteGovernanceEvidenceStore(self.db)
        try:
            records = _ledger_records(ledger, [evidence_id for evidence_id, _ in rows])
        except EvidenceError as error:
            return mismatch(f"El ledger D2 no verifica ({error.code})")
        decisions = [record for record in records if isinstance(record, DecisionRecord)]
        if decisions and self.decision is None:
            return mismatch("Hay evidencia D2 sin una decisión D1 verificada")
        for record in decisions:
            problem = self._d2_problem(record)
            if problem is not None:
                return mismatch(problem, evidence_id=record.evidence_id)
        committed = [r for r in decisions if r.application.disposition == "transition_committed"]
        reservation = self.reservation
        if len(committed) > 1:
            return mismatch("Más de una autorización D2 aplicada")
        if committed and (
            reservation is None
            or committed[0].application.transition_ref != reservation["reservation_id"]
            or reservation["d2_evidence_id"] != committed[0].evidence_id
        ):
            return mismatch("La autorización D2 no es la de la reserva")
        if reservation is not None and not committed:
            return mismatch("La reserva no tiene una autorización D2 aplicada")
        if not decisions:
            kinds = sorted({record.record_kind for record in records})
            return ok(f"D2 no llegó a decidir ({', '.join(kinds)})", records=len(records))
        record = committed[0] if committed else decisions[-1]
        self.d2_record = record if committed else None
        return _replayed(ledger, record, "D2")

    def _d2_problem(self, record: DecisionRecord) -> str | None:
        d1, grant = self.decision, self.grant
        assert d1 is not None and grant is not None
        data = record.hnp_authorization_input()
        hnp, claimed, source = data.hnp, data.d1, data.freshness.source_offer_ref
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
            return "D2 no referencia la decisión D1 de la ejecución"
        if (hnp.grant.grant_ref.id, hnp.grant.grant_ref.digest) != (
            grant["grant_id"],
            grant["scope_digest"],
        ) or (hnp.open_proof.consent_ref.id, hnp.open_proof.consent_ref.digest) != (
            grant["proposal_id"],
            grant["scope_digest"],
        ):
            return "D2 no referencia el grant ni su consentimiento"
        if (hnp.open_proof.open_checkout_ref.digest, hnp.open_proof.open_payment_ref.digest) != (
            self.stored["open_checkout"]["token_digest"],
            self.stored["open_payment"]["token_digest"],
        ) or (record.decision.outcome == "allow" and hnp.open_proof.status != "valid"):
            return "D2 no referencia los mandatos abiertos verificados"
        intent = hnp.reservation_intent
        if (intent.run_id, intent.operation_id) != (self.run_id, data.operation_id) or (
            self.reservation is not None
            and (intent.reservation_id, intent.operation_id, intent.amount_minor)
            != tuple(
                self.reservation[name]
                for name in ("reservation_id", "operation_id", "amount_minor")
            )
        ):
            return "D2 no autorizó la reserva de esta ejecución"
        check = self.close_check
        if (
            check is None
            or source.id != FRESHNESS_SOURCE_ID
            or (source.version, source.digest) != (check.sequence, check.digest)
        ):
            return "D2 no referencia la frescura `close` del checkout"
        return None

    def reserved(self) -> Link:
        row = self.reservation
        if row is None:
            if self._of("reservation_reserved") or self.outbox is not None:
                return missing("Hay rastro de una reserva pero no su fila")
            return not_applicable("La ejecución no reservó")
        found = [
            event
            for event in self._of("reservation_reserved")
            if event.body.get("reservation_id") == row["reservation_id"]
        ]
        if not found:
            return missing("La reserva no tiene evento reservation_reserved")
        event = found[0]
        refs = {
            name: row[name] for name in ("reservation_id", "operation_id", "amount_minor", "state")
        }
        if not self._linked(event):
            return mismatch("delegation_events no verifica hasta reservation_reserved", **refs)
        names = (
            "reservation_id",
            "operation_id",
            "amount_minor",
            "expected_epoch",
            "d2_evidence_id",
        )
        # Each later state leaves its own trace: the outbox row or its event.
        trace = {
            "released": "reservation_released",
            "consumed": "consumed",
            "uncertain": "reservation_uncertain",
        }.get(row["state"])
        settled = (row["state"] != "committed" or self.outbox is not None) and (
            trace is None or bool(self._of(trace))
        )
        if (
            {name: event.body.get(name) for name in names} != {name: row[name] for name in names}
            or row["expected_epoch"] != self.events[0].body["authority_epoch"]
            or (row["run_id"], row["checkout_id"]) != (self.run_id, self.checkout_id)
            or event.run_id != self.run_id
            or not settled
        ):
            return mismatch("La reserva no coincide con su evento ni con la ejecución", **refs)
        return ok("Reserva de un solo uso registrada en la cadena", **refs)

    def closed_mandates(self) -> Link:
        closed = self._of("mandates_closed")
        stored = [kind for kind in CLOSED if kind in self.stored]
        if not (closed or stored):
            if self.outbox is not None:
                return missing("El despacho no tiene mandatos cerrados")
            return not_applicable("La ejecución no cerró los mandatos")
        if not closed or len(stored) < 2 or self.reservation is None or self.grant is None:
            return missing("Faltan los mandatos cerrados, su evento o su reserva")
        if not self._linked(closed[0]):
            return mismatch("delegation_events no verifica hasta mandates_closed")
        listed = {entry["kind"]: entry for entry in closed[0].body["mandates"]}
        kid = self.grant["agent_key_kid"]
        checkout, payment = (self.stored[kind] for kind in CLOSED)
        for row in (checkout, payment):
            entry, token = listed.get(row["kind"], {}), row["token"]
            opened = self.stored["open_" + row["kind"].removeprefix("closed_")]["token"]
            prefix, last = token.split("~~")
            presented = common.parse_token(last)
            if not (
                row["reservation_id"] == self.reservation["reservation_id"]
                and token_digest(token) == row["token_digest"] == entry.get("token_digest")
                and common.compute_sd_hash(presented) == row["link_hash"] == entry.get("link_hash")
                and prefix == opened.removesuffix("~")
                and presented.payload.get("sd_hash")
                == common.compute_sd_hash(common.parse_token(opened))
                and kid
                == entry.get("agent_key_kid")
                == _thumbprint(json.loads(row["public_jwk_json"]))
            ):
                return mismatch(f"{row['kind']} no cierra el abierto con la Agent Key del grant")
        closing = mandate_claims(checkout["token"].split("~~")[1])
        paying = mandate_claims(payment["token"].split("~~")[1])
        signed = closing["checkout_jwt"]
        bound = checkout_hash(signed)
        refs = {"checkout_hash": bound, "closed_checkout_link_hash": checkout["link_hash"]}
        if (
            {entry.get("checkout_hash") for entry in listed.values()} != {bound}
            or (closing["checkout_hash"], paying["transaction_id"]) != (bound, bound)
            or jwt.decode(signed, options={"verify_signature": False})["checkout"]["id"]
            != self.checkout_id
        ):
            return mismatch("Los mandatos cerrados no firman el checkout de la ejecución", **refs)
        _, trust = self._open()
        proof = verify_closed_proof(
            checkout["token"],
            payment["token"],
            trust=MandateTrust(
                trust.issuer_jwk, json.loads(checkout["public_jwk_json"]), trust.merchant_issuer
            ),
            open_checkout=self.stored["open_checkout"]["token"],
            open_payment=self.stored["open_payment"]["token"],
            checkout_jwt=signed,
            now=datetime.fromtimestamp(closing["iat"], UTC),
        )
        if proof.status != "valid":
            return mismatch(f"Los mandatos cerrados no verifican ({proof.error})", **refs)
        self.checkout_jwt = signed
        return ok("Mandatos cerrados por la Agent Key sobre el checkout firmado", **refs)

    def d3_decision(self) -> Link:
        rows, outbox = self._phase("D3"), self.outbox
        if not rows:
            if outbox is not None or self.checkout_jwt is not None:
                return missing("La ejecución cerró los mandatos sin decisión D3")
            return not_applicable("La ejecución no llegó a D3")
        ledger = SqliteGovernanceEvidenceStore(self.db)
        try:
            records = _ledger_records(ledger, [evidence_id for evidence_id, _ in rows])
        except EvidenceError as error:
            return mismatch(f"El ledger D3 no verifica ({error.code})")
        decisions = [record for record in records if isinstance(record, DecisionRecord)]
        for record in decisions:
            problem = self._d3_problem(record)
            if problem is not None:
                return mismatch(problem, evidence_id=record.evidence_id)
        committed = [r for r in decisions if r.application.disposition == "transition_committed"]
        if len(committed) > 1 or (committed and outbox is None):
            return mismatch("Autorización D3 aplicada sin una única fila en el outbox")
        if outbox is not None:
            events = [
                event
                for event in self._of("dispatch_committed")
                if event.body.get("outbox_id") == outbox["outbox_id"]
            ]
            names = ("outbox_id", "d3_evidence_id", "authority_epoch", "control_epoch")
            if (
                not committed
                or committed[0].application.transition_ref != outbox["outbox_id"]
                or outbox["d3_evidence_id"] != committed[0].evidence_id
                or not events
                or not self._linked(events[0])
                or dict(events[0].body) != {name: outbox[name] for name in names}
            ):
                return mismatch("El outbox no es la transición de la autorización D3")
        if not decisions:
            kinds = sorted({record.record_kind for record in records})
            return ok(f"D3 no llegó a decidir ({', '.join(kinds)})", records=len(records))
        return _replayed(ledger, committed[0] if committed else decisions[-1], "D3")

    def _d3_problem(self, record: DecisionRecord) -> str | None:
        data, d2, reservation = record.dispatch_input(), self.d2_record, self.reservation
        if d2 is None or reservation is None or self.checkout_jwt is None:
            return "D3 sin una autorización D2 aplicada ni mandatos cerrados verificados"
        proof = data.closed_proof
        if (
            (data.d2.evidence_id, data.d2.application_transition_ref)
            != (d2.evidence_id, reservation["reservation_id"])
            or (data.reservation.reservation_id, data.operation_id)
            != (reservation["reservation_id"], reservation["operation_id"])
            or data.grant.grant_ref.id != self.task["grant_id"]
            or (proof.checkout_chain_ref.digest, proof.payment_chain_ref.digest)
            != (
                self.stored["closed_checkout"]["token_digest"],
                self.stored["closed_payment"]["token_digest"],
            )
            or proof.original_checkout_jwt_digest != sha256_digest(self.checkout_jwt.encode())
            or (record.decision.outcome == "allow" and proof.status != "valid")
        ):
            return "D3 no referencia la reserva, D2 ni los mandatos cerrados"
        return None

    # 12-14: receipts, order, consumption (the dispatch is HNP-09b's)

    def hnp_receipts(self) -> Link:
        outbox, recorded = self.outbox, self._of("receipt_recorded")
        if outbox is None or (outbox["state"] not in ("completed", "rejected") and not recorded):
            return not_applicable("El despacho no llegó a un recibo")
        links = {
            kind: self.stored[f"closed_{kind}"]["link_hash"]
            for kind in ("checkout", "payment")
            if f"closed_{kind}" in self.stored
        }
        receipts = {event.body.get("kind"): event for event in recorded}
        if outbox["state"] == "rejected":
            kind = "payment" if outbox["rejected_by"] == "psp" else "checkout"
            event, token = receipts.get(kind), outbox["error_receipt_jwt"]
            if event is None or not token:
                return missing("El rechazo no guardó su recibo de error")
            refs = {
                "receipt_digest": event.body["receipt_digest"],
                "rejected_by": outbox["rejected_by"],
            }
            if not (
                self._linked(event)
                and event.body["receipt_digest"] == token_digest(token)
                and (event.body["status"], event.body["error"]) == ("error", outbox["ap2_error"])
                and event.body["reference"] == links.get(kind)
            ):
                return mismatch("El recibo de error no es el del mandato cerrado", **refs)
            return ok("Recibo de error AP2 ligado al mandato cerrado", **refs)
        for kind, event in receipts.items():
            if not (
                kind in links
                and self._linked(event)
                and event.body["error"] is None
                and event.body["reference"] == links[kind]
            ):
                return mismatch(f"El recibo {kind} no refiere a su mandato cerrado")
        if set(receipts) != {"checkout", "payment"}:
            return missing("La orden no tiene sus dos recibos en la cadena")
        if receipts["checkout"].body.get("order_id") != outbox["order_id"]:
            return mismatch("El recibo del comercio no es el de la orden")
        completion = self.completion
        if completion is None:
            # Extension point: UoW-3 of HNP-09b keeps both JWTs in `completion_results`.
            return missing("Sin completion_results: los JWT de los recibos los guarda HNP-09b")
        refs = {kind: receipts[kind].body["receipt_digest"] for kind in ("checkout", "payment")}
        if (
            token_digest(completion["checkout_receipt_jwt"] or "") != refs["checkout"]
            or token_digest(completion["payment_receipt_jwt"] or "") != refs["payment"]
            or (completion["order_id"], completion["checkout_id"])
            != (outbox["order_id"], self.checkout_id)
        ):
            return mismatch("Los recibos guardados no son los de la cadena", **refs)
        return ok("Recibos de la orden ligados a los mandatos cerrados", **refs)

    def consumption(self) -> Link:
        consumed, reservation, outbox = self._of("consumed"), self.reservation, self.outbox
        if not consumed and self.task["state"] != "purchased":
            if outbox is not None and outbox["state"] == "completed":
                return missing("La orden no consumió la reserva")
            return not_applicable("La ejecución no llegó a una orden")
        if not consumed or reservation is None or outbox is None or self.grant is None:
            return missing("Falta el consumo, su reserva o su outbox")
        event = consumed[-1]
        order = outbox["order_id"]
        refs = {"order_id": order, "grant_state": self.grant["state"]}
        finished = self._of("task_finished")
        if not (
            self._linked(event)
            and dict(event.body)
            == {"reservation_id": reservation["reservation_id"], "order_id": order}
            and (reservation["state"], outbox["state"]) == ("consumed", "completed")
            and (self.task["state"], self.task["outcome_code"]) == ("purchased", None)
            and finished
            and finished[-1].body.get("state") == "purchased"
            and self.grant["state"] in ("consumed", "revoked", "expired")
            and self.purchase["order_id"] in (None, order)
        ):
            return mismatch("El consumo no coincide con la reserva, la orden y la tarea", **refs)
        return ok("Reserva y grant consumidos por la orden", **refs)

    # 15-17: terminals

    def revocation(self) -> Link:
        grant = self.grant
        assert grant is not None
        found = [
            event
            for event in self._of("revoked")
            if event.body.get("revoked_reason") in ("owner", "operator")
        ]
        if not found:
            if grant["revoked_reason"] in ("owner", "operator"):
                return missing("El grant figura revocado sin evento revoked")
            return not_applicable("El grant no fue revocado por su dueño ni por el operador")
        event = found[0]
        body, reservation = event.body, self.reservation
        refs = {
            name: body.get(name)
            for name in ("revoked_reason", "epoch_before", "epoch_after", "effect")
        }
        actor = (
            ("user", event.on_behalf_of)
            if body["revoked_reason"] == "owner"
            else ("operator", OPERATOR_REF)
        )
        held = None if reservation is None or self.outbox is not None else reservation
        if not (
            self._linked(event)
            and (event.actor_kind, event.actor_ref) == actor
            and body["epoch_after"] == body["epoch_before"] + 1 == grant["authority_epoch"]
            and (grant["state"], grant["revoked_reason"]) == ("revoked", body["revoked_reason"])
            and (body["effect"] == "in_flight") == (self.outbox is not None)
            and (
                held is None or (held["state"], held["release_basis"]) == ("released", "no_outbox")
            )
        ):
            return mismatch("La revocación no cerró el grant ni liberó su reserva", **refs)
        return ok("Revocación con su época y su efecto sobre la reserva", **refs)

    def kill_switch(self) -> Link:
        found = self._of("kill_switch_blocked")
        if not found:
            if self.task["outcome_code"] == "kill_switch":
                return missing("La tarea terminó por el kill switch sin kill_switch_blocked")
            return not_applicable("El kill switch no detuvo la ejecución")
        event = found[-1]
        epoch, stage = event.body.get("control_epoch"), event.body.get("stage")
        control = _row(
            self.db,
            "SELECT kill_switch, changed_at FROM hnp_controls WHERE control_epoch = ?",
            epoch,
        )
        refs = {"control_epoch": epoch, "stage": stage}
        if not (
            self._linked(event)
            and control is not None
            and control["kill_switch"] == 1
            and control["changed_at"] <= event.recorded_at
            and stage in ("claim", *AFTER_CLOSE)
            and (self.task["state"], self.task["outcome_code"]) == ("canceled", "kill_switch")
        ):
            return mismatch("kill_switch_blocked no nombra un kill switch encendido", **refs)
        return ok("El kill switch del operador detuvo la ejecución", **refs)

    def escalation(self) -> Link:
        grant = self.grant
        assert grant is not None
        found = [
            event for event in self._of("task_finished") if event.body.get("state") == "escalated"
        ]
        if not found:
            if self.task["state"] == "escalated":
                return missing("La tarea figura escalada sin task_finished")
            return not_applicable("La ejecución no escaló")
        event, reservation = found[-1], self.reservation
        refs = {"outcome_code": event.body.get("outcome_code")}
        revoked = [e for e in self._of("revoked") if e.body.get("revoked_reason") == "escalated"]
        if not (
            self._linked(event)
            and (self.task["state"], self.task["outcome_code"])
            == ("escalated", event.body["outcome_code"])
            and event.body.get("run_id") == self.task["run_id"]
            and revoked
            and self._linked(revoked[-1])
            and (grant["state"], grant["revoked_reason"]) == ("revoked", "escalated")
            and (reservation is None or reservation["state"] == "released")
        ):
            return mismatch("La escalación no revocó el grant ni liberó la reserva", **refs)
        return ok("Escalación con el grant revocado", **refs)


def _replayed(ledger: SqliteGovernanceEvidenceStore, record: DecisionRecord, phase: str) -> Link:
    refs: dict[str, Any] = {
        "decision_id": record.decision.decision_id,
        "evidence_id": record.evidence_id,
        "outcome": record.decision.outcome,
        "disposition": record.application.disposition,
        "transition_ref": record.application.transition_ref,
        "policy_version": record.decision.policy_binding.version,
    }
    try:
        ledger.replay(record.evidence_id)
        refs["replay"] = "reproduced"
    except EvidenceError as error:
        if error.code == "GOV_OUTPUT_INVALID":
            return mismatch(f"Reejecutar la política da otra decisión {phase}", **refs)
        refs["replay"] = f"unavailable ({error.code})"
    unapplied = record.application.disposition == "blocked" and record.decision.outcome == "allow"
    detail = f"Decisión {phase} {record.decision.outcome} verificada"
    return ok(f"{detail}; su unidad de trabajo no la aplicó" if unapplied else detail, **refs)


def _rows(connection: sqlite3.Connection, query: str, *values: Any) -> list[dict[str, Any]]:
    cursor = connection.execute(query, values)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def hnp_runs(
    database: Path,
    connection: sqlite3.Connection,
    psp: sqlite3.Connection | None,
    *,
    run_id: str | None = None,
    grant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Every HNP task of the database (or the one of `run_id` or `grant_id`)."""
    if not _table(connection, "hnp_tasks"):
        return []
    query, values = "SELECT * FROM hnp_tasks", []
    for column, value in (("run_id", run_id), ("grant_id", grant_id)):
        if value is not None:
            query += (" AND " if values else " WHERE ") + f"{column} = ?"
            values.append(value)
    tasks = _rows(connection, f"{query} ORDER BY created_at, task_id", *values)
    return [_document(database, HnpRunVerification(connection, task, psp)) for task in tasks]


def _document(database: Path, run: HnpRunVerification) -> dict[str, Any]:
    links = run.links()
    return {
        "database": database.name,
        "purchase_mode": "HNP",
        "run_id": run.task["run_id"],
        "grant_id": run.task["grant_id"],
        "task_id": run.task["task_id"],
        "task_state": run.task["state"],
        "outcome_code": run.task["outcome_code"],
        "stage": run.purchase["stage"],
        "order_id": run.order_id,
        "links": {name: links[name].document() for name in (*HNP_LINKS, *HP_ONLY)},
        "complete": is_hnp_complete(links, order=run.order_id is not None),
    }


def is_hnp_complete(links: Mapping[str, Link], *, order: bool) -> bool:
    """With an order, links 1-14 `ok` and no terminal `missing` or `mismatch`; otherwise none
    `missing` or `mismatch`. The HP-only links are always `not_applicable`."""
    if set(links) != {*HNP_LINKS, *HP_ONLY} or any(
        links[name].status != "not_applicable" for name in HP_ONLY
    ):
        return False
    if order and any(links[name].status != "ok" for name in HNP_LINKS if name not in TERMINALS):
        return False
    return all(link.status in ("ok", "not_applicable") for link in links.values())


def h2_hnp_summary(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """H2-HNP: complete chains among runs with an order; H2-HNP-bloqueo among blocked runs."""
    rows = list(runs)
    by_link = {
        name: {
            status: sum(row["links"][name]["status"] == status for row in rows)
            for status in STATUSES
        }
        for name in HNP_LINKS
    }
    return {
        "orders": share([row for row in rows if row["order_id"] is not None]),
        "blocked": share([row for row in rows if row["task_state"] in ENDED]),
        "all_runs": share(rows),
        "by_link": by_link,
    }
