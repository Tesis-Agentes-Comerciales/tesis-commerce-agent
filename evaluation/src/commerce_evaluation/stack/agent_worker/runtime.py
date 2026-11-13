"""The worker's confinement and its protocol helpers; read from its environment."""

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from pydantic import SecretBytes

from commerce_agent.governance.provenance import (
    ProvenanceKeys,
)

ROOT = Path(os.environ["LIVE_STACK_ROOT"]).resolve()
# The proxy is the only peer: the Merchant's public origin for this run.
PORT = int(os.environ["LIVE_STACK_PORT"])
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def confine(event: str, args: tuple[Any, ...]) -> None:
    """Loopback to the proxy only; SQLite and file writes only inside this run's directory."""
    if event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple) or address[:2] != ("127.0.0.1", PORT):
            raise RuntimeError("LIVE_AGENT_SOCKET_REJECTED")
    elif event == "sqlite3.connect":
        if args[0] != ":memory:" and not Path(args[0]).resolve().is_relative_to(ROOT):
            raise RuntimeError("LIVE_AGENT_SQLITE_REJECTED")
    elif event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.name == ".env" or (
            (args[2] or 0) & WRITE_FLAGS
            and not path.is_relative_to(ROOT)
            and str(path) != os.devnull
        ):
            raise RuntimeError("LIVE_AGENT_FILE_REJECTED")


def halt(cutpoint: str) -> None:
    """Announce the cutpoint and park; the harness SIGKILLs this process right here."""
    print(json.dumps({"cutpoint": cutpoint}), flush=True)
    threading.Event().wait()


def keys_from(data: dict[str, Any]) -> ProvenanceKeys:
    return ProvenanceKeys(
        key_id=data["key_id"],
        pseudonym_key=SecretBytes(bytes.fromhex(data["hmac"])),
        encryption_key=SecretBytes(bytes.fromhex(data["aes"])),
    )


def failure(error: Exception) -> dict[str, Any]:
    # No traceback, exception text, credential, request or header export.
    code = (
        str(error)
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", str(error))
        else getattr(error, "code", None)
    )
    if code is not None and not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", code):
        code = None
    return {"ok": False, "error_type": type(error).__name__, "error_code": code}
