"""A service (Merchant, payment provider or Agent) served by uvicorn on an inherited listener."""

import os
import signal
import socket
import subprocess
from contextlib import suppress
from pathlib import Path

import httpx

from commerce_evaluation.stack.environment import closed, uv_run, wait_until
from commerce_evaluation.stack.fault_proxy import FaultProxy


class ServiceServer:
    """`uvicorn <app>` from another checkout (Merchant or PSP), on a listener made here.

    The listener is bound in this process and inherited (pass_fds + `uvicorn --fd`), so no
    other process can take the port between choosing and serving it. The parent closes its
    copy at once: a stopped service refuses connections instead of queueing them. With a
    proxy, the proxy is the service's public origin and follows it across restarts.
    """

    def __init__(
        self,
        root: Path,
        name: str,
        project: Path,
        app: tuple[str, ...],
        env: dict[str, str],
        ready: str,
        proxy: FaultProxy | None = None,
    ):
        self.root = root
        self.name = name
        self.project = project
        self.app = app
        self.env = env
        self.ready = ready
        self.proxy = proxy
        self.process: subprocess.Popen[bytes] | None = None
        self.port: int | None = None
        self.pids: list[int] = []

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        assert self.process is None
        listener = socket.socket()
        log = (self.root / f"{self.name}.log").open("ab")
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            self.port = listener.getsockname()[1]
            descriptor = listener.fileno()
            self.process = subprocess.Popen(
                [
                    *uv_run(self.project),
                    "uvicorn",
                    *self.app,
                    "--fd",
                    str(descriptor),
                    "--log-level",
                    "warning",
                    "--no-access-log",
                ],
                pass_fds=(descriptor,),
                # The run directory, never the checkout: no service reads a developer .env.
                cwd=self.root,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        finally:
            listener.close()
            log.close()
        self.pids.append(self.process.pid)

        def ready():
            assert self.process is not None and self.process.poll() is None
            with httpx.Client(trust_env=False, timeout=2) as client:
                response = client.get(self.origin + self.ready)
            assert response.status_code == 200 and response.json()["status"] == "ok"

        wait_until(ready, self.name)
        if self.proxy is not None:
            self.proxy.upstream = self.origin

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            # uv and uvicorn share a session of their own; stop both.
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=15)
        self.process = None
        port = self.port
        assert port is not None
        wait_until(lambda: closed(port), f"{self.name} listener closed", timeout=15)
