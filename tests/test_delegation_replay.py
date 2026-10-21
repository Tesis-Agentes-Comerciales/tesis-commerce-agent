"""The UI replays real delegation API answers (HNP-10): the committed files match the backend."""

from pathlib import Path

from tests.delegation_replay import REPLAY_DIR, replay_files


def test_committed_replays_match_the_backend_output(tmp_path: Path) -> None:
    generated = replay_files(tmp_path)
    committed = {path.name: path.read_text(encoding="utf-8") for path in REPLAY_DIR.iterdir()}
    assert sorted(committed) == sorted(generated)
    for name, content in generated.items():
        assert committed[name] == content, (
            f"{name} is stale; run scripts/export_delegation_replays.py"
        )
