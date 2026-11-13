"""Markdown tables and Spanish number formats for the report (suite/report_md.py)."""

from collections.abc import Iterable, Sequence

from commerce_evaluation.suite.reduce import Rate


def table(header: Sequence[object], rows: Iterable[Sequence[object]]) -> str:
    lines = [line(header), "| " + " | ".join("---" for _ in header) + " |"]
    lines += [line(row) for row in rows]
    return "\n".join(lines)


def line(cells: Sequence[object]) -> str:
    return "| " + " | ".join(cell(value) for value in cells) + " |"


def cell(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}".replace(",", ".")
    return " ".join(str(value).split()).replace("|", "\\|")


def rate(given: Rate | None) -> str:
    """A share with its 95 % Wilson interval."""
    if given is None or given.rate is None or given.low is None or given.high is None:
        return "sin datos"
    return f"{pct(given.rate)} ({pct(given.low)} a {pct(given.high)})"


def share(value: float | None) -> str:
    return "sin datos" if value is None else pct(value)


def attack(given: Rate | None) -> tuple[str, str]:
    """How many attacks reached their goal, of how many could be told, and the rate."""
    if given is None:
        return ("sin datos", "sin datos")
    return (f"{given.successes:g} de {given.n}", rate(given))


def pct(value: float) -> str:
    return f"{value * 100:.1f}".replace(".", ",") + " %"


def usd(value: object) -> str:
    return "USD " + f"{float(str(value)):.4f}".replace(".", ",")
