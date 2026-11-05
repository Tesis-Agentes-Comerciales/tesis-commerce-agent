"""The Agent API under uvicorn on 127.0.0.1, in a thread of the test process.

It needs no live stack: CI uses it for a real client disconnect (p0-ux08-ux10.md §5.1), and the
live suite (tests/integration/) serves the Agent with it so its model boundary can be scripted.
"""

import socket
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI


class AgentServer:
    """The Agent API under uvicorn in a thread of this process, on a listener bound here. A new
    server can take the port of a stopped one, so the UI keeps the Agent's origin."""

    def __init__(self, app: FastAPI, port: int = 0):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", port))
        config = uvicorn.Config(app, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.listener]}, daemon=True
        )

    @property
    def port(self) -> int:
        return self.listener.getsockname()[1]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 60) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while True:
            try:
                assert self.thread.is_alive()
                with httpx.Client(trust_env=False, timeout=2) as client:
                    assert client.get(self.origin + "/health").status_code == 200
                return
            except (OSError, EOFError, httpx.HTTPError, AssertionError, RuntimeError):
                if time.monotonic() > deadline:
                    raise RuntimeError("LIVE_STACK_NOT_READY:agent") from None
                time.sleep(0.2)

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread.ident is not None:
            self.thread.join(timeout=15)
        self.listener.close()
