"""Human Not Present purchases on the live stack (docs/design/hnp-governed.md §12, HNP-I1).

The `hnp` lane: the real Merchant behind its FaultProxy, the real payment provider and, per
episode, the Agent API with AGENT_HNP_ENABLED in a process of its own (`HnpAgent`), assembled by
production `create_app` from its variables. The buyer proposes and consents by API with its token;
the service token triggers the task; the deterministic selector picks. No model is configured.
"""

import re
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_checkout_mandate import (
    AllowedMerchants,
    Item,
    LineItemRequirements,
    LineItems,
    OpenCheckoutMandate,
)
from ap2.sdk.generated.open_payment_mandate import (
    AllowedPayees,
    AllowedPaymentInstruments,
    AmountRange,
    Budget,
    OpenPaymentMandate,
    PaymentReference,
)
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.sdjwt import common, sd_jwt
from jwcrypto.jwk import JWK

from commerce_agent.acp import ACPClient, ACPClientError, load_product_feed
from commerce_agent.acp.models import CheckoutSession, SandboxPaymentData
from commerce_agent.ap2 import checkout_hash
from commerce_agent.ap2_hnp import (
    LINE_ID,
    MERCHANT_ID,
    MERCHANT_NAME,
    PAYMENT_AUDIENCE,
    AcceptableItem,
    AgentKeyCloser,
    OpenMandateIssuer,
    present_closed_mandate,
)
from commerce_agent.delegated_payment import DelegatedPaymentClient, DelegationError
from commerce_agent.delegation.contract import HNP_MESSAGES, escalation_message
from commerce_agent.delegation.models import LEASE_SECONDS, token_digest
from commerce_agent.delegation.views import format_cop
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_evaluation.verify_evidence import verify_database
from commerce_evaluation.verify_evidence_hnp import HNP_LINKS
from tests.integration.live_purchase import (
    HnpAgent,
    PurchaseLane,
    ap2_key,
    await_call,
    buyer_details,
    delegation_request,
    hnp_agent,
    on_checkout,
)
from tests.integration.test_live_purchase import OTHER_PHONE, PHONE, agent, payments
from tests.support.ap2_hnp_fixtures import (
    INSTRUMENT,
    OTHER_MERCHANT,
    AllowedRegions,
    OpenCheckoutBeyondProfile,
    splice,
)

# `format_cop`, the UI's notation (K16): "$", a no-break space, dots for thousands, ",00", COP.
COP = re.compile(r"\$\N{NO-BREAK SPACE}(\d{1,3}(?:\.\d{3})*),(\d{2}) COP")
# Room over the catalog price for shipping: the scope's maximum includes it (§4.2).
SHIPPING_ROOM_MINOR = 500_000_00


def catalog(feed: Path, variant: str) -> tuple[str, int]:
    """The feed title and observed price of a variant, as the delegation form shows them."""
    candidate = next(
        item for item in load_product_feed(feed).candidates if item.variant_id == variant
    )
    return candidate.variant_title, candidate.observed_unit_amount_minor


def minor_of(text: str) -> int:
    """Read an amount back as a person reads `format_cop`: the pesos, then the two decimals."""
    [(pesos, cents)] = COP.findall(text)
    return int(pesos.replace(".", "")) * 100 + int(cents)


def decisions(agent: HnpAgent, run_id: str) -> list[DecisionRecord]:
    """D1/D2/D3 of the run; B0-HNP (`off`) never creates the evidence table."""
    if not agent.rows("SELECT 1 FROM sqlite_master WHERE name = 'governance_evidence'"):
        return []
    with closing(sqlite3.connect(agent.database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        return [
            item
            for item in SqliteGovernanceEvidenceStore(connection).for_run(run_id)
            if isinstance(item, DecisionRecord)
        ]


def mandates(agent: HnpAgent, grant_id: str) -> list[str]:
    return sorted(
        kind
        for (kind,) in agent.rows(
            "SELECT kind FROM delegation_mandates WHERE grant_id = ?", grant_id
        )
    )


def effects(lane: PurchaseLane) -> dict[str, int]:
    """What only an order or a payment leaves: Merchant orders and checkouts, and PSP tokens."""
    merchant = lane.rpc("effects")
    return {
        "orders": merchant["orders"],
        "checkout_sessions": merchant["checkout_sessions"],
        "tokens": lane.payment_tokens(),
    }


def grown(lane: PurchaseLane, before: dict[str, int]) -> dict[str, int]:
    return {name: value - before[name] for name, value in effects(lane).items()}


def scheduled(agent: HnpAgent, total_max: int, name: str) -> tuple[str, str]:
    """A consented delegation for the phone, up to `total_max`: its grant and its task."""
    _, view = agent.delegate(delegation_request([PHONE], total_max, name=name))
    return view["grant_id"], view["task"]["task_id"]


def reservations(agent: HnpAgent, grant_id: str) -> list[tuple[str, str | None, str | None]]:
    return agent.rows(
        "SELECT state, release_basis, uncertain_reason FROM grant_reservations WHERE grant_id = ?",
        grant_id,
    )


def sent(lane: PurchaseLane, suffix: str) -> int:
    """Requests whose path ends in `suffix`, whatever the checkout."""
    return sum(call.path.endswith(suffix) for call in lane.proxy.calls)


def verified(agent: HnpAgent, lane: PurchaseLane, grant_id: str) -> dict[str, Any]:
    [document] = verify_database(
        agent.database, psp_database=lane.root / "payments.sqlite", grant_id=grant_id
    )
    return document


@pytest.mark.parametrize("condition", ["B0-HNP", "B2-HNP"])
def test_hnp_purchase_completes_with_two_receipts_and_a_verified_chain(hnp, condition):
    """Consent, a delegated run with the deterministic selector, both verifiers and the order.

    The buyer writes «Bogotá» with its accent: the proposal binds the destination as the Merchant
    echoes it («Bogota»), so POL-08 passes in B2-HNP instead of escalating (coordination risk)."""
    with hnp_agent(hnp, f"purchase-{condition.lower()}", condition) as agent:
        title, price = catalog(agent.episode.feed, PHONE)
        total_max = price + SHIPPING_ROOM_MINOR
        before, tokens = hnp.rpc("effects"), hnp.payment_tokens()
        proposal, view = agent.delegate(
            delegation_request([PHONE], total_max, name="hnp-purchase", city="Bogotá")
        )
        assert proposal["scope"]["destination_label"] == "Bogota, DC"
        assert proposal["scope"]["total_max"] == {"minor": total_max, "currency": "COP"}
        assert (view["state"], view["task"]["state"]) == ("active", "scheduled")
        grant_id, task_id = view["grant_id"], view["task"]["task_id"]

        triggered = agent.trigger(task_id)
        assert triggered.status_code == 202, triggered.text
        task = agent.finished(task_id)
        assert (task["state"], task["outcome_code"]) == ("purchased", None), task
        order_id, run_id = task["order_id"], task["run_id"]
        # «Compré <título> por <total>…»: the reserved total, in the UI's notation with «,00».
        [(amount,)] = agent.rows(
            "SELECT amount_minor FROM grant_reservations WHERE grant_id = ?", grant_id
        )
        assert price < amount <= total_max
        assert task["message"] == HNP_MESSAGES["task.purchased"].format(
            title=title, total=format_cop(amount), order_id=order_id
        )
        assert minor_of(task["message"]) == amount
        assert agent.delegation(grant_id)["state"] == "consumed"

        receipts = agent.rows(
            "SELECT checkout_receipt_jwt, payment_receipt_jwt FROM completion_results "
            "WHERE run_id = ?",
            run_id,
        )
        assert len(receipts) == 1 and all(receipts[0])
        assert mandates(agent, grant_id) == [
            "closed_checkout",
            "closed_payment",
            "open_checkout",
            "open_payment",
        ]
        after = hnp.rpc("effects")
        assert after["orders"] == before["orders"] + 1
        assert after["checkout_sessions"] == before["checkout_sessions"] + 1
        assert hnp.payment_tokens() == tokens + 1
        assert hnp.proxy.count("POST", "/checkout_sessions") == 1
        checkout_id = agent.rows("SELECT checkout_id FROM dispatch_outbox")[0][0]
        assert hnp.proxy.count("POST", f"/checkout_sessions/{checkout_id}/ap2/complete") == 1
        assert hnp.proxy.count("POST", f"/checkout_sessions/{checkout_id}/complete") == 0

        records = decisions(agent, run_id)
        if condition == "B2-HNP":
            assert [
                (item.decision.phase, item.decision.purchase_mode, item.decision.outcome)
                for item in records
            ] == [("D1", "HNP", "allow"), ("D2", "HNP", "allow"), ("D3", "HNP", "allow")]
            d2 = next(item.decision for item in records if item.decision.phase == "D2")
            assert ("POL-08", "pass") in {(rule.policy_id, rule.result) for rule in d2.rule_results}
        else:
            assert records == []

        # The Merchant's dispatcher posts the order's `order_create` to this Agent's database.
        with hnp.receiver.delivering_to(SqliteAgentStore(agent.database)):
            delivered = hnp.rpc("dispatch_webhooks", limit=50)
        assert delivered and {item["outcome"] for item in delivered} == {"delivered"}

    document = verified(agent, hnp, grant_id)
    assert (document["task_state"], document["order_id"]) == ("purchased", order_id)
    statuses = {name: document["links"][name]["status"] for name in HNP_LINKS}
    terminals = dict.fromkeys(("revocation", "kill_switch", "escalation"), "not_applicable")
    governed = ("intent", "d1_decision", "checkout_created", "freshness", "d2_decision")
    if condition == "B2-HNP":
        assert statuses == dict.fromkeys(HNP_LINKS, "ok") | terminals
        assert document["complete"] is True
    else:
        # B0-HNP has no PDP or freshness: what H2-HNP measures (§9.2).
        missing = dict.fromkeys((*governed, "d3_decision"), "missing")
        assert statuses == dict.fromkeys(HNP_LINKS, "ok") | terminals | missing
        assert document["complete"] is False
    hnp.stack.record(f"hnp:{condition}:purchase", orders_created=1, chain=document["complete"])


@pytest.mark.parametrize("condition", ["B0-HNP", "B2-HNP"])
def test_hnp_price_over_the_limit_escalates_without_signing(hnp, condition):
    """A total under the catalog price: B2-HNP escalates at D1 (POL-11) before any checkout and
    hands the decision back with a chat prefill; B0-HNP has no D1, so its common guard cancels
    the checkout it created and the task fails. Neither signs a closed mandate."""
    with hnp_agent(hnp, f"over-{condition.lower()}", condition) as agent:
        title, price = catalog(agent.episode.feed, PHONE)
        total_max = price - 1_000_00  # a thousand pesos under the catalog price
        before = effects(hnp)
        grant_id, task_id = scheduled(agent, total_max, "hnp-over")
        assert agent.trigger(task_id).status_code == 202
        task = agent.finished(task_id)
        delegation = agent.delegation(grant_id)
        if condition == "B2-HNP":
            assert (task["state"], task["outcome_code"]) == ("escalated", "budget_not_satisfiable")
            assert (delegation["state"], delegation["revoked_reason"]) == ("revoked", "escalated")
            escalation = delegation["escalation"]
            assert escalation["message"] == escalation_message("budget_not_satisfiable")
            # «Quiero comprar <título> por hasta <total>»: the consented maximum with «,00».
            assert escalation["prefill_request"] == HNP_MESSAGES[
                "escalation.prefill_request"
            ].format(title=title, total=format_cop(total_max))
            assert minor_of(escalation["prefill_request"]) == total_max
            [d1] = [item.decision for item in decisions(agent, task["run_id"])]
            assert (d1.phase, d1.outcome) == ("D1", "deny")
            assert ("POL-11", "fail") in {(rule.policy_id, rule.result) for rule in d1.rule_results}
            assert hnp.proxy.count("POST", "/checkout_sessions") == 0
            assert grown(hnp, before) == {"orders": 0, "checkout_sessions": 0, "tokens": 0}
        else:
            assert (task["state"], task["outcome_code"]) == ("failed", "budget_guard")
            assert (delegation["state"], delegation["revoked_reason"]) == ("revoked", "failed")
            assert delegation["escalation"] is None
            assert hnp.proxy.count("POST", "/checkout_sessions") == 1
            assert sent(hnp, "/cancel") == 1
            assert grown(hnp, before) == {"orders": 0, "checkout_sessions": 1, "tokens": 0}
        assert mandates(agent, grant_id) == ["open_checkout", "open_payment"]
        assert reservations(agent, grant_id) == []
        assert sent(hnp, "/ap2/checkout-jwt") == sent(hnp, "/ap2/complete") == 0
    if condition == "B2-HNP":
        document = verified(agent, hnp, grant_id)
        assert document["links"]["escalation"]["status"] == "ok", document["links"]
        assert document["complete"] is True, document["links"]
    hnp.stack.record(f"hnp:{condition}:over_limit", outcome=task["outcome_code"])


def test_hnp_two_triggers_at_once_make_one_order(hnp):
    """The scheduler fires twice at the same moment: one claim wins the lease, the other gets
    409 `hnp_task_not_runnable`, and the Merchant and the PSP see a single purchase."""
    with hnp_agent(hnp, "double", "B2-HNP") as agent:
        _, price = catalog(agent.episode.feed, PHONE)
        before = effects(hnp)
        grant_id, task_id = scheduled(agent, price + SHIPPING_ROOM_MINOR, "hnp-double")
        answers = agent.trigger_at_once(task_id)
        assert sorted(answer.status_code for answer in answers) == [202, 409], answers
        [refused] = [answer for answer in answers if answer.status_code == 409]
        assert refused.json()["code"] == "hnp_task_not_runnable"
        task = agent.finished(task_id)
        assert task["state"] == "purchased", task
        again = agent.trigger(task_id)
        assert (again.status_code, again.json()["code"]) == (409, "hnp_task_not_runnable")
        assert agent.rows("SELECT claim_count FROM hnp_tasks WHERE task_id = ?", task_id) == [(1,)]
        assert grown(hnp, before) == {"orders": 1, "checkout_sessions": 1, "tokens": 1}
        assert hnp.proxy.count("POST", "/checkout_sessions") == 1
        assert sent(hnp, "/ap2/complete") == 1
        assert agent.delegation(grant_id)["state"] == "consumed"
    hnp.stack.record("hnp:B2-HNP:double_trigger", orders_created=1)


def test_hnp_revocation_while_the_checkout_is_signed_releases_without_order_or_token(hnp):
    """The buyer revokes while the Agent fetches the Checkout JWT, after UoW-1 reserved: the
    revocation releases the reservation, the closed mandates are never written, and neither the
    PSP nor the Merchant hears of a payment."""
    with hnp_agent(hnp, "revoke", "B2-HNP") as agent:
        _, price = catalog(agent.episode.feed, PHONE)
        before = effects(hnp)
        grant_id, task_id = scheduled(agent, price + SHIPPING_ROOM_MINOR, "hnp-revoke")
        receipts: list[httpx.Response] = []

        def revoke(_: bytes, __: dict[str, str]) -> None:
            receipts.append(agent.revoke(grant_id))

        def prepare(checkout_id: str) -> None:
            hnp.proxy.before_forward[f"/checkout_sessions/{checkout_id}/ap2/checkout-jwt"] = revoke

        on_checkout(hnp.proxy, prepare)
        assert agent.trigger(task_id).status_code == 202
        task = agent.finished(task_id)
        assert (task["state"], task["outcome_code"]) == ("canceled", "revoked_by_owner"), task
        assert task["message"] == HNP_MESSAGES["task.revoked_by_owner"]
        [answer] = receipts
        assert answer.status_code == 200, answer.text
        receipt = answer.json()
        assert (receipt["revoked_reason"], receipt["effect"]) == ("owner", "released")
        assert receipt["message"] == HNP_MESSAGES["revoke.released"]
        assert reservations(agent, grant_id) == [("released", "no_outbox", None)]
        assert mandates(agent, grant_id) == ["open_checkout", "open_payment"]
        assert agent.rows("SELECT count(*) FROM dispatch_outbox") == [(0,)]
        assert sent(hnp, "/ap2/checkout-jwt") == 1
        assert sent(hnp, "/ap2/complete") == 0
        assert grown(hnp, before) == {"orders": 0, "checkout_sessions": 1, "tokens": 0}
    document = verified(agent, hnp, grant_id)
    assert document["links"]["revocation"]["status"] == "ok", document["links"]
    assert document["complete"] is True, document["links"]
    hnp.stack.record("hnp:B2-HNP:revocation_race", effect=receipt["effect"])


def open_payment(
    provider: JWK,
    holder: dict[str, Any],
    origin: str,
    open_checkout: str,
    total_max: int,
    *,
    iat: int,
    exp: int,
    budget: bool = False,
) -> str:
    """An open payment mandate as `OpenMandateIssuer` signs it, linked to `open_checkout`. With
    `budget` it also carries `payment.budget`, which AP2 defines and `ap2-hnp/v1` does not
    (docs/ap2-profile.md): the product's issuer never signs one."""
    mandate = OpenPaymentMandate(
        constraints=[
            AmountRange(currency="COP", max=total_max),
            AllowedPayees(allowed=[Merchant(id=MERCHANT_ID, name=MERCHANT_NAME, website=origin)]),
            AllowedPaymentInstruments(allowed=[INSTRUMENT]),
            PaymentReference(
                conditional_transaction_id=common.compute_issuer_jwt_hash(
                    common.parse_token(open_checkout)
                )
            ),
            *([Budget(max=total_max / 100, currency="COP")] if budget else []),
        ],
        cnf={"jwk": holder},
        iat=iat,
        exp=exp,
    )
    return sd_jwt.create(mandate, provider).sd_jwt_issuance


def beyond_the_profile(agent: HnpAgent, lane: PurchaseLane, grant_id: str, total_max: int) -> None:
    """The grant's open payment mandate, re-issued by the same provider key with `payment.budget`,
    a constraint AP2 defines and `ap2-hnp/v1` does not (docs/ap2-profile.md). The product's
    issuer never writes one, so the test puts it in the ledger after consent, as a provider that
    changed would have issued it; the open checkout mandate and its link stay as they were."""
    assert lane.agent_key is not None
    [(open_checkout, consented_at, expires_at)] = agent.rows(
        "SELECT m.token, g.consented_at, g.expires_at FROM delegation_mandates m "
        "JOIN delegation_grants g USING (grant_id) "
        "WHERE m.grant_id = ? AND m.kind = 'open_checkout'",
        grant_id,
    )
    token = open_payment(
        JWK.from_pem(lane.agent_key.get_secret_value().encode()),
        JWK.from_pem(agent.agent_key.get_secret_value().encode()).export_public(as_dict=True),
        lane.origin,
        open_checkout,
        total_max,
        iat=int(datetime.fromisoformat(consented_at).timestamp()),
        exp=int(datetime.fromisoformat(expires_at).timestamp()),
        budget=True,
    )
    with closing(sqlite3.connect(agent.database)) as connection, connection:
        changed = connection.execute(
            "UPDATE delegation_mandates SET token = ?, token_digest = ?, link_hash = ? "
            "WHERE grant_id = ? AND kind = 'open_payment'",
            (
                token,
                token_digest(token),
                common.compute_issuer_jwt_hash(common.parse_token(token)),
                grant_id,
            ),
        ).rowcount
    assert changed == 1


@pytest.mark.parametrize("condition", ["B0-HNP", "B2-HNP"])
def test_hnp_open_mandate_beyond_the_profile_is_never_paid(hnp, condition):
    """An unknown constraint in the open payment mandate. B2-HNP's D2 finds the authorization
    profile unresolved and escalates before reserving; B0-HNP has no PDP, so the PSP is the one
    that refuses it with a signed `unresolved_constraint` receipt (§8.2). No token, no order."""
    with hnp_agent(hnp, f"beyond-{condition.lower()}", condition) as agent:
        _, price = catalog(agent.episode.feed, PHONE)
        total_max = price + SHIPPING_ROOM_MINOR
        before = effects(hnp)
        grant_id, task_id = scheduled(agent, total_max, "hnp-beyond")
        beyond_the_profile(agent, hnp, grant_id, total_max)
        assert agent.trigger(task_id).status_code == 202
        task = agent.finished(task_id)
        delegation = agent.delegation(grant_id)
        if condition == "B2-HNP":
            expected = ("escalated", "authorization_profile_unresolved")
            assert (task["state"], task["outcome_code"]) == expected, task
            assert delegation["escalation"]["message"] == escalation_message(expected[1])
            assert (delegation["state"], delegation["revoked_reason"]) == ("revoked", "escalated")
            assert reservations(agent, grant_id) == []
            assert mandates(agent, grant_id) == ["open_checkout", "open_payment"]
            assert sent(hnp, "/cancel") == 1  # an escalation cancels its own checkout (§8.2)
        else:
            expected = ("failed", "verifier_unresolved_constraint")
            assert (task["state"], task["outcome_code"]) == expected, task
            assert (delegation["state"], delegation["revoked_reason"]) == ("revoked", "failed")
            assert reservations(agent, grant_id) == [("released", "psp_error_receipt", None)]
            assert agent.rows("SELECT state, rejected_by FROM dispatch_outbox") == [
                ("rejected", "psp")
            ]
        assert sent(hnp, "/ap2/complete") == 0
        assert grown(hnp, before) == {"orders": 0, "checkout_sessions": 1, "tokens": 0}
    hnp.stack.record(f"hnp:{condition}:unknown_constraint", outcome=task["outcome_code"])


def test_hnp_crash_after_the_merchant_took_the_order_resumes_with_the_same_keys(hnp):
    """SIGKILL while `ap2/complete` is in flight: the Merchant still takes the order the Agent
    never saw. A new process re-claims once the dead run's lease lapsed (its authority clock runs
    ahead instead of waiting) and resumes the outbox with the same keys: the PSP and the Merchant
    replay, so there is one token and one order, and nothing is closed again."""
    with hnp_agent(hnp, "crash", "B2-HNP") as agent:
        _, price = catalog(agent.episode.feed, PHONE)
        before = effects(hnp)
        grant_id, task_id = scheduled(agent, price + SHIPPING_ROOM_MINOR, "hnp-crash")
        keys: list[str] = []
        crashed = threading.Event()

        def complete(_: bytes, headers: dict[str, str]) -> None:
            keys.append({name.lower(): value for name, value in headers.items()}["idempotency-key"])
            if len(keys) == 1:
                agent.crash()  # the request still goes on to the Merchant
                crashed.set()

        def prepare(checkout_id: str) -> None:
            hnp.proxy.before_forward[f"/checkout_sessions/{checkout_id}/ap2/complete"] = complete

        on_checkout(hnp.proxy, prepare)
        assert agent.trigger(task_id).status_code == 202
        assert crashed.wait(60)
        [(checkout_id,)] = agent.rows("SELECT checkout_id FROM dispatch_outbox")
        await_call(hnp.proxy, "POST", f"/checkout_sessions/{checkout_id}/ap2/complete")
        assert agent.rows("SELECT state FROM dispatch_outbox") == [("complete_sent",)]
        assert grown(hnp, before)["orders"] == 1
        closed = agent.rows(
            "SELECT kind, token_digest FROM delegation_mandates WHERE reservation_id IS NOT NULL "
            "ORDER BY kind"
        )

        agent.restart(clock_ahead_seconds=LEASE_SECONDS + 30)
        assert agent.trigger(task_id).status_code == 202
        task = agent.finished(task_id)
        assert task["state"] == "purchased", task
        assert len(keys) == 2 and keys[0] == keys[1], keys
        assert agent.rows("SELECT claim_count FROM hnp_tasks WHERE task_id = ?", task_id) == [(2,)]
        assert (
            agent.rows(
                "SELECT kind, token_digest FROM delegation_mandates "
                "WHERE reservation_id IS NOT NULL ORDER BY kind"
            )
            == closed
        )
        assert grown(hnp, before) == {"orders": 1, "checkout_sessions": 1, "tokens": 1}
        assert hnp.proxy.count("POST", "/checkout_sessions") == 1
        with hnp.receiver.delivering_to(SqliteAgentStore(agent.database)):
            hnp.rpc("dispatch_webhooks", limit=50)
    document = verified(agent, hnp, grant_id)
    assert (document["task_state"], document["complete"]) == ("purchased", True), document["links"]
    hnp.stack.record("hnp:B2-HNP:crash", orders_created=1, claims=2)


def test_hnp_merchant_timeout_leaves_the_task_uncertain_and_never_retries(hnp):
    """`ap2/complete` answers after the Agent's 5 s timeout. The Merchant did take the order; the
    Agent cannot know, so the task and the reservation stay `uncertain(dispatch_timeout)`, the
    grant stays active and no second authorization is tried (manual reconciliation, §4.4)."""
    with hnp_agent(hnp, "timeout", "B2-HNP") as agent:
        _, price = catalog(agent.episode.feed, PHONE)
        before = effects(hnp)
        grant_id, task_id = scheduled(agent, price + SHIPPING_ROOM_MINOR, "hnp-timeout")
        paths: list[str] = []

        def prepare(checkout_id: str) -> None:
            paths.append(f"/checkout_sessions/{checkout_id}/ap2/complete")
            hnp.proxy.faults[paths[0]] = "delay"

        hnp.proxy.delay_seconds = 6.5  # over AGENT's MERCHANT_TIMEOUT_SECONDS default of 5
        on_checkout(hnp.proxy, prepare)
        assert agent.trigger(task_id).status_code == 202
        task = agent.finished(task_id)
        assert (task["state"], task["outcome_code"]) == ("uncertain", "dispatch_timeout"), task
        assert task["message"] == HNP_MESSAGES["task.uncertain"]
        assert reservations(agent, grant_id) == [("uncertain", None, "dispatch_timeout")]
        assert agent.rows("SELECT state FROM dispatch_outbox") == [("uncertain",)]
        assert agent.delegation(grant_id)["state"] == "active"
        again = agent.trigger(task_id)
        assert (again.status_code, again.json()["code"]) == (409, "hnp_task_not_runnable")
        await_call(hnp.proxy, "POST", paths[0])
        assert hnp.proxy.count("POST", paths[0]) == 1
        assert agent.rows("SELECT count(*) FROM completion_results") == [(0,)]
        # The order the Agent never heard of: what the manual reconciliation has to find.
        assert grown(hnp, before) == {"orders": 1, "checkout_sessions": 1, "tokens": 1}
    hnp.stack.record("hnp:B2-HNP:timeout", outcome=task["outcome_code"])


class LiveChains:
    """The alterations of the golden table (tests/support/ap2_hnp_fixtures.py) rebuilt on the
    live stack: the lane's provider key as root, a new Agent Key, real Merchant checkouts and
    their Checkout JWTs, and real time, which the Merchant and the PSP verify with. The product's
    issuer and closer sign whatever they can; the rest is signed here as the fixtures do."""

    def __init__(self, lane: PurchaseLane, items: list[AcceptableItem], total_max: int) -> None:
        assert lane.agent_key is not None
        self.origin, self.items, self.total_max = lane.origin, items, total_max
        self.provider = JWK.from_pem(lane.agent_key.get_secret_value().encode())
        self.holder_pem, _ = ap2_key()
        self.closer = AgentKeyCloser(self.holder_pem, merchant_issuer=lane.origin)
        self.issuer = OpenMandateIssuer(
            lane.agent_key.get_secret_value(),
            agent_public_jwk=self.closer.public_jwk,
            merchant_issuer=lane.origin,
        )
        now = datetime.now(UTC)
        self.consented_at, self.expires_at = now - timedelta(seconds=60), now + timedelta(hours=1)
        self.timing = {
            "iat": int(self.consented_at.timestamp()),
            "exp": int(self.expires_at.timestamp()),
        }
        self.opens = self.issue()

    def issue(self, total_max: int | None = None) -> tuple[str, str]:
        pair = self.issuer.issue(
            self.items,
            total_max_minor=total_max or self.total_max,
            consented_at=self.consented_at,
            expires_at=self.expires_at,
        )
        return pair.checkout.token, pair.payment.token

    def beyond(self, *, merchant: Merchant | None = None, regions: bool = False) -> tuple[str, str]:
        """Open mandates the product's issuer never signs: another allowed merchant, or
        `checkout.allowed_regions`, which no profile defines."""
        ours = Merchant(id=MERCHANT_ID, name=MERCHANT_NAME, website=self.origin)
        timing: dict[str, Any] = {"cnf": {"jwk": self.closer.public_jwk}, **self.timing}
        acceptable = [Item(id=item.id, title=item.title) for item in self.items]
        constraints = [
            AllowedMerchants(allowed=[merchant or ours]),
            LineItems(
                items=[LineItemRequirements(id=LINE_ID, quantity=1, acceptable_items=acceptable)]
            ),
        ]
        open_checkout = (
            OpenCheckoutBeyondProfile(
                constraints=[*constraints, AllowedRegions(regions=["CO"])], **timing
            )
            if regions
            else OpenCheckoutMandate(constraints=constraints, **timing)
        )
        checkout = sd_jwt.create(open_checkout, self.provider).sd_jwt_issuance
        return checkout, self.payment(checkout)

    def budget(self) -> tuple[str, str]:
        """The grant's open checkout mandate with an open payment that carries `payment.budget`."""
        return self.opens[0], self.payment(self.opens[0], budget=True)

    def payment(self, open_checkout: str, *, budget: bool = False) -> str:
        return open_payment(
            self.provider,
            self.closer.public_jwk,
            self.origin,
            open_checkout,
            self.total_max,
            budget=budget,
            **self.timing,
        )

    def close(
        self,
        checkout: CheckoutSession,
        token: str,
        opens: tuple[str, str] | None = None,
        closer: AgentKeyCloser | None = None,
    ) -> tuple[str, str]:
        pair = (closer or self.closer).close(
            *(opens or self.opens), checkout_jwt=token, checkout=checkout, closed_at=self.now()
        )
        return pair.checkout.token, pair.payment.token

    def present(
        self, open_token: str, closed: CheckoutMandate | PaymentMandate, aud: str, token: str
    ) -> str:
        """`closed` presented with the Agent Key; the nonce is always the presented JWT's hash."""
        return present_closed_mandate(
            open_token,
            closed,
            holder_key=JWK.from_pem(self.holder_pem.encode()),
            aud=aud,
            nonce=checkout_hash(token),
            issued_at=closed.iat or 0,
        )

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)


def psp_rejects(
    provider: DelegatedPaymentClient,
    checkout: CheckoutSession,
    token: str,
    chains: tuple[str, str],
    key: str,
) -> str:
    """The AP2 code of the PSP's signed error receipt for `chains`; it issues no token."""
    with pytest.raises(DelegationError) as rejected:
        provider.hnp_payment_data(
            checkout=checkout,
            idempotency_key=key,
            checkout_jwt=token,
            payment_mandate=chains[1],
            checkout_mandate=chains[0],
        )
    error = rejected.value
    assert error.status_code == 422 and error.receipt is not None, error.code
    return provider.verify_error_receipt(error.receipt, mandate=chains[1])["error"]


def merchant_rejects(
    gateway: ACPClient, checkout_id: str, payment: SandboxPaymentData, chain: str, key: str
) -> str:
    """The AP2 code of the Merchant's signed error receipt for the checkout `chain`."""
    with pytest.raises(ACPClientError) as rejected:
        gateway.complete_checkout_ap2(checkout_id, payment, chain, idempotency_key=key)
    error = rejected.value
    assert error.status_code == 422 and error.receipt is not None, error.code
    return gateway.verify_checkout_error_receipt(error.receipt, mandate=chain)["error"]


def test_hnp_altered_chains_sent_straight_to_the_merchant_and_the_psp_are_rejected(hnp):
    """Each verifier, reached directly with no Agent in between, refuses the alterations its side
    of the golden table refuses, with a signed error receipt and its AP2 code; nothing it refuses
    leaves a token or an order. The valid chain then gets one token and one order."""
    episode = hnp.episode("altered")
    title, _ = catalog(episode.feed, PHONE)
    before = effects(hnp)
    with agent(hnp, episode) as buyer, payments(hnp) as provider:
        prepared = buyer.purchases.start(
            run_id=f"hnp-altered-{episode.run_id}",
            request="Compra delegada",
            intent=PurchaseIntent(),
            selected_variant_id=PHONE,
            details=buyer_details("altered"),
        )
        other = buyer.purchases.start(
            run_id=f"hnp-altered-other-{episode.run_id}",
            request="Compra delegada",
            intent=PurchaseIntent(),
            selected_variant_id=OTHER_PHONE,
            details=buyer_details("altered"),
        )
        checkout, foreign = prepared.checkout, other.checkout
        token = buyer.gateway.signed_checkout(checkout)
        second = buyer.gateway.signed_checkout(checkout)
        assert second != token
        live = LiveChains(hnp, [AcceptableItem(PHONE, title)], checkout.total_minor + 100_000_00)
        valid = live.close(checkout, token)
        stranger = AgentKeyCloser(ap2_key()[0], merchant_issuer=hnp.origin)
        spliced = live.close(checkout, token, opens=live.issue())
        signed_at = int(live.now().timestamp())
        declared = live.present(
            live.opens[0],
            CheckoutMandate(
                checkout_jwt=token,
                checkout_hash=checkout_hash(second),
                iat=signed_at,
                exp=signed_at + 300,
            ),
            hnp.origin,
            token,
        )
        elsewhere = live.present(
            live.opens[1],
            PaymentMandate(
                transaction_id=checkout_hash(token),
                payee=OTHER_MERCHANT,
                payment_amount=Amount(amount=checkout.total_minor, currency="COP"),
                payment_instrument=INSTRUMENT,
                iat=signed_at,
                exp=signed_at + 300,
            ),
            PAYMENT_AUDIENCE,
            token,
        )
        both = {
            "closed_with_other_key": (
                live.close(checkout, token, closer=stranger),
                "invalid_credential",
            ),
            "foreign_sd_hash": (
                (splice(live.opens[0], spliced[0]), splice(live.opens[1], spliced[1])),
                "invalid_credential",
            ),
            "checkout_hash_mismatch": ((declared, valid[1]), "invalid_mandate"),
        }
        to_psp = {
            **both,
            "total_out_of_range": (
                live.close(checkout, token, opens=live.issue(checkout.total_minor - 1)),
                "invalid_mandate",
            ),
            "payee_mismatch": ((valid[0], elsewhere), "invalid_mandate"),
            "unknown_constraint_payment": (
                live.close(checkout, token, opens=live.budget()),
                "unresolved_constraint",
            ),
        }
        psp = {
            name: psp_rejects(provider, checkout, token, chains, f"altered-{name}")
            for name, (chains, _) in to_psp.items()
        }
        assert psp == {name: code for name, (_, code) in to_psp.items()}
        assert grown(hnp, before)["tokens"] == 0

        payment = provider.hnp_payment_data(
            checkout=checkout,
            idempotency_key="altered-valid",
            checkout_jwt=token,
            payment_mandate=valid[1],
            checkout_mandate=valid[0],
        )
        foreign_token = buyer.gateway.signed_checkout(foreign)
        to_merchant = {
            **{name: (checkout.id, chains[0], code) for name, (chains, code) in both.items()},
            "variant_not_acceptable": (
                foreign.id,
                live.close(foreign, foreign_token)[0],
                "invalid_mandate",
            ),
            "merchant_not_allowed": (
                checkout.id,
                live.close(checkout, token, opens=live.beyond(merchant=OTHER_MERCHANT))[0],
                "invalid_mandate",
            ),
            "unknown_constraint_checkout": (
                checkout.id,
                live.close(checkout, token, opens=live.beyond(regions=True))[0],
                "unresolved_constraint",
            ),
        }
        merchant = {
            name: merchant_rejects(buyer.gateway, checkout_id, payment, chain, f"altered-{name}")
            for name, (checkout_id, chain, _) in to_merchant.items()
        }
        assert merchant == {name: code for name, (_, _, code) in to_merchant.items()}
        assert grown(hnp, before) == {"orders": 0, "checkout_sessions": 2, "tokens": 1}

        completed, receipt = buyer.gateway.complete_checkout_ap2(
            checkout.id, payment, valid[0], idempotency_key="altered-valid"
        )
        assert completed.order.checkout_session_id == checkout.id and receipt
        assert provider.payment_receipt(
            token=payment.instrument.credential.token.get_secret_value(), mandate=valid[1]
        )
    assert grown(hnp, before) == {"orders": 1, "checkout_sessions": 2, "tokens": 1}
    hnp.stack.record("hnp:altered_chains", psp=sorted(psp), merchant=sorted(merchant))
