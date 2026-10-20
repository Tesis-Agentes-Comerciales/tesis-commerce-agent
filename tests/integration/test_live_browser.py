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
import secrets
import sqlite3
from contextlib import closing

import pytest
from playwright.sync_api import Page, Response, expect, sync_playwright

from tests.integration.live_browser import BuyerUi, browser_requirements
from tests.integration.live_purchase import PHONE_INTENT, PHONE_REQUEST, StaticConversationRouter


def sign_up(page: Page, email: str) -> None:
    page.get_by_role("button", name="Crear cuenta").click()
    page.get_by_label("Nombre").fill("Comprador B0")
    page.get_by_label("Correo").fill(email)
    page.get_by_label("Contraseña").fill(secrets.token_urlsafe(18))
    page.get_by_role("button", name="Crear cuenta").click()
    expect(page.get_by_role("button", name="Salir")).to_be_visible(timeout=15_000)


def test_b0_purchase_from_the_browser_signs_up_approves_and_keeps_both_receipts(b0):
    if problems := browser_requirements():
        pytest.fail("The browser test also requires " + "; ".join(problems), pytrace=False)
    episode = b0.episode("browser")
    # RUN_LIVE_LLM=1 puts the configured OpenAI model behind the boundary (manual runs only).
    router = None
    if os.getenv("RUN_LIVE_LLM") != "1":
        router = StaticConversationRouter(PHONE_REQUEST, PHONE_INTENT)
    with BuyerUi(b0, episode, router) as ui, sync_playwright() as playwright:
        agent_errors: list[str] = []

        def observe(response: Response) -> None:
            if response.url.startswith(ui.agent.origin) and response.status >= 400:
                agent_errors.append(f"{response.status} {response.url.split('?')[0]}")

        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("response", observe)
        try:
            page.goto(ui.origin)
            sign_up(page, f"buyer.{episode.actor_id}@example.test")
            page.get_by_placeholder("Describe lo que quieres comprar...").fill(PHONE_REQUEST)
            page.get_by_role("button", name="Enviar mensaje").click()
            approve = page.get_by_role("button", name="Aprobar y comprar")
            expect(approve).to_be_visible(timeout=60_000)
            assert agent_errors == []
            approve.click()
            expect(page.get_by_text("Compra completada").first).to_be_visible(timeout=30_000)
            activity = page.get_by_test_id("purchase-activity")
            expect(activity).to_have_attribute("data-status", "completed", timeout=15_000)
            receipt = page.get_by_test_id("approval-receipt")
            expect(receipt).to_contain_text("Aprobado por ti")
            expect(receipt).to_contain_text("autorización firmada")
            activity.get_by_role("button", name=re.compile("Compra completada")).click()
            expect(activity.get_by_text("Política desactivada")).to_be_visible()
            expect(activity.get_by_text("Orden confirmada")).to_be_visible()
        finally:
            # Evidence for the nightly artifact; the run directory holds no secret.
            page.screenshot(path=ui.root / "browser.png", full_page=True)
            browser.close()
    assert agent_errors == []

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
