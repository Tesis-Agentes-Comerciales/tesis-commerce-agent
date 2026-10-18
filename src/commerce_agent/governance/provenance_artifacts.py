"""Measured source manifests, LF source profile and installed dependency verification.

Project source archives are recoverable; dependency hashes describe this installed
environment, not a portable binary distribution or a full interpreter attestation.
"""

import base64
import csv
import hashlib
import io
import platform
import sys
import tomllib
import unicodedata
from importlib.metadata import distribution
from pathlib import Path
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


def lf_source(payload: bytes) -> str:
    normalized = payload.replace(b"\r\n", b"\n")
    if b"\r" in normalized:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    return normalized.decode("utf-8")


def _dependency(name: str, locked: dict[str, set[str]]) -> dict[str, str]:
    package = distribution(name)
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
        if not recorded_hash:
            if not path.endswith(".dist-info/RECORD"):
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            continue
        actual_path = Path(str(package.locate_file(path))).resolve()
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


def measure_d1_sources() -> dict[Literal["algorithm", "normalization"], bytes]:
    """Setup I/O only. No git process, credentials, network or commercial mutation."""
    try:
        root = Path(__file__).resolve().parents[3]
        lock = lf_source((root / "uv.lock").read_bytes())
        locked: dict[str, set[str]] = {}
        for item in tomllib.loads(lock)["package"]:
            locked.setdefault(item["name"].lower().replace("_", "-"), set()).add(item["version"])
        dependencies = [_dependency(name, locked) for name in DEPENDENCIES]
        source = {path: lf_source((root / path).read_bytes()) for path in SOURCE_PATHS}
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
    except Exception:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH") from None
