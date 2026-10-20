import os
from pathlib import Path

import pytest

from commerce_agent.config import Settings

TESTS = Path(__file__).parent
# Layers follow the directory (docs/testing.md): a new file is classified by where it lives.
LIVE = TESTS / "integration"
EVALUATION = (TESTS / "evaluation", TESTS / "hnp_experiment")
LIVE_OPT_IN = "RUN_LIVE_STACK"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_live = pytest.mark.skip(reason=f"requires {LIVE_OPT_IN}=1 (docs/testing.md)")
    for item in items:
        if item.path.is_relative_to(LIVE):
            item.add_marker(pytest.mark.live)
            # Opting in without a requirement fails in the stack fixture, never skips quietly.
            if os.environ.get(LIVE_OPT_IN) != "1":
                item.add_marker(skip_live)
        elif any(item.path.is_relative_to(directory) for directory in EVALUATION):
            item.add_marker(pytest.mark.evaluation)


@pytest.fixture(autouse=True)
def ignore_developer_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
