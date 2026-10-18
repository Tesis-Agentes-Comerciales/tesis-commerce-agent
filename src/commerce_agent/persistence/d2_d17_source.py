"""Local read-only D17 verifier. No HTTP, re-evaluation, dispatch or recovery writes."""

import sqlite3
from contextlib import closing

from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.guarded_creation import GuardedObservation, verify_proof
from commerce_agent.governance.human_consent import CommercialTerms, ConsentBlocked
from commerce_agent.governance.preselection_d1 import PreselectionD1Request
from commerce_agent.persistence.guarded_d1_sqlite import SqliteGuardedD1


def _readonly(action, _arg1, _arg2, _db, _trigger):
    # BEGIN IMMEDIATE in the historical verifier only takes a lock and rolls back.
    # Reject actual DML/DDL even if that verifier is changed in a later increment.
    if action == sqlite3.SQLITE_PRAGMA:
        return (
            sqlite3.SQLITE_OK
            if (_arg1 == "foreign_keys" and _arg2 is None) or _arg1 == "table_info"
            else sqlite3.SQLITE_DENY
        )
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_RECURSIVE,
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


class VerifiedD17Source:
    """Backend capability boundary; request comes from backend episode registry.

    Reuses the original P/Q/B custody and reference replay; no policy decision is
    issued. Requires an already durable, successful historical lookup event.
    """

    def __init__(self, applied: SqliteGuardedD1, request: PreselectionD1Request):
        self.applied = applied
        self.request = parse_model(PreselectionD1Request, canonical_bytes(request))

    def terms(self) -> CommercialTerms:
        try:
            case, request = self.applied, self.request
            _selection, shadow = case._source(request)
            with closing(case._connect()) as db:
                db.set_authorizer(_readonly)
                _record, legacy, _uow = case._authorization(db, request, shadow)
                db.execute("BEGIN")
                guard = case._guard(db, request, shadow, legacy)
                events, _last = case._events(db, request.run_id)
                if db.execute(
                    "SELECT state FROM d1_checkout_operations WHERE operation_id=?",
                    (guard.operation_id,),
                ).fetchone() != ("claimed",):
                    raise ValueError
                digest = sha256_digest(canonical_bytes(guard))
                if not events or any(event["guard_digest"] != digest for event in events):
                    raise ValueError
                event = events[-1]
                observation = parse_model(GuardedObservation, canonical_bytes(event["observation"]))
                if (
                    event["kind"] != "lookup"
                    or observation.result != "found"
                    or observation.proof is None
                ):
                    raise ValueError
                proof = observation.proof
                verify_proof(proof, guard, legacy)
                # Recheck every earlier found proof, including its original ACP schema.
                for old in events:
                    prior = parse_model(GuardedObservation, canonical_bytes(old["observation"]))
                    if prior.result in {"found", "rejected"} and prior != observation:
                        raise ValueError
                raw = proof.creation.original_response
                offer = guard.selected_offer
                line = raw["line_items"][0]
                if (
                    raw["status"] != "not_ready_for_payment"
                    or raw["currency"] != "cop"
                    or line["quantity"] != 1
                    or type(line["quantity"]) is not int
                    or line["item"]["id"] != offer.id
                ):
                    raise ValueError
                return CommercialTerms(
                    merchant_id=offer.merchant_id,
                    merchant_origin=guard.merchant_origin,
                    remote_actor_id=proof.creation.actor_id,
                    remote_run_id=proof.creation.run_id,
                    checkout_id=proof.creation.checkout_id,
                    checkout_status=raw["status"],
                    offer_id=offer.id,
                    product_id=offer.product_id,
                    variant_id=offer.id,
                    offer_revision=offer.revision,
                    offer_name=offer.name,
                    checkout_item_name=line["item"]["name"],
                    unit_amount_minor=line["item"]["unit_amount"],
                    shipping_amount_minor=offer.pricing.shipping_total_minor,
                    total_minor=offer.pricing.total_minor,
                    destination_country=offer.delivery_context.country,
                    destination_city=offer.delivery_context.city,
                    destination_postal_code=offer.delivery_context.postal_code,
                    delivery_days=offer.delivery_days,
                    product_condition=offer.condition,
                    fulfillment_options_canonical=canonical_bytes(
                        raw.get("fulfillment_options", [])
                    ).decode(),
                    offer_expires_at=offer.expires_at,
                    checkout_expires_at=raw["expires_at"],
                    offer_digest="sha256:" + proof.accepted_offer_snapshot_sha256,
                    d17_proof_digest="sha256:" + proof.digest(),
                    d17_envelope_digest=digest,
                    d17_history_digest=sha256_digest(canonical_bytes(events)),
                    original_response_digest="sha256:" + proof.creation.original_response_sha256,
                    selection_seal_digest=request.seal_digest,
                    shadow_binding_digest=guard.shadow_binding_digest,
                    applied_decision_id=guard.decision_id,
                    applied_evidence_digest=guard.evidence_digest,
                    operation_id=guard.operation_id,
                )
        except Exception:
            raise ConsentBlocked("VERIFIED_FOUND_D17_REQUIRED") from None
