"""K16: the HNP messages write amounts as `formatMoney` of the UI does.

ui/src/storybook/fixtures/money-notation.json is the table both sides read; the UI's half is
ui/src/components/agent/agent-format.test.ts.
"""

import json
from pathlib import Path

import pytest

from commerce_agent.delegation.views import format_cop

TABLE = Path(__file__).parents[1] / "ui" / "src" / "storybook" / "fixtures" / "money-notation.json"
NOTATION = json.loads(TABLE.read_text(encoding="utf-8"))


def test_the_shared_table_is_in_pesos() -> None:
    assert NOTATION["currency"] == "COP"


@pytest.mark.parametrize(
    ("minor", "text"), [(case["minor"], case["text"]) for case in NOTATION["cases"]]
)
def test_an_amount_is_written_as_the_ui_writes_it(minor: int, text: str) -> None:
    assert format_cop(minor) == text


def test_the_symbol_is_followed_by_a_no_break_space() -> None:
    assert format_cop(192400000).startswith("$\N{NO-BREAK SPACE}1.924.000")


def test_a_negative_amount_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        format_cop(-1)
