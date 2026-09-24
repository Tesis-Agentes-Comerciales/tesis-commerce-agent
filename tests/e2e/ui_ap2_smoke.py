"""Exercise the public Human Present flow through a real browser."""

import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    url = os.environ["UI_AP2_SMOKE_URL"]
    database = Path(os.environ["AGENT_DATABASE_PATH"])
    email = f"ap2-ui-{uuid4().hex[:12]}@example.test"
    failed_requests: list[str] = []
    with sync_playwright() as playwright:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(chrome) if chrome.is_file() else None,
        )
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})

            def observe_response(response) -> None:  # type: ignore[no-untyped-def]
                if response.status >= 400 and (
                    "/purchases/" in response.url or "/assistant" in response.url
                ):
                    failed_requests.append(f"{response.status} {response.url.split('?')[0]}")

            page.on("response", observe_response)
            page.goto(url, wait_until="networkidle")
            expect(page.get_by_role("button", name="Crear cuenta")).to_be_visible()
            page.get_by_role("button", name="Crear cuenta").click()
            page.get_by_label("Nombre").fill("Comprador AP2")
            page.get_by_label("Correo").fill(email)
            page.get_by_label("Contraseña").fill("DemoAP2!2026-Temporary")
            page.get_by_role("button", name="Crear cuenta").click()
            expect(page.get_by_role("button", name="Salir")).to_be_visible(timeout=15_000)
            page.wait_for_load_state("networkidle")

            composer = page.get_by_placeholder("Describe lo que quieres comprar...")
            expect(composer).to_be_visible()
            composer.fill("Hola, ¿qué puedes hacer?")
            page.get_by_role("button", name="Enviar mensaje").click()
            expect(page.get_by_text("Hola, ¿qué puedes hacer?", exact=True)).to_be_visible()
            expect(page.get_by_role("heading", name="Respuesta del agente")).to_be_visible(
                timeout=45_000
            )
            expect(page.get_by_role("button", name="Copiar respuesta")).to_be_visible(
                timeout=45_000
            )
            assert "error" not in page.locator("body").inner_text().lower()

            composer.fill(
                "Quiero comprar unos audífonos Sonora negros nuevos por menos de 800 USD "
                "incluyendo el envío."
            )
            page.get_by_role("button", name="Enviar mensaje").click()
            approve = page.get_by_role("button", name="Aprobar y comprar")
            expect(approve).to_be_visible(timeout=90_000)
            assert failed_requests == [], failed_requests
            assert page.get_by_text("Sonora", exact=False).count() > 0
            approve.click()
            expect(page.get_by_text("Compra completada", exact=False)).to_be_visible(timeout=30_000)
            assert failed_requests == [], failed_requests
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT c.checkout_receipt_jwt, c.payment_receipt_jwt "
                    "FROM completion_results c JOIN purchases p ON p.run_id = c.run_id "
                    "WHERE p.owner_id != 'local-human' ORDER BY c.completed_at DESC LIMIT 1"
                ).fetchone()
            assert row is not None and all(row), "AP2 receipts were not persisted"
            screenshot = (
                Path(__file__).resolve().parents[3] / "output" / f"ap2-ui-{uuid4().hex[:8]}.png"
            )
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot), full_page=True)
            print("UI AP2 PASS: signup, chat, approval, order, both receipts")
            print(f"Screenshot: {screenshot}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
