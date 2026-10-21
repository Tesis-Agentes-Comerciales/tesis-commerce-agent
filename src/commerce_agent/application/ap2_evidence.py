"""Consent and effect records that link an AP2 approval to its receipts (hp-p2-evidence-chain).

`consent` is written once the approval is saved: the hashes the mandates bind (`checkout_hash`,
the `transaction_id` of both) and their `sd_hash`, which is what each receipt references, plus
the digest of the `approve` freshness record that let it through. `effect` is written once the
order is recorded: the digest and the public claims of each receipt. A verifier recomputes them
from `approvals`, `completion_results` and `hp_evidence`.

Best effort: the purchase never waits on, nor fails for, its evidence. A record that cannot be
written leaves the warning `ap2_evidence_unavailable`, and the verifier sees it as missing.
"""

import hashlib
import logging
from collections.abc import Callable, Mapping
from typing import Literal

import jwt
from ap2.sdk.sdjwt import common

from commerce_agent.acp.models import CheckoutSessionWithOrder
from commerce_agent.ap2 import checkout_hash
from commerce_agent.config import Settings
from commerce_agent.persistence import ApprovalRecord
from commerce_agent.persistence.ap2_evidence_sqlite import Ap2RecordKind, SqliteAp2EvidenceStore
from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore

logger = logging.getLogger(__name__)


def text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def mandate_reference(mandate: str) -> str:
    """The `sd_hash` of an SD-JWT mandate: the `reference` its receipt carries."""
    return common.compute_sd_hash(common.parse_token(mandate))


def consent_body(approval: ApprovalRecord, freshness_digest: str | None) -> dict[str, object]:
    jwt_text, checkout, payment = (
        approval.checkout_jwt,
        approval.checkout_mandate,
        approval.payment_mandate,
    )
    return {
        "run_id": approval.run_id,
        "checkout_id": approval.checkout_id,
        "approved_at": approval.approved_at,
        "checkout_fingerprint": approval.checkout_fingerprint,
        "actor_digest": text_digest(approval.actor_id),
        "freshness_digest": freshness_digest,
        # The checkout JWT carries the buyer and the address: only its hash is kept.
        "checkout_hash": checkout_hash(jwt_text) if jwt_text is not None else None,
        "checkout_mandate_sd_hash": mandate_reference(checkout) if checkout is not None else None,
        "payment_mandate_sd_hash": mandate_reference(payment) if payment is not None else None,
    }


def receipt_summary(
    receipt: str | None, identifier: Literal["order_id", "payment_id"]
) -> dict[str, object] | None:
    """Digest and public claims. The signature was verified when the receipt arrived."""
    if receipt is None:
        return None
    claims = jwt.decode(receipt, options={"verify_signature": False})
    return {
        "digest": text_digest(receipt),
        "issuer": _text(claims.get("iss")),
        "status": _text(claims.get("status")),
        "reference": _text(claims.get("reference")),
        identifier: _text(claims.get(identifier)),
    }


def effect_body(
    run_id: str,
    completed: CheckoutSessionWithOrder,
    checkout_receipt: str | None,
    payment_receipt: str | None,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "checkout_id": completed.id,
        "order_id": completed.order.id,
        "checkout_receipt": receipt_summary(checkout_receipt, "order_id"),
        "payment_receipt": receipt_summary(payment_receipt, "payment_id"),
    }


class Ap2EvidenceRecorder:
    def __init__(self, store: SqliteAp2EvidenceStore, freshness: SqliteHpEvidenceStore) -> None:
        self._store = store
        self._freshness = freshness

    def consent(self, approval: ApprovalRecord) -> None:
        self._record(
            approval.run_id,
            "consent",
            lambda: consent_body(approval, self._approve_freshness(approval)),
        )

    def effect(
        self,
        run_id: str,
        completed: CheckoutSessionWithOrder,
        checkout_receipt: str | None,
        payment_receipt: str | None,
    ) -> None:
        self._record(
            run_id,
            "effect",
            lambda: effect_body(run_id, completed, checkout_receipt, payment_receipt),
        )

    def _approve_freshness(self, approval: ApprovalRecord) -> str | None:
        """The last `approve` check of this checkout: the one that let the approval through."""
        checks = [
            record.digest
            for record in self._freshness.for_run(approval.run_id)
            if record.record_kind == "freshness_check"
            and record.body.get("stage") == "approve"
            and record.body.get("checkout_id") == approval.checkout_id
        ]
        return checks[-1] if checks else None

    def _record(
        self, run_id: str, kind: Ap2RecordKind, body: Callable[[], Mapping[str, object]]
    ) -> None:
        try:
            self._store.append(run_id, kind, body())
        except Exception as error:
            logger.warning("ap2_evidence_unavailable kind=%s error=%s", kind, type(error).__name__)


def build_ap2_evidence(settings: Settings) -> Ap2EvidenceRecorder | None:
    """Where the HP freshness evidence exists: shadow, or enforce with the governed HP flow."""
    mode = settings.agent_governance_mode
    if mode == "off" or (mode == "enforce" and settings.agent_hp_flow != "governed"):
        return None
    store = SqliteAp2EvidenceStore(settings.agent_database_path)
    store.initialize()
    freshness = SqliteHpEvidenceStore(settings.agent_database_path)
    freshness.initialize()
    return Ap2EvidenceRecorder(store, freshness)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None
