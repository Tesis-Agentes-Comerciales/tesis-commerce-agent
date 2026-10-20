"""Write deterministic `/assistant` SSE fixtures for Storybook.

Usage: uv run python scripts/export_assistant_fixtures.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.assistant_support import FIXTURE_DIR, fixture_files


def main() -> None:
    with tempfile.TemporaryDirectory() as workdir:
        files = fixture_files(Path(workdir))
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in FIXTURE_DIR.glob("*.sse"):
        if stale.name not in files:
            stale.unlink()
    for name, content in files.items():
        (FIXTURE_DIR / name).write_text(content, encoding="utf-8")
        print(f"wrote {(FIXTURE_DIR / name).relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
