"""A bench for the HNP executor: the ledger, the store and the purchase on one real SQLite file.

The merchant, the Agent Key, the freshness check and D2/D3-HNP are scripted doubles that count
what each step reached, so a test can say which effects a deny or a lost CAS let through. With
`real_chains` the mandates are the product's (`OpenMandateIssuer`, `AgentKeyCloser`) over a Checkout
JWT the merchant double signs, so a re-claim or a dispatch can read them back.
"""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import jwt
from pydantic import SecretStr

from commerce_agent.acp import load_product_feed
from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.ap2_hnp import (
    AcceptableItem,
    AgentKeyCloser,
    MandatePair,
    OpenMandateIssuer,
)
from commerce_agent.ap2_hnp import SignedMandate as Ap2Mandate
from commerce_agent.application import PurchaseService
from commerce_agent.application.hnp_execution import (
    Dispatcher,
    HnpExecutionService,
    NullHnpPolicyGate,
    PurchaseStarter,
)
from commerce_agent.application.hnp_selection import DeterministicHnpSelector, HnpSelector
from commerce_agent.application.offer_freshness import FreshnessStage
from commerce_agent.application.purchase import CheckoutGateway
from commerce_agent.approval import ApprovalError, ApprovalService
from commerce_agent.delegation.models import Execution, ProposalDraft, SignedMandate, Step
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.hnp_models import ConsentScope, consent_scope_bytes
from commerce_agent.governance.hnp_ports import HnpGateDecision, HnpPolicyGate
from commerce_agent.governance.models import PolicyDecision
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger, utc_text
from tests.support.ap2_hnp_fixtures import MERCHANT_ISSUER, checkout_jwt, private_key_pem
from tests.support.delegation import START, AuthorityClock, Ids, consent_request
from tests.test_purchase_service import FIXTURE, FakeCheckoutGateway, details

OWNER = "buyer-a"
SCOPE = Path(__file__).parents[1] / "governance" / "fixtures" / "consent-scope.json"
CHECKOUT_JWT = "eyJ.merchant-checkout.sig"
CHECKOUT_EXPIRES = utc_text(START + timedelta(minutes=30))
GRANTED = ("SON-01", "SON-07")  # SON-07 is out of stock: the shortlist is SON-01 alone
Hook = Callable[[], object]


def scope_json(variant_ids: tuple[str, ...], valid_for: int) -> str:
    data = json.loads(SCOPE.read_text(encoding="utf-8"))
    expires = utc_text(START + timedelta(seconds=valid_for))
    data["presented_at"] = utc_text(START)
    data["expires_at"] = data["display"]["expires_at"] = expires
    data["preference_text"] = data["display"]["preference_text"] = None
    data["scope"]["variant_ids"] = list(variant_ids)
    data["display"]["variants"] = [
        {"variant_id": variant, "title": f"Audifonos Sonora {variant}"} for variant in variant_ids
    ]
    return consent_scope_bytes(parse_model(ConsentScope, canonical_bytes(data))).decode()


def draft(variant_ids: tuple[str, ...], valid_for: int) -> ProposalDraft:
    scope = scope_json(variant_ids, valid_for)
    return ProposalDraft(
        proposal_id="dpr_1",
        owner_id=OWNER,
        scope_json=scope,
        scope_digest=sha256_digest(b"commerce-delegation-scope/v1\0" + scope.encode()),
        checkout_details_json=details().model_dump_json(),
        presented_at=utc_text(START),
        expires_at=utc_text(START + timedelta(seconds=600)),
    )


class Merchant(FakeCheckoutGateway):
    """One checkout for SON-01; counts creates, merchant signatures and cancels."""

    def __init__(self, *, total: int = 74_000, refuse: bool = False) -> None:
        super().__init__(total=total)
        self.refuse = refuse
        self.on_create: Hook | None = None
        self.on_sign: Hook | None = None
        self.current = self._dated(self.current)

    @staticmethod
    def _dated(checkout: CheckoutSession) -> CheckoutSession:
        return checkout.model_copy(update={"expires_at": CHECKOUT_EXPIRES})

    def create_checkout(
        self, variant_id: str, *, currency: str, idempotency_key: str
    ) -> CheckoutSession:
        if self.on_create is not None:
            self.on_create()
        if self.refuse:
            self.calls.append(("create", idempotency_key))
            raise ACPClientError("Out of stock", status_code=409, code="out_of_stock")
        return super().create_checkout(
            variant_id, currency=currency, idempotency_key=idempotency_key
        )

    def update_checkout(self, checkout_id, update, *, idempotency_key):
        super().update_checkout(checkout_id, update, idempotency_key=idempotency_key)
        self.current = self._dated(self.current)
        return self.current

    def signed_checkout(self, checkout: CheckoutSession) -> str:
        if self.on_sign is not None:
            self.on_sign()
        self.calls.append(("sign", checkout.id))
        return CHECKOUT_JWT

    def verify_signed_checkout(self, token: str, checkout: CheckoutSession) -> None:
        assert token == CHECKOUT_JWT

    def count(self, kind: str) -> int:
        return sum(1 for call in self.calls if call[0] == kind)


class SigningMerchant(Merchant):
    """Signs the Checkout JWT of the current checkout, as the merchant's `/ap2/checkout-jwt`."""

    def __init__(self, **changes: Any) -> None:
        super().__init__(**changes)
        self.pem = private_key_pem()

    def signed_checkout(self, checkout: CheckoutSession) -> str:
        if self.on_sign is not None:
            self.on_sign()
        self.calls.append(("sign", checkout.id))
        jti = f"jti_{self.count('sign')}"
        return checkout_jwt(self.pem, checkout.model_dump(mode="json"), jti=jti)

    def verify_signed_checkout(self, token: str, checkout: CheckoutSession) -> None:
        signed = jwt.decode(token, options={"verify_signature": False})["checkout"]
        assert CheckoutSession.model_validate(signed) == checkout


class Crash(BaseException):
    """The process dies mid-run: nothing below the executor catches it."""


class AgentKey:
    """The Agent Key: counts its signatures (closes)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.signatures = 0
        self.before: Hook | None = None
        self.pair: MandatePair | None = None

    def close(
        self, open_checkout: str, open_payment: str, *, checkout_jwt: str, checkout, closed_at
    ) -> MandatePair:
        if self.before is not None:
            self.before()
        self.signatures += 1
        if self.fail:
            raise RuntimeError("Agent Key unavailable")
        jwk = {"kty": "EC", "crv": "P-256", "x": "agent-x", "y": "agent-y"}
        self.pair = MandatePair(
            Ap2Mandate(f"{open_checkout}~closed", "sd-hash-c", jwk),
            Ap2Mandate(f"{open_payment}~closed", "sd-hash-p", jwk),
        )
        return self.pair


class RealAgentKey(AgentKey):
    """The product's Agent Key, counting its closes like the double."""

    def __init__(self) -> None:
        super().__init__()
        self.key = AgentKeyCloser(private_key_pem(), merchant_issuer=MERCHANT_ISSUER)

    def close(
        self, open_checkout: str, open_payment: str, *, checkout_jwt, checkout, closed_at
    ) -> MandatePair:
        if self.before is not None:
            self.before()
        self.signatures += 1
        self.pair = self.key.close(
            open_checkout,
            open_payment,
            checkout_jwt=checkout_jwt,
            checkout=checkout,
            closed_at=closed_at,
        )
        return self.pair


def real_opens(closer: RealAgentKey, variant_ids: tuple[str, ...], valid_for: int) -> MandatePair:
    issuer = OpenMandateIssuer(
        private_key_pem(), agent_public_jwk=closer.key.public_jwk, merchant_issuer=MERCHANT_ISSUER
    )
    items = [AcceptableItem(variant, f"Audifonos Sonora {variant}") for variant in variant_ids]
    expires = START + timedelta(seconds=valid_for)
    return issuer.issue(items, total_max_minor=150_000, consented_at=START, expires_at=expires)


def _ledger_mandate(mandate: Ap2Mandate) -> SignedMandate:
    return SignedMandate(mandate.token, mandate.link_hash, json.dumps(mandate.public_jwk))


class Freshness:
    def __init__(self, code: str | None = None) -> None:
        self.code = code
        self.stages: list[FreshnessStage] = []
        self.before: Hook | None = None

    def check(self, *, run_id: str, checkout: CheckoutSession, stage: FreshnessStage) -> None:
        if self.before is not None:
            self.before()
        self.stages.append(stage)
        if self.code is not None:
            raise ApprovalError("The offer moved", code=self.code)


@dataclass(frozen=True)
class Verdict:
    outcome: Literal["allow", "deny", "raise", "none"] = "allow"
    reasons: tuple[str, ...] = ()
    proof: Literal["valid", "invalid", "unresolved"] | None = "valid"


ALLOW = Verdict()


def deny(*reasons: str, proof: Literal["valid", "invalid", "unresolved"] = "valid") -> Verdict:
    return Verdict("deny", reasons, proof)


class Gate:
    """D2-HNP and D3-HNP as scripted verdicts. Each hook notes what its unit of work sees."""

    def __init__(self, d2: Verdict = ALLOW, d3: Verdict = ALLOW) -> None:
        self.verdicts = {"D2": d2, "D3": d3}
        self.calls: list[str] = []
        self.applied: list[tuple[str, tuple[Any, ...]]] = []
        # The allow's conflict hooks, when its unit of work comes back blocked (§4.5).
        self.lost: list[tuple[str, tuple[Any, ...]]] = []
        self.outbox_id: str | None = None
        self.before: dict[str, Hook] = {}

    def decide_d2(self, **request: Any) -> HnpGateDecision:
        return self._decide("D2", **request)

    def decide_d3(self, **request: Any) -> HnpGateDecision:
        return self._decide("D3", **request)

    def _decide(
        self,
        phase: str,
        *,
        run_id: str,
        grant_id: str,
        checkout: CheckoutSession,
        reservation_id: str,
        operation_id: str,
    ) -> HnpGateDecision:
        if phase in self.before:
            self.before[phase]()
        self.calls.append(phase)
        verdict = self.verdicts[phase]
        if verdict.outcome == "raise":
            raise RuntimeError("PDP unavailable")
        decision = (
            None
            if verdict.outcome == "none"
            else PolicyDecision.model_construct(
                outcome=verdict.outcome, reason_codes=verdict.reasons
            )
        )

        def hook(connection: sqlite3.Connection) -> None:
            self.applied.append((phase, seen(connection, grant_id)))

        def lost(connection: sqlite3.Connection) -> None:
            self.lost.append((phase, seen(connection, grant_id)))

        return HnpGateDecision(
            decision,
            verdict.proof,
            f"gov_{phase.lower()}_1",
            (hook,),
            conflict_hooks=(lost,) if verdict.outcome == "allow" else (),
            transition_ref=self.outbox_id if phase == "D3" else None,
        )


def seen(connection: sqlite3.Connection, grant_id: str) -> tuple[Any, ...]:
    """Task state, reservation state and outbox rows inside the hook's own transaction."""
    task = connection.execute("SELECT state FROM hnp_tasks WHERE grant_id=?", (grant_id,))
    reservation = connection.execute(
        "SELECT state FROM grant_reservations WHERE grant_id=?", (grant_id,)
    ).fetchone()
    outbox = connection.execute(
        "SELECT COUNT(*) FROM dispatch_outbox WHERE grant_id=?", (grant_id,)
    )
    return (
        task.fetchone()[0],
        None if reservation is None else reservation[0],
        outbox.fetchone()[0],
    )


@dataclass(frozen=True)
class Effects:
    creates: int
    signatures: int
    commits: int
    cancels: int


class Bench:
    def __init__(
        self,
        tmp_path: Path,
        *,
        mode: Literal["off", "enforce"] = "off",
        variant_ids: tuple[str, ...] = GRANTED,
        valid_for: int = 3600,
        budget: int = 150_000,
        merchant: Merchant | None = None,
        gate: HnpPolicyGate | None = None,
        freshness: Freshness | None = None,
        selector: HnpSelector | None = None,
        closer: AgentKey | None = None,
        purchases: Callable[["Bench"], PurchaseStarter] | None = None,
        d1_reasons: tuple[str, ...] = (),
        d1_denials: Callable[[str], tuple[str, ...]] | None = None,
        checkout: CheckoutGateway | None = None,
        real_chains: bool = False,
        dispatcher: Callable[["Bench"], Dispatcher] | None = None,
    ) -> None:
        self.path = tmp_path / "agent.db"
        self.mode: Literal["off", "enforce"] = mode
        self.clock = AuthorityClock()
        self.ledger = SqliteDelegationLedger(self.path, clock=self.clock, id_factory=Ids())
        self.ledger.initialize()
        self.store = SqliteAgentStore(self.path)
        self.store.initialize()
        proposal = draft(variant_ids, valid_for)
        self.ledger.create_proposal(proposal)
        intent = PurchaseIntent(hard=HardConstraints(currency="COP", max_total_amount_minor=budget))
        consent = replace(consent_request(proposal), intent_json=intent.model_dump_json())
        if real_chains:
            closer = closer or RealAgentKey()
            merchant = merchant or SigningMerchant()
            assert isinstance(closer, RealAgentKey)
            opens = real_opens(closer, variant_ids, valid_for)
            consent = replace(
                consent,
                agent_key_kid=closer.key.kid,
                open_checkout=_ledger_mandate(opens.checkout),
                open_payment=_ledger_mandate(opens.payment),
            )
        result = self.ledger.consent(consent)
        assert result.task is not None and result.grant is not None, result
        self.task_id, self.grant_id = result.task.task_id, result.grant.grant_id
        self.merchant = merchant or Merchant()
        self.closer = closer or AgentKey()
        self.gate = gate or (NullHnpPolicyGate() if mode == "off" else Gate())
        self.freshness = freshness or (None if mode == "off" else Freshness())
        approvals = ApprovalService(self.store, SecretStr("approval-secret-with-at-least-32-chars"))
        self.service = HnpExecutionService(
            mode=mode,
            ledger=self.ledger,
            store=self.store,
            purchases=purchases(self)
            if purchases
            else PurchaseService(
                feed=load_product_feed(FIXTURE),
                checkout=self.merchant,
                store=self.store,
                approvals=approvals,
            ),
            checkout=checkout or self.merchant,
            closer=self.closer,
            gate=self.gate,
            selector=selector or DeterministicHnpSelector(),
            feed=lambda: load_product_feed(FIXTURE),
            clock=self.clock,
            id_factory=Ids(),
            freshness=self.freshness,
            d1_denials=None if mode == "off" else d1_denials or (lambda run_id: d1_reasons),
            dispatcher=dispatcher(self) if dispatcher else None,
        )

    def run(self) -> Step:
        return self.service.run(self.task_id)

    def execution(self) -> Execution:
        execution = self.ledger.execution(self.task_id)
        assert execution is not None
        return execution

    def ended(self) -> tuple[str, str | None]:
        task = self.execution().task
        return task.state, task.outcome_code

    def run_id(self) -> str:
        run_id = self.execution().task.run_id
        assert run_id is not None
        return run_id

    def purchase(self) -> tuple[str, str | None] | None:
        """Stage and failure code of the run's purchase; None if `start` never ran."""
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT stage, failure_code FROM purchases WHERE run_id = ?", (self.run_id(),)
            ).fetchone()
        return None if row is None else (row[0], row[1])

    def events(self) -> list[str]:
        return [event.event_type for event in self.ledger.events(self.grant_id)]

    def effects(self) -> Effects:
        with closing(sqlite3.connect(self.path)) as connection:
            (commits,) = connection.execute("SELECT COUNT(*) FROM dispatch_outbox").fetchone()
        return Effects(
            creates=self.merchant.count("create"),
            signatures=self.closer.signatures,
            commits=commits,
            cancels=self.merchant.count("cancel"),
        )

    def revoke(self) -> None:
        assert self.ledger.revoke(OWNER, self.grant_id) is not None

    def bump_grant(self) -> None:
        """A CAS loss with no cause of its own: the grant's version moved under the run."""
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE delegation_grants SET state_version = state_version + 1 WHERE grant_id=?",
                (self.grant_id,),
            )
