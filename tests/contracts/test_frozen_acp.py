import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "src" / "commerce_agent" / "vendor" / "acp"


def test_frozen_acp_sources_match_recorded_hashes() -> None:
    provenance: dict[str, Any] = json.loads(
        (VENDOR / "provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["version"] == "2026-04-17"
    assert provenance["commit"] == "7fdd78df677a94dce04c770644b0fbbb1401272b"

    for record in provenance["files"]:
        source = VENDOR / record["path"]
        assert source.is_file(), f"Missing frozen ACP source: {record['path']}"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == record["sha256"]
        assert record["modified"] is False
