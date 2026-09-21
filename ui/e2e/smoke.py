import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Route, sync_playwright

ARTIFACTS = Path(__file__).parents[2] / ".local" / "ui-e2e"
ORIGIN = os.environ.get("UI_TEST_ORIGIN", "http://127.0.0.1:5173")

PREPARATION = {
    "run_id": "run_browser_1",
    "stage": "awaiting_confirmation",
    "selected_candidate": {
        "product_id": "prod-son-01",
        "product_title": "Audífonos Sonora SON-01",
        "variant_id": "SON-01",
        "variant_title": "Sonora negro",
        "observed_unit_amount_minor": 72000,
        "currency": "USD",
    },
    "checkout": {
        "id": "cs_browser_1",
        "status": "ready_for_payment",
        "currency": "usd",
        "totals": [{"type": "total", "display_text": "Total", "amount": 74000}],
        "expires_at": "2099-09-20T13:00:00Z",
    },
    "price_changed": False,
}

TERMS = {
    "merchant_origin": "http://127.0.0.1:4120",
    "checkout_id": "cs_browser_1",
    "currency": "usd",
    "total_minor": 74000,
    "lines": [
        {
            "item_id": "SON-01",
            "name": "Audífonos Sonora SON-01",
            "quantity": 1,
            "unit_amount_minor": 72000,
        }
    ],
    "selected_fulfillment_options": [
        {"type": "shipping", "option_id": "ship_1", "item_ids": ["li_1"]}
    ],
    "buyer": {"email": "ana@example.test"},
    "fulfillment_details": {"address": {"city": "Bogotá", "country": "CO"}},
    "payment_handler": "tesis_sandbox",
    "action": "complete_purchase",
    "expires_at": "2099-09-20T13:00:00Z",
    "fingerprint_version": "v1",
    "fingerprint": "a" * 64,
}

COMPLETED = {
    **PREPARATION["checkout"],
    "status": "completed",
    "order": {
        "id": "ord_browser_1",
        "checkout_session_id": "cs_browser_1",
        "permalink_url": "http://127.0.0.1:4120/orders/ord_browser_1",
        "order_number": "BROWSER-001",
        "status": "confirmed",
    },
}


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    turn_bodies: list[dict[str, object]] = []

    def api(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        headers = {
            "Access-Control-Allow-Origin": ORIGIN,
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Content-Type": "application/json",
        }
        if request.method == "OPTIONS":
            route.fulfill(status=204, headers=headers)
            return
        if path == "/health":
            payload = {"status": "ok"}
        elif path == "/conversation/turn" and request.method == "POST":
            conversation = json.loads(request.post_data or "{}")
            turn_bodies.append(conversation)
            latest = conversation["messages"][-1]["content"]
            if latest == "hola":
                payload = {
                    "action": "respond",
                    "assistant_message": (
                        "¡Hola! Dime qué producto buscas y, si quieres, tu presupuesto o "
                        "preferencias."
                    ),
                    "purchase": None,
                    "approval_terms": None,
                }
            else:
                payload = {
                    "action": "purchase",
                    "assistant_message": (
                        "Elegí los Sonora negros porque cumplen el presupuesto y están disponibles."
                    ),
                    "purchase": PREPARATION,
                    "approval_terms": TERMS,
                }
        elif path.endswith("/approval") and request.method == "POST":
            payload = {
                "run_id": "run_browser_1",
                "checkout_id": "cs_browser_1",
                "principal_id": "local-human",
                "approved_at": "2026-09-20T12:00:00Z",
                "stage": "approved",
            }
        elif path.endswith("/complete") and request.method == "POST":
            payload = COMPLETED
        else:
            route.fulfill(status=404, headers=headers, body='{"message":"not found"}')
            return
        route.fulfill(status=200, headers=headers, body=json.dumps(payload))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 980}, device_scale_factor=1)
        page.route("http://127.0.0.1:43120/**", api)
        page.goto(ORIGIN, wait_until="networkidle")

        assert page.title() == "Comprador ACP"
        page.get_by_text("Agente conectado").wait_for()
        page.get_by_text("¿Qué quieres comprar?").wait_for()
        page.screenshot(path=ARTIFACTS / "desktop-home.png", full_page=True)

        composer = page.get_by_label("Mensaje de compra")
        composer.fill("hola")
        composer.press("Enter")
        page.get_by_text(
            "¡Hola! Dime qué producto buscas y, si quieres, tu presupuesto o preferencias."
        ).wait_for()
        assert len(turn_bodies) == 1

        composer.fill("Quiero audífonos negros, nuevos y disponibles por máximo 800 USD.")
        composer.press("Enter")
        page.get_by_text(
            "Elegí los Sonora negros porque cumplen el presupuesto y están disponibles."
        ).wait_for()
        page.get_by_test_id("receipt-pending").wait_for()
        page.get_by_text("CONFIRMAR COMPRA").wait_for()
        page.wait_for_timeout(500)
        assert page.evaluate("window.scrollY") == 0
        page.screenshot(path=ARTIFACTS / "desktop-approval.png", full_page=True)

        assert len(turn_bodies) == 2
        body = turn_bodies[1]
        assert body["messages"][-1]["content"] == (
            "Quiero audífonos negros, nuevos y disponibles por máximo 800 USD."
        )
        assert "intent" not in body
        details = body["details"]
        assert isinstance(details, dict)
        assert details["buyer"]["email"] == "ana@example.test"
        assert "phone_number" not in details["buyer"]
        assert details["fulfillment"]["address"]["city"] == "Bogota"

        page.get_by_test_id("approve-purchase").click()
        page.get_by_test_id("receipt-completed").wait_for()
        page.get_by_text("Compra completada").wait_for()
        page.screenshot(path=ARTIFACTS / "desktop-completed.png", full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload(wait_until="networkidle")
        page.get_by_text("¿Qué quieres comprar?").wait_for()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=ARTIFACTS / "mobile-home.png", full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
