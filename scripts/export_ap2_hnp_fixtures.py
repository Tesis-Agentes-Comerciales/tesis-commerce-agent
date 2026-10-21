"""Write the golden AP2 Human Not Present chains to `tests/fixtures/ap2_hnp/`.

Usage: uv run python scripts/export_ap2_hnp_fixtures.py

Every run generates new fixture-only keys, so the merchant and the PSP copy the whole directory.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.support.ap2_hnp_fixtures import FIXTURE_DIR, fixture_files


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in fixture_files().items():
        (FIXTURE_DIR / name).write_text(content, encoding="utf-8")
        print(f"wrote {(FIXTURE_DIR / name).relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
