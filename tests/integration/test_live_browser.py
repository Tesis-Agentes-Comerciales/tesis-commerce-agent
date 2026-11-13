"""A buyer completes the B0 purchase from the browser, against the live stack.

Sign-up through the auth service, the request in the chat, explicit approval on the approval
card and the confirmed order, with both AP2 receipts kept by the Agent. The chat reads the
purchase trace from the live Agent: the activity row ends completed, the card turns into a
receipt and, with the policy off in this lane, the control shows as disabled. The model boundary is
scripted (live_purchase.StaticConversationRouter) unless RUN_LIVE_LLM=1; the rest is the real
UI and services.
"""

import os
import re
import sqlite3
from contextlib import closing

import pytest
from playwright.sync_api import expect, sync_playwright

from commerce_evaluation.stack.purchase_agent import (
    PHONE_INTENT,
    PHONE_REQUEST,
    StaticConversationRouter,
)
from tests.integration.live_browser import (
    BuyerUi,
    agent_errors,
    browser_requirements,
    journey,
    sign_up,
)


def test_b0_purchase_from_the_browser_signs_up_approves_and_keeps_both_receipts(b0):
    if problems := browser_requirements():
        pytest.fail("The browser test also requires " + "; ".join(problems), pytrace=False)
    episode = b0.episode("browser")
    # RUN_LIVE_LLM=1 puts the configured OpenAI model behind the boundary (manual runs only).
    router, model = StaticConversationRouter(PHONE_REQUEST, PHONE_INTENT), {}
    if os.getenv("RUN_LIVE_LLM") == "1":
        router = None
        model = {name: os.environ[name] for name in ("OPENAI_API_KEY", "OPENAI_MODEL")}
    with (
        BuyerUi(b0, episode, router, model) as ui,
        sync_playwright() as playwright,
        journey(playwright, "b0-compra", ui.root) as recorded,
    ):
        page = recorded.page
        errors = agent_errors(page, ui.agent.origin)
        page.goto(ui.origin)
        sign_up(page, f"buyer.{episode.actor_id}@example.test", "Comprador B0")
        page.get_by_placeholder("Describe lo que quieres comprar...").fill(PHONE_REQUEST)
        page.get_by_role("button", name="Enviar mensaje").click()
        approve = page.get_by_role("button", name="Aprobar y comprar")
        expect(approve).to_be_visible(timeout=60_000)
        assert errors == []
        recorded.step("aprobacion", approve)
        approve.click()
        expect(page.get_by_text("Compra completada").first).to_be_visible(timeout=30_000)
        activity = page.get_by_test_id("purchase-activity")
        expect(activity).to_have_attribute("data-status", "completed", timeout=15_000)
        receipt = page.get_by_test_id("approval-receipt")
        expect(receipt).to_contain_text("Aprobado por ti")
        expect(receipt).to_contain_text("autorización firmada")
        # La fila del stream sale cerrada y deshabilitada; la de la traza la reemplaza ya abierta
        # (`defaultOpen`). Un clic a ciegas la cerraba si la traza llegaba antes: se abre solo si
        # hace falta y se espera a que quede abierta.
        expect(activity).to_have_attribute("data-source", "trace", timeout=15_000)
        summary = activity.get_by_role("button", name=re.compile("Compra completada"))
        if summary.get_attribute("aria-expanded") != "true":
            summary.click()
        expect(summary).to_have_attribute("aria-expanded", "true")
        expect(activity.get_by_text("Política desactivada")).to_be_visible()
        expect(activity.get_by_text("Orden confirmada")).to_be_visible()
        recorded.step("orden", activity.get_by_text("Orden confirmada"))
    assert errors == []

    with closing(sqlite3.connect(ui.database)) as connection:
        rows = connection.execute(
            "SELECT p.owner_id, p.stage, c.checkout_receipt_jwt, c.payment_receipt_jwt "
            "FROM purchases p JOIN completion_results c ON c.run_id = p.run_id"
        ).fetchall()
    # One purchase, owned by the JWT subject (not the local principal), with both receipts.
    assert len(rows) == 1
    owner, stage, checkout_receipt, payment_receipt = rows[0]
    assert owner != "local-human" and stage == "completed"
    assert checkout_receipt and payment_receipt
    # The Merchant's order events go out signed; the lane's receiver takes them.
    delivered = b0.rpc("dispatch_webhooks", limit=10)
    assert delivered and {item["outcome"] for item in delivered} == {"delivered"}
