import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Route, sync_playwright

ROOT = Path(__file__).parents[2]
ARTIFACTS = ROOT / ".local" / "ui-e2e"
FIXTURES = ROOT / "ui" / "src" / "storybook" / "fixtures" / "assistant-transport"
ORIGIN = os.environ.get("UI_TEST_ORIGIN", "http://127.0.0.1:5173")

PURCHASE_REQUEST = "Quiero audífonos negros por menos de 800 USD"
PURCHASE_RATIONALE = "Elegí los Sonora negros porque cumplen el presupuesto y están disponibles."
COMPLETED_TEXT = "La compra quedó completada. El comercio registró la orden ord_storybook_1."
RUN_ID = "run_52df74bfc304253c74087d9927446bb3"

COMPLETED = {
    "id": "cs_storybook_1",
    "status": "completed",
    "currency": "usd",
    "totals": [{"type": "total", "display_text": "Total", "amount": 74000}],
    "expires_at": "2099-09-20T13:00:00Z",
    "order": {
        "id": "ord_storybook_1",
        "checkout_session_id": "cs_storybook_1",
        "permalink_url": "http://127.0.0.1:4120/orders/ord_storybook_1",
        "order_number": "BROWSER-001",
        "status": "confirmed",
    },
}


def fixture(name: str) -> str:
    """Stream SSE real del backend; `scripts/export_assistant_fixtures.py` los regenera."""
    return (FIXTURES / f"{name}.sse").read_text(encoding="utf-8")


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    assistant_bodies: list[dict[str, object]] = []

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
        if path == "/assistant" and request.method == "POST":
            body = json.loads(request.post_data or "{}")
            assistant_bodies.append(body)
            command = (body.get("commands") or [{}])[-1]
            # El segundo turno continúa el estado del primero, así que los fixtures encajan.
            name = "purchase-completed" if command.get("type") == "purchase-updated" else "purchase"
            route.fulfill(
                status=200,
                headers={**headers, "Content-Type": "text/event-stream"},
                body=fixture(name),
            )
            return
        if path == "/health":
            payload: dict[str, object] = {"status": "ok"}
        elif path.endswith("/approval") and request.method == "POST":
            payload = {
                "run_id": RUN_ID,
                "checkout_id": "cs_storybook_1",
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
        page.get_by_role("button", name=PURCHASE_REQUEST).wait_for()
        page.screenshot(path=ARTIFACTS / "desktop-home.png", full_page=True)

        composer = page.get_by_label("Mensaje de compra")
        composer.fill(PURCHASE_REQUEST)
        composer.press("Enter")

        card = page.get_by_test_id("purchase-approval-card")
        card.wait_for()
        page.get_by_test_id("product-card-hero").wait_for()
        page.get_by_text(PURCHASE_RATIONALE).wait_for()
        page.wait_for_function(
            "document.querySelector('[data-testid=purchase-approval-card]')"
            "?.dataset.status === 'awaiting_approval'"
        )
        assert page.evaluate("window.scrollY") == 0
        page.screenshot(path=ARTIFACTS / "desktop-approval.png", full_page=True)

        assert len(assistant_bodies) == 1
        body = assistant_bodies[0]
        commands = body["commands"]
        assert isinstance(commands, list)
        assert commands[0]["type"] == "add-message"
        assert commands[0]["message"]["parts"][0]["text"] == PURCHASE_REQUEST
        assert isinstance(body["turn_id"], str) and body["turn_id"]
        details = body["details"]
        assert isinstance(details, dict)
        assert details["buyer"]["email"] == "ana@example.test"
        assert "phone_number" not in details["buyer"]
        assert details["fulfillment"]["address"]["city"] == "Bogota"
        # Ningún comando transporta evidencia de aprobación ni totales autoritativos.
        assert "fingerprint" not in json.dumps(commands)

        page.get_by_test_id("approve-purchase").click()
        page.get_by_text("Compra completada").wait_for()
        page.get_by_text(COMPLETED_TEXT).wait_for()
        page.get_by_role("link", name="Ver orden").wait_for()

        # El segundo turno es el comando propio purchase-updated, sin datos de aprobación.
        assert len(assistant_bodies) == 2
        follow_up = assistant_bodies[1]["commands"]
        assert follow_up == [{"type": "purchase-updated", "runId": RUN_ID}]
        page.screenshot(path=ARTIFACTS / "desktop-completed.png", full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload(wait_until="networkidle")
        page.get_by_text("¿Qué quieres comprar?").wait_for()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=ARTIFACTS / "mobile-home.png", full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
