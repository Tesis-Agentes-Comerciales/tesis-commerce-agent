"""HNP with a consented `priority` (docs/design/p0-conditional-intent.md §3.9, §4.3, §4.4, D8, D15).

The executor tries the grant's variants in the consented order: it reads each one's offer from
the merchant, buys the first that is for sale within the grant's total with one checkout, and
that checkout must carry the total the offer gave (§3.4). A variant it cannot verify stops it
before the next one (`selection_unavailable`); none for sale within the total escalates
`no_candidate_in_scope`. The selector is never asked. The intent reviewer runs only when the
consent promised it (`preference_review`), and a promised review without a reviewer does not buy.

The ledger, the store and the purchase are real SQLite; the merchant, the offers, the Agent Key
and D2/D3-HNP are the doubles of tests/support/hnp_executor.py. The `-rev` cells use the
product's model reviewer and selector over the scripted `ModelServer`: no request leaves the
process and no model is called.
"""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from pydantic import SecretStr

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application import PurchaseService
from commerce_agent.application.hnp_execution import HnpExecutionService, NullHnpPolicyGate
from commerce_agent.application.hnp_intent_review import REVIEW_STAGE, HnpIntentReviewer
from commerce_agent.application.hnp_selection import HnpSelector, ModelHnpSelector
from commerce_agent.approval import ApprovalService
from commerce_agent.delegation.models import ProposalDraft
from commerce_agent.delegation.service import DelegationConsentRequest, DelegationProposalRequest
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.hnp_models import ConsentScope, consent_scope_bytes
from commerce_agent.llm.delegated_choice import ClaudeDelegatedChooser
from commerce_agent.llm.intent_review import ModelHnpIntentReviewer
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.conditional_sqlite import ConditionalRecord
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger, utc_text
from commerce_agent.persistence.model_requests_sqlite import SqliteModelRequests
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from commerce_agent.runtime import AgentRuntime, build_runtime
from commerce_evaluation.stack.fake_checkout import details
from tests.delegation.test_hnp_runtime import governed_hnp
from tests.support.conditional import (
    PRICES,
    SHIPPING,
    T_A,
    T_B,
    A,
    B,
    C,
    FakeOfferReader,
    acp_error,
    offer,
    phone_feed,
)
from tests.support.delegation import START, AuthorityClock, Ids, consent_request
from tests.support.hnp_dispatch import HnpMerchant, Psp
from tests.support.hnp_executor import (
    CHECKOUT_EXPIRES,
    SCOPE,
    AgentKey,
    Bench,
    Freshness,
    Gate,
    Merchant,
)
from tests.support.model_server import ModelServer, structured
from tests.test_delegation_api import MERCHANT, hnp_settings, proposal_body

OWNER = "buyer-a"
PREFERENCE = "Quiero el Xperia; si no hay, el OnePlus."
# Never sent anywhere: the scripted transport answers every request in the process.
API_KEY = SecretStr("test-key-never-sent")
OFFER_READ = ("POST", "/lab/offers/get")
CREATE = ("POST", "/checkout_sessions")
LEDGER_EFFECTS = (
    "purchases",
    "approvals",
    "orders",
    "completion_results",
    "grant_reservations",
    "dispatch_outbox",
)
Mode = Literal["off", "enforce"]


# The bench: a grant over the phone pair, consented with or without a priority.


def priority_scope(
    priority: tuple[str, ...] | None,
    *,
    variant_ids: tuple[str, ...],
    budget: int,
    preference_text: str | None,
    preference_review: bool,
) -> str:
    data = json.loads(SCOPE.read_text(encoding="utf-8"))
    expires = utc_text(START + timedelta(hours=1))
    data["presented_at"] = utc_text(START)
    data["expires_at"] = data["display"]["expires_at"] = expires
    data["preference_text"] = data["display"]["preference_text"] = preference_text
    data["scope"]["variant_ids"] = sorted(variant_ids)
    data["scope"]["total_max_minor"] = data["display"]["total_max"]["minor"] = budget
    data["display"]["variants"] = [
        {"variant_id": variant, "title": f"Teléfono {variant}"} for variant in sorted(variant_ids)
    ]
    if priority is not None:
        data["priority"] = data["display"]["priority"] = list(priority)
    if preference_review:
        data["display"]["preference_review"] = True
    return consent_scope_bytes(parse_model(ConsentScope, canonical_bytes(data))).decode()


def phone_checkout(variant_id: str, status: str, total: int) -> CheckoutSession:
    price = PRICES[variant_id]
    return CheckoutSession.model_validate(
        {
            "id": f"cs_{variant_id}",
            "protocol": {"version": "2026-04-17"},
            "status": status,
            "currency": "cop",
            "capabilities": {"payment": {"handlers": []}},
            "line_items": [
                {
                    "id": "li_1",
                    "item": {"id": variant_id, "name": variant_id, "unit_amount": price},
                    "quantity": 1,
                    "totals": [{"type": "total", "display_text": "Item", "amount": price}],
                }
            ],
            "totals": [{"type": "total", "display_text": "Total", "amount": total}],
            "fulfillment_options": [{"type": "shipping", "id": "ship_1"}],
            "selected_fulfillment_options": [],
            "messages": [],
            "links": [],
            "created_at": "2026-10-21T15:00:00Z",
            "updated_at": "2026-10-21T15:00:00Z",
            "expires_at": CHECKOUT_EXPIRES,
        }
    )


class PhoneMerchant(Merchant):
    """Prepares the checkout of any phone at its offer's total, or at `totals[variant]`."""

    def __init__(self, totals: dict[str, int] | None = None) -> None:
        super().__init__()
        self.totals = dict(totals or {})
        self.created: list[str] = []

    def _session(self, variant_id: str, status: str) -> CheckoutSession:
        total = self.totals.get(variant_id, PRICES[variant_id] + SHIPPING)
        return phone_checkout(variant_id, status, total)

    def create_checkout(
        self, variant_id: str, *, currency: str, idempotency_key: str
    ) -> CheckoutSession:
        self.calls.append(("create", idempotency_key))
        self.created.append(variant_id)
        self.current = self._session(variant_id, "not_ready_for_payment")
        return self.current

    def update_checkout(self, checkout_id, update, *, idempotency_key):
        self.calls.append(("update", idempotency_key))
        self.current = self._session(self.created[-1], "ready_for_payment")
        return self.current

    def get_checkout(self, checkout_id: str) -> CheckoutSession:
        self.calls.append(("get", checkout_id))
        return self.current

    def cancel_checkout(
        self, checkout_id: str, *, idempotency_key: str, reason_code: str | None = None
    ) -> CheckoutSession:
        self.calls.append(("cancel", idempotency_key))
        self.current = self.current.model_copy(update={"status": "canceled"})
        return self.current


class Recording:
    """`PurchaseService.start`, keeping the variant and the intent of each call."""

    def __init__(self, inner: PurchaseService) -> None:
        self.inner = inner
        self.started: list[tuple[str, PurchaseIntent]] = []

    def start(self, **request: Any):
        self.started.append((request["selected_variant_id"], request["intent"]))
        return self.inner.start(**request)


class NeverAsked:
    """A selector the priority path must never consult."""

    def __init__(self) -> None:
        self.calls = 0

    def choose(self, shortlist, intent, preference_text) -> str:
        self.calls += 1
        raise AssertionError("The selector was asked")


def offers_of(**changes: Any) -> FakeOfferReader:
    """Both offers as the merchant serves them; `changes` replaces one by variant."""
    answers: dict[str, Any] = {A: offer(A), B: offer(B), C: offer(C)}
    return FakeOfferReader(answers | {_VARIANT[key]: value for key, value in changes.items()})


_VARIANT = {"a": A, "b": B, "c": C}


class PriorityBench(Bench):
    """`Bench` over the phone pair, with the consent, the feed and the offers of each cell."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        priority: tuple[str, ...] | None = (A, B),
        variant_ids: tuple[str, ...] | None = None,
        budget: int = max(T_A, T_B),
        mode: Mode = "enforce",
        preference_text: str | None = None,
        preference_review: bool = False,
        offers: FakeOfferReader | None = None,
        no_offers: bool = False,
        feed: ProductFeed | None = None,
        merchant: PhoneMerchant | None = None,
        reviewer: HnpIntentReviewer | None = None,
        selector: HnpSelector | None = None,
    ) -> None:
        self.path = tmp_path / "agent.db"
        self.mode = mode
        self.clock = AuthorityClock()
        self.ledger = SqliteDelegationLedger(self.path, clock=self.clock, id_factory=Ids())
        self.ledger.initialize()
        self.store = SqliteAgentStore(self.path)
        self.store.initialize()
        scope = priority_scope(
            priority,
            variant_ids=variant_ids or priority or (A, B),
            budget=budget,
            preference_text=preference_text,
            preference_review=preference_review,
        )
        proposal = ProposalDraft(
            proposal_id="dpr_1",
            owner_id=OWNER,
            scope_json=scope,
            scope_digest=sha256_digest(b"commerce-delegation-scope/v1\0" + scope.encode()),
            checkout_details_json=details().model_dump_json(),
            presented_at=utc_text(START),
            expires_at=utc_text(START + timedelta(seconds=600)),
        )
        self.ledger.create_proposal(proposal)
        self.intent = PurchaseIntent(
            hard=HardConstraints(currency="COP", max_total_amount_minor=budget)
        )
        consent = replace(consent_request(proposal), intent_json=self.intent.model_dump_json())
        result = self.ledger.consent(consent)
        assert result.task is not None and result.grant is not None, result
        self.task_id, self.grant_id = result.task.task_id, result.grant.grant_id
        self.feed = feed or phone_feed()
        self.merchant = merchant or PhoneMerchant()
        self.closer = AgentKey()
        self.gate = NullHnpPolicyGate() if mode == "off" else Gate()
        self.freshness = None if mode == "off" else Freshness()
        self.offers = None if no_offers else offers or offers_of()
        self.selector = selector or NeverAsked()
        approvals = ApprovalService(self.store, SecretStr("approval-secret-with-at-least-32-chars"))
        self.purchases = Recording(
            PurchaseService(
                feed=self.feed, checkout=self.merchant, store=self.store, approvals=approvals
            )
        )
        self.service = HnpExecutionService(
            mode=mode,
            ledger=self.ledger,
            store=self.store,
            purchases=self.purchases,
            checkout=self.merchant,
            closer=self.closer,
            gate=self.gate,
            selector=self.selector,
            feed=lambda: self.feed,
            clock=self.clock,
            id_factory=Ids(),
            freshness=self.freshness,
            d1_denials=None if mode == "off" else (lambda run_id: ()),
            reviewer=reviewer,
            offers=self.offers,
        )

    def reads(self) -> list[str]:
        assert self.offers is not None
        return self.offers.read_ids

    def records(self) -> tuple[ConditionalRecord, ...]:
        return self.store.conditional_records(self.run_id())

    def decision(self) -> dict[str, Any]:
        decisions = [record for record in self.records() if record.kind == "decision"]
        assert len(decisions) == 1, decisions
        return decisions[0].body

    def statuses(self) -> list[str]:
        return [branch["status"] for branch in self.decision()["branches"]]

    def checks(self) -> list[str]:
        return [record.outcome for record in self.records() if record.kind == "checkout_check"]

    def reviews(self) -> list[dict[str, Any]]:
        return [
            dict(event.body)
            for event in self.ledger.events(self.grant_id)
            if event.event_type == "choice_reviewed"
        ]

    def counts(self) -> dict[str, int]:
        with closing(sqlite3.connect(self.path)) as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in LEDGER_EFFECTS
            }

    def bought(self) -> str | None:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT selected_variant_id FROM purchases").fetchone()
        return None if row is None else row[0]


def assert_zero_effects(bench: PriorityBench, *, purchases: int = 0) -> None:
    """§4.4 on the agent's side: nothing created, signed, reserved or committed, and one
    `decision` row. The offer reads are counted apart; they are the evidence of what was read."""
    assert bench.merchant.calls == []  # no POST or GET to the checkout API of any kind
    assert bench.closer.signatures == 0
    assert bench.counts() == {table: 0 for table in LEDGER_EFFECTS} | {"purchases": purchases}
    kinds = sorted(mandate.kind for mandate in bench.ledger.mandates(bench.grant_id))
    assert kinds == ["open_checkout", "open_payment"]  # no closed mandate
    assert bench.checks() == []
    bench.decision()  # exactly one decision row
    if isinstance(bench.gate, Gate):
        assert bench.gate.calls == []


# UX04-HNP: the consented order wins over the cheaper variant.


@pytest.mark.parametrize("mode", ["enforce", "off"])
def test_ux04_hnp_buys_the_first_option_even_if_the_second_is_cheaper(
    tmp_path: Path, mode: Mode
) -> None:
    assert T_B < T_A
    bench = PriorityBench(tmp_path, mode=mode)

    assert bench.run().status == "applied"  # UoW-2 committed: the dispatch would follow

    assert bench.ended() == ("running", None)
    assert bench.reads() == [A]  # B is never read when A decides
    assert bench.merchant.created == [A] and bench.effects().creates == 1
    assert bench.effects().commits == 1
    assert bench.bought() == A
    body = bench.decision()
    assert (body["outcome"], body["label"], body["chosen_index"]) == ("branch", "A", 0)
    assert (body["purchase_mode"], body["governance_mode"]) == ("HNP", mode)
    assert bench.statuses() == ["eligible", "not_evaluated"]
    assert bench.checks() == ["checkout_matches"]
    assert bench.reviews() == [] and "choice_reviewed" not in bench.events()
    assert isinstance(bench.selector, NeverAsked) and bench.selector.calls == 0


def test_ux04_hnp_starts_the_purchase_with_the_grants_own_intent(tmp_path: Path) -> None:
    """The branch's effective intent is the grant's: COP and its total, nothing wider (§3.1)."""
    bench = PriorityBench(tmp_path)

    bench.run()

    assert bench.purchases.started == [(A, bench.intent)]
    assert bench.decision()["branches"][0]["limit_minor"] == max(T_A, T_B)


def test_the_equal_total_is_within_the_grant(tmp_path: Path) -> None:
    bench = PriorityBench(tmp_path, budget=T_A)

    assert bench.run().status == "applied"

    assert bench.reads() == [A] and bench.bought() == A


# UX05-HNP and UX06-HNP: nothing for sale within the total escalates before any effect.


def test_ux05_hnp_escalates_when_no_option_fits_the_total(tmp_path: Path) -> None:
    bench = PriorityBench(tmp_path, budget=min(T_A, T_B) - 1)

    assert bench.run().status == "closed"

    assert bench.ended() == ("escalated", "no_candidate_in_scope")
    assert bench.decision()["outcome"] == "none"
    assert bench.statuses() == ["over_limit", "over_limit"]
    assert bench.reads() == [A, B]  # C, outside the grant, is never read
    assert_zero_effects(bench)
    grant = bench.execution().grant
    assert (grant.state, grant.revoked_reason) == ("revoked", "escalated")


def test_ux06_hnp_reads_the_second_option_after_the_first_is_sold_out(tmp_path: Path) -> None:
    """D6: the second is read because it comes second, not because the first sold out."""
    bench = PriorityBench(tmp_path, budget=T_B - 1, offers=offers_of(a=offer(A, stock=0)))

    bench.run()

    assert bench.ended() == ("escalated", "no_candidate_in_scope")
    assert bench.statuses() == ["out_of_stock", "over_limit"]
    assert bench.reads() == [A, B]
    assert_zero_effects(bench)


def test_ux06_hnp_ctl_buys_the_second_option(tmp_path: Path) -> None:
    bench = PriorityBench(tmp_path, budget=T_B, offers=offers_of(a=offer(A, stock=0)))

    assert bench.run().status == "applied"

    assert bench.reads() == [A, B]
    assert bench.merchant.created == [B] and bench.bought() == B
    body = bench.decision()
    assert (body["outcome"], body["label"], body["chosen_index"]) == ("branch", "B", 1)
    assert bench.checks() == ["checkout_matches"]


def test_an_inactive_first_option_is_not_for_sale(tmp_path: Path) -> None:
    bench = PriorityBench(tmp_path, offers=offers_of(a=offer(A, status="inactive")))

    bench.run()

    assert bench.statuses() == ["inactive", "eligible"]
    assert bench.bought() == B


def test_b0_hnp_fails_with_the_same_code(tmp_path: Path) -> None:
    bench = PriorityBench(tmp_path, mode="off", budget=min(T_A, T_B) - 1)

    bench.run()

    assert bench.ended() == ("failed", "no_candidate_in_scope")
    assert bench.decision()["governance_mode"] == "off"
    assert_zero_effects(bench)


# An option that cannot be verified stops the run before the next one (D3).


@pytest.mark.parametrize(
    "failure",
    [
        acp_error("http_error", 503),
        acp_error("timeout"),
        acp_error("transport_error"),
        acp_error("http_error", 401),
        acp_error("http_error", 429),
        acp_error("offer_expired"),
        acp_error("invalid_offer"),  # OFFER_NOT_FOUND
        RuntimeError("anything else"),
    ],
    ids=["503", "timeout", "transport", "401", "429", "expired", "not-found", "other"],
)
def test_an_unreadable_first_option_never_falls_to_the_second(
    tmp_path: Path, failure: Exception
) -> None:
    bench = PriorityBench(tmp_path, offers=offers_of(a=failure))

    assert bench.run().status == "closed"

    assert bench.ended() == ("escalated", "selection_unavailable")
    assert bench.decision()["outcome"] == "undetermined"
    assert bench.statuses() == ["unknown", "not_evaluated"]
    assert bench.reads() == [A]
    assert_zero_effects(bench)
    # No model failed: the terminal names no `failure_kind`.
    [finished] = [
        e.body for e in bench.ledger.events(bench.grant_id) if e.event_type == "task_finished"
    ]
    assert "failure_kind" not in finished


def test_an_offer_the_feed_calls_unavailable_is_undetermined(tmp_path: Path) -> None:
    """`start` re-ranks the feed: an offer for sale that the feed says is not is a disagreement."""
    bench = PriorityBench(tmp_path, feed=phone_feed(unavailable=[A]))

    bench.run()

    assert bench.ended() == ("escalated", "selection_unavailable")
    [first, _] = bench.decision()["branches"]
    assert (first["status"], first["error_code"]) == ("unknown", "feed_offer_mismatch")
    assert bench.reads() == [A]
    assert_zero_effects(bench)


def test_a_first_option_missing_from_the_feed_never_falls_to_the_second(tmp_path: Path) -> None:
    """A priority variant the feed no longer has is a clarification: nothing is read."""
    bench = PriorityBench(tmp_path, feed=phone_feed(only=[B, C]))

    bench.run()

    assert bench.ended() == ("escalated", "selection_unavailable")
    body = bench.decision()
    assert body["outcome"] == "clarify"
    assert (body["clarify"]["cause"], body["clarify"]["branch_index"]) == ("branch_not_found", 0)
    assert bench.reads() == []
    assert_zero_effects(bench)


def test_without_an_offer_reader_nothing_is_bought(tmp_path: Path) -> None:
    bench = PriorityBench(tmp_path, no_offers=True)

    bench.run()

    assert bench.ended() == ("escalated", "selection_unavailable")
    assert bench.merchant.calls == [] and bench.counts()["purchases"] == 0


# D14: an option against the grant's intent is never skipped to buy the next one.


def test_a_first_option_against_the_intent_stops_the_run_and_reads_only_the_second(
    tmp_path: Path,
) -> None:
    """«A intent_mismatch, B eligible»: B is read once and the run escalates, buying nothing."""
    bench = PriorityBench(tmp_path, feed=phone_feed(currency={A: "USD"}))

    bench.run()

    assert bench.ended() == ("escalated", "selection_unavailable")
    body = bench.decision()
    assert body["outcome"] == "clarify"
    assert (body["clarify"]["cause"], body["clarify"]["branch_index"]) == ("intent_mismatch", 0)
    assert bench.statuses() == ["intent_mismatch", "eligible"]
    assert body["branches"][0]["rejection_code"] == "currency"
    assert bench.reads() == [B]
    assert_zero_effects(bench)


def test_a_first_option_against_the_intent_and_a_second_over_the_total_is_none(
    tmp_path: Path,
) -> None:
    bench = PriorityBench(tmp_path, budget=T_B - 1, feed=phone_feed(currency={A: "USD"}))

    bench.run()

    assert bench.ended() == ("escalated", "no_candidate_in_scope")
    assert bench.statuses() == ["intent_mismatch", "over_limit"]
    assert bench.reads() == [B]
    assert_zero_effects(bench)


def test_a_second_option_against_the_intent_does_not_matter_once_the_first_decides(
    tmp_path: Path,
) -> None:
    bench = PriorityBench(tmp_path, feed=phone_feed(currency={B: "USD"}))

    assert bench.run().status == "applied"

    assert bench.statuses() == ["eligible", "not_evaluated"]
    assert bench.reads() == [A] and bench.bought() == A


# §3.4: the checkout must carry the total the offer gave.


def test_a_checkout_total_that_differs_from_the_offer_escalates_and_cancels_it(
    tmp_path: Path,
) -> None:
    """Strictly (D4): a lower total is a change too, and no other option is tried."""
    bench = PriorityBench(tmp_path, merchant=PhoneMerchant({A: T_A - 1}))

    assert bench.run().status == "closed"

    assert bench.ended() == ("escalated", "offer_changed")
    assert bench.checks() == ["offer_changed"]
    [check] = [r.body for r in bench.records() if r.kind == "checkout_check"]
    assert (check["offer_total_minor"], check["checkout_total_minor"]) == (T_A, T_A - 1)
    assert bench.merchant.created == [A] and bench.reads() == [A]
    effects = bench.effects()
    assert (effects.creates, effects.cancels, effects.signatures, effects.commits) == (1, 1, 0, 0)
    assert isinstance(bench.gate, Gate) and bench.gate.calls == []
    assert bench.freshness is not None and bench.freshness.stages == []
    assert bench.purchase() == ("canceled", None)


def test_b0_hnp_leaves_a_changed_checkout_open_until_it_expires(tmp_path: Path) -> None:
    bench = PriorityBench(tmp_path, mode="off", merchant=PhoneMerchant({A: T_A - 1}))

    bench.run()

    assert bench.ended() == ("failed", "offer_changed")
    assert bench.checks() == ["offer_changed"]
    assert (bench.effects().creates, bench.effects().cancels) == (1, 0)
    assert bench.purchase() == ("failed", "offer_changed")


def test_a_checkout_over_the_total_is_an_offer_change_too(tmp_path: Path) -> None:
    """The offer fit the total and the checkout does not: the budget guard cancels it, and the
    offer changed between the read and the creation (§3.4)."""
    bench = PriorityBench(tmp_path, budget=T_A, merchant=PhoneMerchant({A: T_A + 1}))

    bench.run()

    assert bench.ended() == ("escalated", "offer_changed")
    assert bench.reads() == [A]
    assert (bench.effects().creates, bench.effects().cancels) == (1, 1)  # the guard's only
    assert bench.purchase() == ("canceled", None)


class OtherVariant(Recording):
    """A `start` that hands back a checkout of another variant than the one it was asked for,
    which `PurchaseService` itself refuses (`checkout_mismatch`)."""

    def start(self, **request: Any):
        prepared = super().start(**request)
        other = phone_checkout(B, "ready_for_payment", T_B)
        return prepared.model_copy(update={"checkout": other})


def test_a_checkout_of_another_variant_ends_the_run_with_its_terminal(tmp_path: Path) -> None:
    """Not an exception without a trace: the task ends `offer_changed`, its `task_finished`
    is the record, and its escalation cancels that checkout."""
    bench = PriorityBench(tmp_path)
    bench.service._purchases = OtherVariant(bench.purchases.inner)

    assert bench.run().status == "closed"

    assert bench.ended() == ("escalated", "offer_changed")
    assert "task_finished" in bench.events()
    assert bench.checks() == []  # `verify_checkout` writes no row for another variant
    effects = bench.effects()
    assert (effects.creates, effects.cancels, effects.signatures, effects.commits) == (1, 1, 0, 0)
    assert bench.purchase() == ("canceled", None)


# D8: the reviewer runs only when the consent promised it.


def model(
    path: Path, monkeypatch: pytest.MonkeyPatch, *answers: Any
) -> tuple[ModelHnpSelector, ModelHnpIntentReviewer, ModelServer]:
    """The product's model selector and reviewer, both over one scripted server."""
    server = ModelServer(*answers)
    server.install(monkeypatch)
    timings = SqliteStageTimings(path)
    timings.initialize()
    timings.initialize_model_tokens()
    requests = SqliteModelRequests(path)
    requests.initialize()
    chooser = ClaudeDelegatedChooser(api_key=API_KEY, model="test-model", model_requests=requests)
    reviewer = ModelHnpIntentReviewer(
        api_key=API_KEY, model="test-model", timer=timings, model_requests=requests
    )
    return ModelHnpSelector(chooser, timings), reviewer, server


def verdict(decision: str, reason: str) -> Any:
    return structured(lambda body: {"decision": decision, "reason": reason})


def model_stages(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return [row[0] for row in connection.execute("SELECT stage FROM model_requests")]


def test_ux04_hnp_rev_buys_the_first_option_after_the_review_confirms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selector, reviewer, server = model(
        tmp_path / "agent.db", monkeypatch, verdict("confirmed", "matches_request")
    )
    bench = PriorityBench(
        tmp_path,
        preference_text=PREFERENCE,
        preference_review=True,
        reviewer=reviewer,
        selector=selector,
    )

    assert bench.run().status == "applied"

    assert bench.bought() == A and bench.merchant.created == [A]
    assert [(r["decision"], r["reason"]) for r in bench.reviews()] == [
        ("confirmed", "matches_request")
    ]
    assert len(server.requests) == 1  # the reviewer's; the selector is never asked
    assert model_stages(bench.path) == [REVIEW_STAGE]
    reviewed = json.loads(server.bodies()[0]["messages"][0]["content"])
    assert (reviewed["chosen_variant_id"], reviewed["preference_text"]) == (A, PREFERENCE)
    events = bench.events()
    assert events.index("choice_reviewed") < events.index("reservation_reserved")


def test_ux04_hnp_rev_no_escalates_with_one_read_and_nothing_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selector, reviewer, server = model(
        tmp_path / "agent.db", monkeypatch, verdict("not_confirmed", "contradicts_preference")
    )
    bench = PriorityBench(
        tmp_path,
        preference_text=PREFERENCE,
        preference_review=True,
        reviewer=reviewer,
        selector=selector,
    )

    assert bench.run().status == "closed"

    assert bench.ended() == ("escalated", "choice_not_confirmed")
    assert bench.reads() == [A]
    assert len(server.requests) == 1
    assert [r["reason"] for r in bench.reviews()] == ["contradicts_preference"]
    # `_review` creates the purchase before reviewing, and the terminal closes it (§4.4).
    assert_zero_effects(bench, purchases=1)
    assert bench.purchase() == ("canceled", None)
    assert bench.decision()["outcome"] == "branch"


def test_a_reviewer_without_a_promised_review_is_not_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What is consented rules: a reviewer configured later does not review this grant."""
    selector, reviewer, server = model(tmp_path / "agent.db", monkeypatch)
    bench = PriorityBench(
        tmp_path, preference_text=PREFERENCE, reviewer=reviewer, selector=selector
    )

    assert bench.run().status == "applied"

    assert bench.bought() == A
    assert server.requests == [] and bench.reviews() == []


def test_a_reviewer_without_a_preference_text_is_not_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selector, reviewer, server = model(tmp_path / "agent.db", monkeypatch)
    bench = PriorityBench(tmp_path, reviewer=reviewer, selector=selector)

    assert bench.run().status == "applied"

    assert bench.bought() == A
    assert server.requests == [] and model_stages(bench.path) == []


def test_no_review_without_a_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selector, reviewer, server = model(tmp_path / "agent.db", monkeypatch)
    bench = PriorityBench(
        tmp_path,
        budget=min(T_A, T_B) - 1,
        preference_text=PREFERENCE,
        preference_review=True,
        reviewer=reviewer,
        selector=selector,
    )

    bench.run()

    assert bench.ended() == ("escalated", "no_candidate_in_scope")
    assert server.requests == [] and bench.reviews() == []


# D15: a promised review without a reviewer never buys, with or without a priority.


class Cheapest:
    """The deterministic selector's rule on the phone feed: the first of the shortlist."""

    def choose(self, shortlist, intent, preference_text) -> str:
        return shortlist[0].candidate.variant_id


def test_d15_a_promised_review_without_a_reviewer_escalates(tmp_path: Path) -> None:
    bench = PriorityBench(tmp_path, preference_text=PREFERENCE, preference_review=True)

    assert bench.run().status == "closed"

    assert bench.ended() == ("escalated", "choice_not_confirmed")
    assert bench.reviews() == [] and "choice_reviewed" not in bench.events()
    assert_zero_effects(bench)
    assert bench.reads() == [A]


def test_d15_without_a_priority_a_promised_review_without_a_reviewer_escalates(
    tmp_path: Path,
) -> None:
    bench = PriorityBench(
        tmp_path,
        priority=None,
        variant_ids=(A, B),
        preference_text=PREFERENCE,
        preference_review=True,
        selector=Cheapest(),
    )

    assert bench.run().status == "closed"

    assert bench.ended() == ("escalated", "choice_not_confirmed")
    assert bench.reviews() == []
    assert bench.merchant.calls == [] and bench.counts()["purchases"] == 0
    assert bench.reads() == []  # without a priority nothing is read
    assert bench.records() == ()


def test_d15_b0_hnp_fails_a_promised_review(tmp_path: Path) -> None:
    """A grant proposed in B2-HNP with the reviewer, run by a B0-HNP executor: it fails."""
    bench = PriorityBench(tmp_path, mode="off", preference_text=PREFERENCE, preference_review=True)

    bench.run()

    assert bench.ended() == ("failed", "choice_not_confirmed")
    assert bench.merchant.calls == []


def test_without_a_priority_the_executor_is_unchanged(tmp_path: Path) -> None:
    """A consent without `priority` takes today's path: the cheapest, no offer read, no record."""
    bench = PriorityBench(tmp_path, priority=None, variant_ids=(A, B), selector=Cheapest())

    assert bench.run().status == "applied"

    assert bench.bought() == B  # cheaper than A: today's shortlist order
    assert bench.reads() == [] and bench.records() == ()


# The runtime wires the ACP client as the offer reader.


def runtime(settings: Any, merchant: HnpMerchant, psp: Psp) -> AgentRuntime:
    return build_runtime(
        settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(psp),
    )


@pytest.mark.parametrize("governed", [False, True], ids=["b0-hnp", "b2-hnp"])
def test_the_runtime_reads_the_offer_before_the_one_checkout(
    tmp_path: Path, governed: bool
) -> None:
    merchant, psp = HnpMerchant(MERCHANT), Psp()
    settings = governed_hnp(tmp_path) if governed else hnp_settings(tmp_path)
    built = runtime(settings, merchant, psp)
    try:
        assert built.delegations is not None and built.hnp_executor is not None
        assert built.hnp_executor._offers is built.client
        body = proposal_body(priority=["SON-01"])
        request = DelegationProposalRequest.model_validate_json(json.dumps(body))
        proposal = built.delegations.propose(OWNER, request)
        assert proposal.scope.priority == ("SON-01",)
        consent = DelegationConsentRequest(
            proposal_id=proposal.proposal_id, scope_digest=proposal.scope_digest
        )
        view, _ = built.delegations.consent(OWNER, consent)
        step = built.hnp_executor.run(view.task.task_id)
    finally:
        built.close()

    assert step.status == "closed" and step.execution is not None
    assert step.execution.task.state == "purchased"
    calls = [call for call in merchant.requests if call in (OFFER_READ, CREATE)]
    # One decision read before the one creation; B2-HNP's `close` freshness reads it again.
    assert calls == [OFFER_READ, CREATE, *([OFFER_READ] if governed else [])]
    run_id = step.execution.task.run_id
    assert run_id is not None
    store = SqliteAgentStore(settings.agent_database_path)
    assert [r.outcome for r in store.conditional_records(run_id)] == [
        "branch",
        "checkout_matches",
    ]
