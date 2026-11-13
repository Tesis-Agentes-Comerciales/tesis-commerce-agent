"""The Agent's webhook receiver of a purchase lane, on a loopback HTTP server."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fastapi.testclient import TestClient

from commerce_agent.config import Settings
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.webhooks import create_webhook_app


class WebhookReceiver:
    """The Agent's webhook app behind a loopback HTTP server the Merchant posts to."""

    def __init__(self, store: SqliteAgentStore, secret: str):
        self._secret = secret
        self.client = self._client(store)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                response = receiver.client.post(self.path, content=body, headers=dict(self.headers))
                self.send_response(response.status_code)
                self.send_header(
                    "Content-Type", response.headers.get("content-type", "application/json")
                )
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _client(self, store: SqliteAgentStore) -> TestClient:
        # Explicit store and secret; defaults only, so no developer .env or variable leaks in.
        settings = Settings.model_construct()
        return TestClient(create_webhook_app(store=store, secret=self._secret, settings=settings))

    @contextmanager
    def delivering_to(self, store: SqliteAgentStore) -> Iterator[None]:
        """Webhooks go to another Agent store (a runtime's own database) inside the block."""
        lane = self.client
        self.client = self._client(store)
        try:
            yield
        finally:
            self.client.close()
            self.client = lane

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.client.close()
