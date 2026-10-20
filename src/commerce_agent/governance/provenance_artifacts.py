"""Measured source manifests, LF source profile and installed dependency verification.

Project source archives are recoverable; dependency hashes describe this installed
environment, not a portable binary distribution or a full interpreter attestation.
"""

import base64
import csv
import hashlib
import io
import os
import platform
import sys
import time
import tomllib
import unicodedata
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path
from threading import Lock
from typing import Literal

from .canonical import canonical_bytes, sha256_digest
from .evaluator import GovernanceError

DEPENDENCIES = (
    "pydantic",
    "pydantic_core",
    "annotated-types",
    "typing_extensions",
    "typing-inspection",
    "jsonschema",
    "attrs",
    "jsonschema-specifications",
    "referencing",
    "rpds-py",
)
SOURCE_PATHS = (
    "src/commerce_agent/domain/intent.py",
    "src/commerce_agent/domain/selection.py",
    "src/commerce_agent/domain/__init__.py",
    "src/commerce_agent/acp/feed.py",
    "src/commerce_agent/vendor/acp/2026-04-17/schema.feed.json",
)
type Measured = dict[Literal["algorithm", "normalization"], bytes]
# Every measured path; project sources and lock keep their bytes for a content check.
type Read = dict[Path, bytes | None]
_memo: tuple[Read, object, Measured] | None = None
_memo_lock = Lock()
_RACY_NS = 2_000_000_000  # Coarsest common file timestamp resolution (FAT).


def lf_source(payload: bytes) -> str:
    normalized = payload.replace(b"\r\n", b"\n")
    if b"\r" in normalized:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    return normalized.decode("utf-8")


def _dependency(name: str, locked: dict[str, set[str]], read: Read) -> dict[str, str]:
    package = distribution(name)
    # Installing or removing any distribution beside it changes this directory.
    read[Path(str(package.locate_file(""))).resolve()] = None
    normalized_name = name.lower().replace("_", "-")
    if package.version not in locked.get(normalized_name, set()):
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    record = package.read_text("RECORD")
    if record is None:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    files: dict[str, str] = {}
    for row in csv.reader(io.StringIO(record)):
        path, recorded_hash, _size = row
        if path.endswith(".pyc"):
            continue
        actual_path = Path(str(package.locate_file(path))).resolve()
        read[actual_path] = None
        if not recorded_hash:
            if not path.endswith(".dist-info/RECORD"):
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            continue
        if not actual_path.is_relative_to(Path(sys.prefix).resolve()):
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        algorithm, expected = recorded_hash.split("=", 1)
        if algorithm != "sha256":
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        actual = hashlib.sha256(actual_path.read_bytes()).digest()
        if base64.urlsafe_b64encode(actual).decode().rstrip("=") != expected:
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        files[path] = "sha256:" + actual.hex()
    if not files:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    return {
        "name": normalized_name,
        "version": package.version,
        "verified_files_digest": sha256_digest(canonical_bytes(files)),
    }


def _signature(read: Read) -> object:
    """Cheap change key: writes, replacement or reinstall alter ctime, size or inode."""
    try:
        stats = [os.stat(path) for path in read]
    except OSError:
        return None
    # As in racy git, a write within the timestamp tick of a recent change is
    # invisible to stat, so recently changed files are measured again next time.
    settled = time.time_ns() - _RACY_NS
    if any(max(item.st_mtime_ns, item.st_ctime_ns) >= settled for item in stats):
        return None
    return tuple(sys.path), [
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        for item in stats
    ]


def _measure(read: Read) -> Measured:
    package = Path(str(files("commerce_agent")))
    # A wheel carries the lock as package data; a source checkout keeps it at its root.
    lock_path = package / "uv.lock"
    if not lock_path.is_file():
        lock_path = Path(__file__).resolve().parents[3] / "uv.lock"
    raw_lock = read[lock_path] = lock_path.read_bytes()
    lock = lf_source(raw_lock)
    locked: dict[str, set[str]] = {}
    for item in tomllib.loads(lock)["package"]:
        locked.setdefault(item["name"].lower().replace("_", "-"), set()).add(item["version"])
    dependencies = [_dependency(name, locked, read) for name in DEPENDENCIES]
    # Manifest names stay checkout-relative, so identities match in either layout.
    paths = {path: package / path.removeprefix("src/commerce_agent/") for path in SOURCE_PATHS}
    raw = {path: location.read_bytes() for path, location in paths.items()}
    read.update((paths[path], body) for path, body in raw.items())
    source = {path: lf_source(body) for path, body in raw.items()}
    common = {
        "schema_version": "d1-source-manifest/1",
        "source_profile": "project-source-utf8-lf/1",
        "lock_utf8_lf": lock,
        "dependencies": dependencies,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "unicode": unicodedata.unidata_version,
    }
    return {
        "algorithm": canonical_bytes({**common, "role": "eligibility", "sources": source}),
        "normalization": canonical_bytes(
            {
                **common,
                "role": "intent-normalization",
                "sources": {SOURCE_PATHS[0]: source[SOURCE_PATHS[0]]},
            }
        ),
    }


def measure_d1_sources() -> Measured:
    """Setup I/O only. No git process, credentials, network or commercial mutation.

    Dependency hashing runs once per process and again only after an installed file,
    the distributions beside it or sys.path change. Project sources and the lock are
    compared byte for byte on every use; callers keep comparing every result.
    """
    global _memo
    with _memo_lock:
        try:
            if (
                _memo is not None
                and _signature(_memo[0]) == _memo[1]
                and all(
                    body is None or path.read_bytes() == body for path, body in _memo[0].items()
                )
            ):
                return dict(_memo[2])
            _memo = None
            read: Read = {}
            measured = _measure(read)
            signature = _signature(read)
            # Reuse only if a second read after the stat agrees: an earlier write
            # changes that read, and any later write changes the key.
            if signature is not None and _measure({}) == measured:
                _memo = (read, signature, measured)
            return dict(measured)
        except Exception:
            raise GovernanceError("GOV_ARTIFACT_MISMATCH") from None
