"""Recorded browser journeys of a person over the live stack: governed purchases and delegations.

Chromium signs up in `auth/`, writes in the chat, clicks the cards and creates and revokes
delegations in the UI served by Vite; the Agent API runs in this process with the scripted router
(never a model) and talks to the lane's real Merchant, payment provider and PostgreSQL. Only the
scheduler's trigger (the service token) and the Merchant's price change come from the harness.

- B2 (`b0` lane with the demo's `b2` condition, policy `b2-hp-v2`): the purchase approved on its
  card shows D1 and D2 allowing it in the activity; a total budget below the price stops at D1
  (POL-11) with the explanation in the chat, no approval card and no checkout at the Merchant.
- B2-HNP (`hnp` lane): «Delegar» in the header's purchase mode, a delegation proposed from the
  options seen in the chat with the total the person writes, consented on its card and bought once
  by the trigger, with its governance (the HNP trace) on the card; the price raised over the limit
  after consenting (HN03) escalates and «Comprar desde el chat» prefills the composer; a revocation
  from the card leaves the trigger with 409 and no order; and the first journey again on a
  390 x 844 phone, which must never scroll sideways.

Each journey leaves its video, a screenshot per step and, if it fails, the Playwright trace in
`.local/browser-journeys/<journey>/` (tests/integration/README.md). What a journey expects and
the product does not do yet is a `ProductGap`: those journeys are xfail(strict=True) on that type
only, after the whole journey ran.
"""

import json
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import pytest
from playwright.sync_api import Locator, Page, expect, sync_playwright

from commerce_agent.acp import load_product_feed
from commerce_agent.delegation.contract import HNP_MESSAGES, escalation_message
from commerce_agent.domain import PurchaseIntent, RankedCandidate
from commerce_agent.llm import CandidateDecision, ConversationMessage
from commerce_agent.trace.assembler import PurchaseTraces
from commerce_agent.trace.models import (
    AuthorizationDecisionStep,
    DispatchDecisionStep,
    Money,
    PolicyDecisionStep,
)
from commerce_evaluation.demo_stack import MODES, mode_environment
from commerce_evaluation.governance_pilot import D1RunKeys
from tests.integration.live_browser import (
    BuyerUi,
    Journey,
    ProductGap,
    agent_errors,
    browser_requirements,
    journey,
    sign_up,
)
from tests.integration.live_purchase import (
    PHONE,
    PHONE_INTENT,
    PHONE_REQUEST,
    PurchaseLane,
    StaticConversationRouter,
    ap2_key,
    hnp_condition,
)

# What a person asks before delegating: the chat searches and shows the options, nothing more.
SEARCH_REQUEST = "Busca teléfonos Sony nuevos de 256 GB; quiero que los compres tú más tarde."
SEARCH_ANSWER = (
    "Estas son las opciones que cumplen. Si quieres que compre sin ti, autorízalo en «Delegar»."
)
# The POL-11 turn: a total budget below every phone of the lane (test_live_b2_pol11_budget.py).
BUDGET_MINOR = 100_000_00
BUDGET_REQUEST = (
    "Quiero un teléfono Sony nuevo de 256 GB y no gastar más de 100.000 pesos en total, con envío."
)
BUDGET_INTENT = PHONE_INTENT.model_copy(
    update={"hard": PHONE_INTENT.hard.model_copy(update={"max_total_amount_minor": BUDGET_MINOR})}
)
# Room over the catalog price for shipping: the delegation's maximum includes it (§4.2).
SHIPPING_ROOM_MINOR = 500_000_00
COMPOSER = "Describe lo que quieres comprar..."
# The steps of a delegated purchase's trace (hnp-governed.md §10, trace-panel.md).
DELEGATED_STEPS = [
    "delegation_consented",
    "task_activated",
    "policy_decision",
    "checkout_created",
    "freshness_check",
    "authorization_decision",
    "reservation",
    "mandates_closed",
    "dispatch_decision",
    "payment_authorized",
    "order_confirmed",
]
# What the card's governance shows of a delegated purchase that completed (trace-panel.md,
# «Compra delegada»); «Pago autorizado» is checked in the trace, where it is still missing.
PURCHASED_CONTROLS = [
    "Delegación autorizada",
    "Compra delegada en marcha",
    "Política de compra: permitida",
    "Uso único reservado",
    "Autorización cerrada por el agente",
    "Despacho autorizado",
    "Orden confirmada",
    "Uso único consumido",
]


def cop(minor: int) -> str:
    """An amount as the UI writes it (K16), built here and not by the product: «$», a no-break
    space, dots for thousands, always two decimals and the code."""
    return f"$\N{NO-BREAK SPACE}{minor // 100:,}".replace(",", ".") + f",{minor % 100:02d} COP"


def pesos(minor: int) -> str:
    """What a person types in «Total máximo con envío (pesos)»: whole pesos with dots."""
    assert minor % 100 == 0
    return f"{minor // 100:,}".replace(",", ".")


class SearchOnlyRouter(StaticConversationRouter):
    """The purchase turn up to the catalog search: it shows the options and stops (`clarify`),
    as a model that leaves the purchase to «Delegar». No checkout is created."""

    def choose_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        candidates: tuple[RankedCandidate, ...],
    ) -> CandidateDecision:
        super().choose_candidate(messages, intent, candidates)
        return CandidateDecision(action="clarify", assistant_message=SEARCH_ANSWER)


def b2_condition(lane: PurchaseLane) -> dict[str, str]:
    """The demo's `b2` mode: enforce, the governed HP flow and the b2-hp-v2 policy."""
    condition = mode_environment(MODES["b2"], lane.origin, D1RunKeys.generate())
    assert condition["AGENT_D1_POLICY_SET_ID"] == "b2-hp-v2"
    return condition


def b2_hnp_condition(lane: PurchaseLane) -> dict[str, str]:
    agent_key, _ = ap2_key()
    return hnp_condition(
        lane,
        "B2-HNP",
        agent_key=agent_key,
        service=secrets.token_urlsafe(32),
        operator=secrets.token_urlsafe(32),
    )


def require_browser() -> None:
    if problems := browser_requirements():
        pytest.fail("The browser journeys also require " + "; ".join(problems), pytrace=False)


@contextmanager
def recorded(ui: BuyerUi, name: str, *, phone: bool = False) -> Iterator[tuple[Journey, Page]]:
    """Chromium on the UI, recording the journey `name`, already signed up as a new buyer."""
    with (
        sync_playwright() as playwright,
        journey(playwright, name, ui.root, phone=phone) as session,
    ):
        page = session.page
        page.goto(ui.origin)
        sign_up(page, f"buyer.{ui.episode.actor_id}@example.test", "Comprador de prueba")
        session.step("registro")
        yield session, page


def ask(page: Page, request: str) -> None:
    page.get_by_placeholder(COMPOSER).fill(request)
    page.get_by_role("button", name="Enviar mensaje").click()


def effects(lane: PurchaseLane) -> dict[str, int]:
    merchant = lane.rpc("effects")
    return {
        "orders": merchant["orders"],
        "checkout_sessions": merchant["checkout_sessions"],
        "tokens": lane.payment_tokens(),
    }


# B2: governed Human Present -----------------------------------------------------------------


def test_browser_b2_governed_purchase_shows_d1_and_d2_allowing_it(b0):
    """Recorrido 1: sign-up, the phone in the chat, the approval card with its total, «Aprobar y
    comprar», the confirmed order, and the activity with D1 and D2 allowing the purchase."""
    require_browser()
    episode = b0.episode("browser-b2")
    router = StaticConversationRouter(PHONE_REQUEST, PHONE_INTENT)
    before = effects(b0)
    with (
        BuyerUi(b0, episode, router, b2_condition(b0)) as ui,
        recorded(ui, "b2-compra") as (session, page),
    ):
        errors = agent_errors(page, ui.agent.origin)
        ask(page, PHONE_REQUEST)
        card = page.get_by_test_id("purchase-approval-card")
        approve = card.get_by_role("button", name="Aprobar y comprar")
        expect(approve).to_be_visible(timeout=60_000)
        [(run_id, terms)] = ui.rows("SELECT run_id, terms FROM approval_terms")
        total = json.loads(terms)["total_minor"]
        expect(card).to_contain_text(cop(total).removesuffix(" COP"))
        session.step("aprobacion", card)
        approve.click()

        expect(page.get_by_text("Compra completada").first).to_be_visible(timeout=30_000)
        activity = page.get_by_test_id("purchase-activity")
        expect(activity).to_have_attribute("data-status", "completed", timeout=15_000)
        activity.get_by_role("button", name=re.compile("Compra completada")).click()
        expect(activity.get_by_text("Activa: detiene la compra si no cumple")).to_be_visible()
        expect(activity.get_by_text("Política de compra: permitida")).to_be_visible()
        expect(activity.get_by_text("Autorización verificada", exact=True)).to_be_visible()
        expect(activity.get_by_text("Autorización firmada", exact=True)).to_be_visible()
        expect(activity.get_by_text("Orden confirmada")).to_be_visible()
        session.step("traza-d1", activity.get_by_text("Política de compra: permitida"))
        session.step("traza-d2-y-orden", activity.get_by_text("Orden confirmada"))
        assert errors == []

    # What the activity showed is the Agent's trace: D1 and D2 of b2-hp-v2, both allowing.
    trace = PurchaseTraces(ui.settings).trace(run_id)
    [d1] = [step.decision for step in trace.steps if isinstance(step, PolicyDecisionStep)]
    [d2] = [step.decision for step in trace.steps if isinstance(step, AuthorizationDecisionStep)]
    assert (d1.policy.set_id, d1.mode, d1.outcome, d1.effect) == (
        "b2-hp-v2",
        "enforce",
        "allow",
        "continued",
    )
    assert (d2.outcome, d2.effect) == ("allow", "continued")
    assert {rule.result for rule in d2.rules} == {"pass"}
    receipts = ui.rows(
        "SELECT checkout_receipt_jwt, payment_receipt_jwt FROM completion_results WHERE run_id = ?",
        run_id,
    )
    assert len(receipts) == 1 and all(receipts[0])
    assert b0.proxy.count("POST", "/checkout_sessions") == 1
    after = effects(b0)
    assert (after["orders"], after["tokens"]) == (before["orders"] + 1, before["tokens"] + 1)
    b0.stack.record("browser:b2-compra", d1=d1.outcome, d2=d2.outcome, creation_posts=1)


def test_browser_b2_budget_below_the_price_explains_pol11_without_a_checkout(b0):
    """Recorrido 2: a total budget below the price. The chat explains the budget refusal (POL-11),
    no approval card appears and the Merchant receives no creation POST."""
    require_browser()
    episode = b0.episode("browser-pol11")
    router = StaticConversationRouter(BUDGET_REQUEST, BUDGET_INTENT)
    before = b0.rpc("effects")
    with (
        BuyerUi(b0, episode, router, b2_condition(b0)) as ui,
        recorded(ui, "b2-presupuesto") as (session, page),
    ):
        errors = agent_errors(page, ui.agent.origin)
        ask(page, BUDGET_REQUEST)
        blocked = page.get_by_test_id("policy-blocked-card")
        expect(blocked).to_be_visible(timeout=60_000)
        [(run_id,)] = ui.rows("SELECT run_id FROM purchases")
        trace = PurchaseTraces(ui.settings).trace(run_id)
        [d1] = [step.decision for step in trace.steps if isinstance(step, PolicyDecisionStep)]
        assert (d1.outcome, d1.effect) == ("deny", "blocked")
        assert d1.reason_codes == ("BUDGET_NOT_SATISFIABLE",)
        [pol11] = [rule for rule in d1.rules if rule.id == "POL-11"]
        assert pol11.result == "fail" and isinstance(pol11.observed, Money)
        # The chat says why, with both amounts: the catalog price and the total budget.
        expect(blocked).to_contain_text("La política de compra detuvo esta compra")
        expect(blocked).to_contain_text("Precio dentro de tu presupuesto")
        expect(blocked).to_contain_text("No cumple")
        expect(blocked).to_contain_text(f"Precio en el catálogo{cop(pol11.observed.minor)}")
        expect(blocked).to_contain_text(f"Tu presupuesto total{cop(BUDGET_MINOR)}")
        expect(blocked).to_contain_text("No se creó ningún checkout ni se hizo ningún cobro.")
        session.step("rechazo-presupuesto", blocked)
        expect(page.get_by_test_id("purchase-approval-card")).to_have_count(0)
        expect(page.get_by_role("button", name="Aprobar y comprar")).to_have_count(0)
        assert errors == []

    assert b0.proxy.count("POST", "/checkout_sessions") == 0
    assert b0.rpc("effects") == before
    b0.stack.record("browser:b2-presupuesto", pol11=pol11.result, creation_posts=0)


# B2-HNP: delegations --------------------------------------------------------------------------


def delegation_ui(hnp: PurchaseLane, name: str) -> BuyerUi:
    episode = hnp.episode(name)
    return BuyerUi(
        hnp, episode, SearchOnlyRouter(SEARCH_REQUEST, PHONE_INTENT), b2_hnp_condition(hnp)
    )


def catalog(ui: BuyerUi) -> tuple[str, int]:
    """The feed title and price of the phone the person delegates, as the form shows them."""
    candidate = next(
        item for item in load_product_feed(ui.episode.feed).candidates if item.variant_id == PHONE
    )
    return candidate.variant_title, candidate.observed_unit_amount_minor


class Fit:
    """On a phone, after each step: the page never scrolls sideways and nothing sticks out of
    the «Delegaciones» dialog. A miss is a `ProductGap`, so the journey goes on."""

    PAGE = "document.documentElement.scrollWidth <= window.innerWidth"
    # Once nothing animates, the dialog's sideways overflow and the elements that stick out.
    SETTLED = "document.getAnimations().every((animation) => animation.playState !== 'running')"
    DIALOG = """(() => {
      const dialog = document.querySelector("[data-testid='delegations']")
      if (!dialog) return { scroll: 0, outside: [] }
      const box = dialog.getBoundingClientRect()
      const outside = [...dialog.querySelectorAll("*")].filter((child) => {
        const inner = child.getBoundingClientRect()
        return inner.width > 0 && (inner.left < box.left - 0.5 || inner.right > box.right + 0.5)
      }).slice(0, 3).map((child) => {
        const inner = child.getBoundingClientRect()
        const name = child.getAttribute("data-testid") || child.getAttribute("aria-label")
          || (child.textContent || "").trim().slice(0, 40)
        return `<${child.tagName.toLowerCase()}> «${name}» ${Math.round(inner.left)}..`
          + `${Math.round(inner.right)} (diálogo ${Math.round(box.left)}..${Math.round(box.right)})`
      })
      return { scroll: dialog.scrollWidth - dialog.clientWidth, outside }
    })()"""

    def __init__(self, page: Page, enabled: bool) -> None:
        self.page = page
        self.enabled = enabled
        self.misses: list[str] = []

    def check(self, step: str) -> None:
        if not self.enabled:
            return
        self.page.wait_for_function(self.SETTLED, timeout=5_000)
        if not self.page.evaluate(self.PAGE):
            self.misses.append(f"{step}: la página se desplaza de lado")
        dialog = self.page.evaluate(self.DIALOG)
        if dialog["scroll"] > 0 or dialog["outside"]:
            self.misses.append(
                f"{step}: algo se sale del diálogo por los lados (desborde {dialog['scroll']} px; "
                + ("; ".join(dialog["outside"]) or "ningún elemento afuera")
                + ")"
            )


def delegate(page: Page) -> Locator:
    """«Delegar» in the header's purchase mode («Comprar conmigo» is the chat): it opens the
    «Delegaciones» dialog, modal, so the selector behind it is hidden from the accessibility
    tree while it is open."""
    page.get_by_role("group", name="Modo de compra").get_by_text("Delegar", exact=True).click()
    expect(page.get_by_role("radio", name="Delegar", include_hidden=True)).to_be_checked()
    dialog = page.get_by_test_id("delegations")
    expect(dialog.get_by_role("heading", name="Mis delegaciones")).to_be_visible()
    return dialog


def consent(ui: BuyerUi, session: Journey, page: Page, fit: Fit) -> tuple[str, int, int, Locator]:
    """From the chat to an active delegation, as a person: search, «Delegar», the form with the
    phone, the maximum they write and «6 horas», the consent card and «Autorizar». Returns the
    title, the catalog price, the consented maximum and the delegation's status card."""
    title, price = catalog(ui)
    total_max = price + SHIPPING_ROOM_MINOR
    ask(page, SEARCH_REQUEST)
    expect(page.get_by_text(SEARCH_ANSWER)).to_be_visible(timeout=60_000)
    # The finished turn folds its steps; the person opens them to see the options again.
    steps = page.get_by_role("button", name=re.compile("^Pensó durante"))
    expect(steps).to_have_attribute("aria-expanded", "false")
    steps.click()
    shown = page.get_by_role("heading", name=re.compile(r"\d+ coincidencias"))
    expect(shown).to_be_visible()
    fit.check("búsqueda")
    session.step("busqueda", shown)

    dialog = delegate(page)
    expect(dialog.get_by_text("Todavía no tienes delegaciones.")).to_be_visible()
    dialog.get_by_role("button", name="Nueva delegación").click()
    form = dialog.get_by_test_id("delegation-form")
    option = form.get_by_role("checkbox", name=re.compile(re.escape(title)))
    option.check()
    # The catalog price of the option, with «,00» (K16).
    expect(form.get_by_text(f"Catálogo: {cop(price)}")).to_be_visible()
    # The person chooses the maximum (hnp-governed.md §3.2): the form never proposes one.
    total = form.get_by_label("Total máximo con envío (pesos)")
    expect(total).to_have_value("")
    total.fill(pesos(total_max))
    form.get_by_label("Vigencia").select_option(label="6 horas")
    fit.check("formulario")
    session.step("formulario", total)
    submit = form.get_by_role("button", name="Preparar la autorización")
    session.step("preparar", submit)
    submit.click()

    card = dialog.get_by_test_id("delegation-consent-card")
    expect(card).to_be_visible(timeout=15_000)
    expect(card.get_by_text("El total máximo incluye envío")).to_be_visible()
    expect(card.get_by_text(cop(total_max).removesuffix(" COP"), exact=True)).to_be_visible()
    expect(card).to_contain_text(title)
    expect(card).to_contain_text("Bogota, DC")
    expect(card.get_by_text("Un solo uso")).to_be_visible()
    expect(card.get_by_text(re.compile(r"^Vence el "))).to_be_visible()
    fit.check("consentimiento")
    session.step("consentimiento", card)
    card.get_by_role("button", name="Autorizar").click()

    expect(card).to_have_count(0, timeout=15_000)
    status = dialog.get_by_test_id("delegation-status-card")
    expect(status).to_have_count(1)
    expect(status).to_have_attribute("data-status", "scheduled")
    expect(status.get_by_text("Delegación activa")).to_be_visible()
    expect(status.get_by_text(HNP_MESSAGES["task.scheduled"])).to_be_visible()
    expect(status.get_by_text(cop(total_max))).to_be_visible()
    expect(status.get_by_role("button", name="Revocar", exact=True)).to_be_visible()
    fit.check("activa")
    session.step("activa", status)
    # «6 horas» is what the grant holds, counted from the proposal a moment before the consent.
    [(valid_from, expires_at)] = ui.rows("SELECT valid_from, expires_at FROM delegation_grants")
    validity = datetime.fromisoformat(expires_at) - datetime.fromisoformat(valid_from)
    assert abs(validity.total_seconds() - 6 * 3_600) < 60, validity
    return title, price, total_max, status


def task_id(ui: BuyerUi) -> str:
    """The task the scheduler would trigger: the episode's only one."""
    [(task,)] = ui.rows("SELECT task_id FROM hnp_tasks")
    return task


def reopen(page: Page) -> Locator:
    """The person comes back later: a new page load and «Delegar» again."""
    page.reload()
    status = delegate(page).get_by_test_id("delegation-status-card")
    expect(status).to_have_count(1, timeout=15_000)
    return status


def buy_by_delegation(hnp: PurchaseLane, name: str, *, phone: bool) -> None:
    """Recorridos 3 y 6: consent in the UI, the scheduler's trigger, and the purchase as the UI
    shows it, with the order; the receipts and the HNP trace are checked in the Agent."""
    require_browser()
    gaps: list[str] = []
    before = effects(hnp)
    with delegation_ui(hnp, name) as ui, recorded(ui, name, phone=phone) as (session, page):
        errors = agent_errors(page, ui.agent.origin)
        fit = Fit(page, phone)
        title, _, total_max, _ = consent(ui, session, page, fit)

        triggered = ui.trigger(task_id(ui))
        assert triggered.status_code == 202, triggered.text
        task = ui.finished(task_id(ui))
        assert (task["state"], task["outcome_code"]) == ("purchased", None), task
        [(amount,)] = ui.rows("SELECT amount_minor FROM grant_reservations")
        message = HNP_MESSAGES["task.purchased"].format(
            title=title, total=cop(amount), order_id=task["order_id"]
        )
        assert task["message"] == message

        status = reopen(page)
        expect(status).to_have_attribute("data-status", "purchased")
        expect(status.get_by_role("heading", name="Compra completada")).to_be_visible()
        expect(status.get_by_text(message, exact=True)).to_be_visible()
        expect(status.get_by_role("button", name="Revocar", exact=True)).to_have_count(0)
        fit.check("compra")
        session.step("compra-hecha", status)
        # Its governance on the card: the HNP trace that GET /purchases/{run_id}/trace serves.
        activity = governance(status, "completed")
        activity.get_by_role("button", name=re.compile("^Compra completada")).click()
        for control in PURCHASED_CONTROLS:
            expect(activity.get_by_text(control, exact=True)).to_be_visible()
        fit.check("gobernanza")
        session.step("gobernanza", activity.get_by_text("Política de compra: permitida"))
        session.step("gobernanza-orden", activity.get_by_text("Orden confirmada", exact=True))
        assert errors == []
        gaps += fit.misses

    run_id = task["run_id"]
    receipts = ui.rows(
        "SELECT checkout_receipt_jwt, payment_receipt_jwt FROM completion_results WHERE run_id = ?",
        run_id,
    )
    assert len(receipts) == 1 and all(receipts[0])
    trace = PurchaseTraces(ui.settings).trace(run_id)
    assert trace.purchase_mode == "HNP" and trace.status == "completed"
    # Every step of a delegated purchase; the last ones share UoW-3, so only their set is fixed.
    kinds = {step.kind for step in trace.steps}
    assert kinds >= set(DELEGATED_STEPS) - {"payment_authorized"}, kinds
    if "payment_authorized" not in kinds:
        gaps.append(
            "La traza HNP de la compra completada no trae «Pago autorizado» (`payment_authorized`) "
            "aunque `completion_results` guarda el recibo de pago: quedó «Pendiente de 09b» "
            "(src/commerce_agent/trace/assembler.py:824, docs/design/trace-panel.md:350, "
            "hnp-governed.md §10)"
        )
    decisions = [
        (step.decision.outcome, step.decision.effect)
        for step in trace.steps
        if isinstance(step, PolicyDecisionStep | AuthorizationDecisionStep | DispatchDecisionStep)
    ]
    assert decisions == [("allow", "continued")] * 3
    assert grown(hnp, before) == {"orders": 1, "checkout_sessions": 1, "tokens": 1}
    assert total_max >= amount
    hnp.stack.record(f"browser:{name}", order_id=task["order_id"], gaps=gaps)
    if gaps:
        raise ProductGap("; ".join(gaps))


def grown(lane: PurchaseLane, before: dict[str, int]) -> dict[str, int]:
    return {name: value - before[name] for name, value in effects(lane).items()}


def governance(status: Locator, purchase: str) -> Locator:
    """«Gobernanza de la compra» on a delegation's card: the activity row of its purchase, read
    from the trace and folded. `purchase` is the trace's status, as the row exposes it."""
    group = status.get_by_role("group", name="Gobernanza de la compra")
    expect(group).to_be_visible(timeout=15_000)
    activity = group.get_by_test_id("purchase-activity")
    expect(activity).to_have_attribute("data-source", "trace")
    expect(activity).to_have_attribute("data-status", purchase)
    return activity


PURCHASE_GAPS = "La traza HNP de la compra completada no trae «Pago autorizado»"


@pytest.mark.xfail(strict=True, raises=ProductGap, reason=PURCHASE_GAPS)
def test_browser_hnp_delegation_buys_once_and_shows_the_order(hnp):
    """Recorrido 3, en escritorio."""
    buy_by_delegation(hnp, "hnp-compra", phone=False)


@pytest.mark.xfail(strict=True, raises=ProductGap, reason=PURCHASE_GAPS)
def test_browser_hnp_delegation_on_a_phone_fits_the_screen(hnp):
    """Recorrido 6: el recorrido 3 en un teléfono de 390 x 844, sin cortes ni scroll lateral."""
    buy_by_delegation(hnp, "hnp-compra-telefono", phone=True)


def test_browser_hnp_price_over_the_limit_asks_for_a_decision_and_prefills_the_chat(hnp):
    """Recorrido 4: after the consent the Merchant raises the price over the maximum and exports
    its feed again, and the Agent restarts on it (HN03). The trigger escalates at D1 (POL-11); the
    UI shows «Necesito tu decisión» and «Comprar desde el chat» prefills the composer with the
    exact request, amount included. It is not sent."""
    require_browser()
    before = effects(hnp)
    with (
        delegation_ui(hnp, "hnp-escala") as ui,
        recorded(ui, "hnp-escala") as (session, page),
    ):
        errors = agent_errors(page, ui.agent.origin)
        title, _, total_max, _ = consent(ui, session, page, Fit(page, False))

        raised = total_max + 100_000_00
        hnp.rpc(
            "reprice_offer",
            run_id=ui.episode.run_id,
            variant=PHONE,
            price_minor=raised,
            feed_dir=str(ui.episode.feed),
        )
        ui.restart_agent()
        assert catalog(ui)[1] == raised
        triggered = ui.trigger(task_id(ui))
        assert triggered.status_code == 202, triggered.text
        task = ui.finished(task_id(ui))
        assert (task["state"], task["outcome_code"]) == ("escalated", "budget_not_satisfiable")

        status = reopen(page)
        expect(status).to_have_attribute("data-status", "escalated")
        expect(status.get_by_role("heading", name="Necesito tu decisión")).to_be_visible()
        expect(status.get_by_text(escalation_message("budget_not_satisfiable"))).to_be_visible()
        session.step("necesito-tu-decision", status)
        # Its governance: the trace of the purchase D1 stopped, with the escalation last.
        trace = PurchaseTraces(ui.settings).trace(task["run_id"])
        assert trace.purchase_mode == "HNP" and trace.steps[-1].kind == "escalated"
        activity = governance(status, trace.status)
        activity.get_by_role("button", name=re.compile(".")).first.click()
        expect(activity.get_by_text("Delegación autorizada", exact=True)).to_be_visible()
        expect(activity.get_by_text("Necesito tu decisión", exact=True)).to_be_visible()
        session.step("gobernanza-escalada", activity)
        status.get_by_role("button", name="Comprar desde el chat").click()

        expect(page.get_by_test_id("delegations")).to_be_hidden()
        composer = page.get_by_placeholder(COMPOSER)
        prefill = f"Quiero comprar {title} por hasta {cop(total_max)}"
        expect(composer).to_have_value(prefill)
        expect(composer).to_be_focused()
        session.step("compositor-prellenado", composer)
        assert errors == []
        # Not sent: the chat holds no message and the Agent no new purchase.
        assert ui.rows("SELECT count(*) FROM purchases WHERE purchase_mode = 'HP'") == [(0,)]

    assert (
        HNP_MESSAGES["escalation.prefill_request"].format(title=title, total=cop(total_max))
        == prefill
    )
    assert hnp.proxy.count("POST", "/checkout_sessions") == 0
    assert grown(hnp, before) == {"orders": 0, "checkout_sessions": 0, "tokens": 0}
    hnp.stack.record("browser:hnp-escala", outcome=task["outcome_code"])


def test_browser_hnp_revoke_from_the_card_leaves_no_order(hnp):
    """Recorrido 5: consent, «Revocar» on the card and its receipt; the scheduler's trigger then
    answers 409 and nothing reaches the Merchant or the provider; the UI shows it revoked."""
    require_browser()
    before = effects(hnp)
    with (
        delegation_ui(hnp, "hnp-revoca") as ui,
        recorded(ui, "hnp-revoca") as (session, page),
    ):
        errors = agent_errors(page, ui.agent.origin)
        _, _, _, status = consent(ui, session, page, Fit(page, False))

        status.get_by_role("button", name="Revocar", exact=True).click()
        receipt = status.get_by_test_id("delegation-revoke-receipt")
        expect(receipt).to_have_attribute("data-effect", "no_effect")
        expect(receipt).to_contain_text(HNP_MESSAGES["revoke.no_effect"])
        expect(status).to_have_attribute("data-status", "revoked")
        expect(status.get_by_role("button", name="Revocar", exact=True)).to_have_count(0)
        session.step("recibo-revocacion", receipt)

        refused = ui.trigger(task_id(ui))
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "hnp_task_not_runnable"

        status = reopen(page)
        expect(status).to_have_attribute("data-status", "revoked")
        expect(status.get_by_text("Delegación revocada")).to_be_visible()
        expect(status.get_by_text(HNP_MESSAGES["task.revoked_by_owner"])).to_be_visible()
        session.step("revocada", status)
        assert errors == []

    assert ui.rows("SELECT state, revoked_reason FROM delegation_grants") == [("revoked", "owner")]
    assert ui.rows("SELECT state, outcome_code FROM hnp_tasks") == [
        ("canceled", "revoked_by_owner")
    ]
    assert hnp.proxy.count("POST", "/checkout_sessions") == 0
    assert grown(hnp, before) == {"orders": 0, "checkout_sessions": 0, "tokens": 0}
    hnp.stack.record("browser:hnp-revoca", trigger=refused.status_code)
