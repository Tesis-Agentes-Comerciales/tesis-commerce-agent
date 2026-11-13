"""An Agent process of the live stack for restarts and crashes at named cutpoints."""

import json
import secrets
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from commerce_evaluation.stack.environment import clean_environment
from commerce_evaluation.stack.live_stack import LiveStack

AGENT_WORKER = "commerce_evaluation.stack.agent_worker"


class CutpointReached(RuntimeError):
    pass


class AgentProcess:
    """`agent_worker` under this checkout's interpreter; killable at named cutpoints."""

    def __init__(self, stack: LiveStack, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.log = (directory / ("agent-" + secrets.token_hex(4) + ".log")).open("ab")
        env = clean_environment()
        env.update(LIVE_STACK_ROOT=str(directory), LIVE_STACK_PORT=str(stack.proxy.port))
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-m", AGENT_WORKER],
            cwd=directory,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            encoding="utf-8",
        )
        self.pid = self.process.pid

    def _send(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(
            json.dumps(
                {"action": action, **data},
                default=lambda value: (
                    value.get_secret_value() if isinstance(value, SecretStr) else None
                ),
            )
            + "\n"
        )
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        assert line, "LIVE_STACK_AGENT_STOPPED"
        return json.loads(line)

    def call(self, action: str, *, required: bool = True, **data: Any) -> Any:
        reply = self._send(action, data)
        if "cutpoint" in reply:
            raise CutpointReached(reply["cutpoint"])
        if required:
            assert reply["ok"], (
                f"Agent worker rejected {action}: {reply.get('error_type')}:"
                f"{reply.get('error_code')}"
            )
            return reply["result"]
        return reply

    def kill_at(self, cutpoint: str, action: str, **data: Any) -> int:
        """Run an action that parks at a cutpoint, then SIGKILL the process there."""
        reply = self._send(action, {**data, "cutpoint": cutpoint})
        assert reply == {"cutpoint": cutpoint}, "Agent did not reach the cutpoint"
        self.process.kill()
        code = self.process.wait(timeout=20)
        self._release()
        return code

    def close(self) -> None:
        if self.process.poll() is None:
            with suppress(OSError, AssertionError):
                self._send("close", {})
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self._release()

    def _release(self) -> None:
        for stream in (self.process.stdin, self.process.stdout):
            if stream and not stream.closed:
                with suppress(OSError):
                    stream.close()
        self.log.close()
