from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:6007/iframe.html?id=pages-app--payment-declined&viewMode=story"


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(URL, wait_until="networkidle")
            card = page.get_by_test_id("purchase-approval-card")
            card.get_by_role("button", name="Nueva compra").wait_for(timeout=30_000)
            assert "merchant-req-123" in card.get_by_role("alert").inner_text()
            assert card.get_by_role("button", name="Reintentar compra").count() == 0
        finally:
            browser.close()


if __name__ == "__main__":
    main()
