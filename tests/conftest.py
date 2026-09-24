import pytest

from commerce_agent.config import Settings


@pytest.fixture(autouse=True)
def ignore_developer_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
