"""Write the delegation API replays the UI's Playwright suite plays back.

Usage: uv run python scripts/export_delegation_replays.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.delegation_replay import REPLAY_DIR, replay_files


def main() -> None:
    with tempfile.TemporaryDirectory() as workdir:
        files = replay_files(Path(workdir))
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    for stale in REPLAY_DIR.iterdir():
        if stale.name not in files:
            stale.unlink()
    for name, content in files.items():
        (REPLAY_DIR / name).write_text(content, encoding="utf-8")
        print(f"wrote {(REPLAY_DIR / name).relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
