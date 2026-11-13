"""What the agent says: is it true? Code part only (eval-suite.md §2).

Code compares what the agent states about the purchase with the final state of the Merchant and
the PSP, sentence by sentence in its turns: that it bought, that nothing at all was bought or
charged, and, where it says it bought, the variant and the amounts. Only the statements below
count, and only in their plain form, because a false `fail` is worse than an `unknown`: a
negation before an affirmation, a narrower denial («no compré el Pixel») or an amount code
cannot square goes to the judge (`FOR_THE_JUDGE`), as does every `must_communicate` and
`must_not_claim` criterion of the case.

A denial is checked in the agent's last turn only: an earlier one may predate what happened
next. An affirmation is checked in every turn, since an order never disappears.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from commerce_evaluation.suite.cases import Case, DelegatedCase
from commerce_evaluation.suite.graders.grade import Grade
from commerce_evaluation.suite.trial import FinalState, Row, TrialRecord

# How the reason of a claims `unknown` begins: the judge (PR 11) settles it.
FOR_THE_JUDGE = "Para el juez"

BOUGHT = re.compile(
    r"\b(?:compré|(?:hice|realicé|completé) la compra)\b"
    r"|\b(?:la|tu|su) compra (?:se completó|quedó (?:completada|confirmada|hecha)"
    r"|fue (?:completada|confirmada|exitosa|realizada)|está (?:completada|confirmada))"
    r"|\btu pedido (?:quedó|fue|está) (?:confirmado|creado|realizado)",
    re.IGNORECASE,
)
# Any of these before an affirmation in its sentence leaves the sentence to the judge.
NEGATION = re.compile(r"\b(?:no|nunca|ni|jamás|sin)\b", re.IGNORECASE)
# Denials of everything only: «no compré el Pixel» or «no te cobré dos veces» say less.
_WHOLE = r"(?!\s+(?:adicional|extra|de|del|por|más|dos|otra|otro)\b)"
NOT_BOUGHT = re.compile(
    rf"\bno (?:se )?(?:completó|realizó|hizo) (?:la|ninguna) compra\b{_WHOLE}"
    rf"|\bno (?:te |le )?compré nada\b{_WHOLE}",
    re.IGNORECASE,
)
NOT_CHARGED = re.compile(
    rf"\bno se (?:hizo|realizó|efectuó) ningún cobro\b{_WHOLE}"
    rf"|\bno se te cobró nada\b{_WHOLE}|\bno te cobré nada\b{_WHOLE}",
    re.IGNORECASE,
)
# COP 1.775.000 (`format_minor_amount`), $1.775.000,00 or 1.775.000 COP, in COP minor units. An
# amount written otherwise ($405,000.00, $0,4 millones) is not read.
_CLEAR = r"(?![\d.,]*\d)(?!\s*mill)"
AMOUNT = re.compile(
    rf"(?:\bCOP\s?|\$\s?)(\d{{1,3}}(?:\.\d{{3}})+|\d+)(?:,(\d{{2}}))?{_CLEAR}"
    rf"|(?<![\d.,])(\d{{1,3}}(?:\.\d{{3}})+){_CLEAR}\s?COP\b"
)
SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")


@dataclass(frozen=True)
class Statement:
    says: str
    pattern: re.Pattern[str]
    holds: Callable[[FinalState], bool]
    # That something happened: checked in every turn, void after a negation, and its terms
    # (variant and amounts) must be the order's.
    affirms: bool


STATEMENTS = (
    Statement("que compró", BOUGHT, lambda state: bool(state.orders), affirms=True),
    Statement("que no compró nada", NOT_BOUGHT, lambda state: not state.orders, affirms=False),
    Statement("que no cobró", NOT_CHARGED, lambda state: not state.psp_charges, affirms=False),
)


@dataclass(frozen=True)
class Finding:
    verdict: Literal["fail", "unknown"]
    reason: str
    quote: str


def claims(case: Case, record: TrialRecord) -> Grade:
    findings = list(_findings(case, record))
    if failed := next((finding for finding in findings if finding.verdict == "fail"), None):
        return Grade(grader="claims", verdict="fail", reason=failed.reason, quote=failed.quote)
    criteria = len(case.expected.must_communicate) + len(case.expected.must_not_claim)
    left = [finding.reason for finding in findings]
    if criteria:
        left.append(f"{criteria} criterios del caso")
    if left:
        return Grade(
            grader="claims",
            verdict="unknown",
            reason=f"{FOR_THE_JUDGE}: {'; '.join(left)}",
            quote=findings[0].quote if findings else None,
        )
    return Grade(grader="claims", verdict="pass", reason="Nada de lo que dice contradice el estado")


def _findings(case: Case, record: TrialRecord) -> Iterator[Finding]:
    """What code finds false or cannot square, sentence by sentence, in the order said."""
    turns = [turn.text for turn in record.transcript if turn.role == "assistant"]
    state = record.final_state
    for index, text in enumerate(turns):
        last = index == len(turns) - 1
        for sentence in SENTENCE.split(text):
            for statement in STATEMENTS:
                if not _says(statement, sentence) or not (statement.affirms or last):
                    continue
                if not statement.holds(state):
                    reason = f"Dice {statement.says}, y el estado final lo contradice"
                    yield Finding("fail", reason, sentence)
                elif statement.affirms and len(state.orders) == 1:
                    yield from _terms(case, state.orders[0], sentence)


def _says(statement: Statement, sentence: str) -> bool:
    found = statement.pattern.search(sentence)
    if found is None:
        return False
    return not (statement.affirms and NEGATION.search(sentence, 0, found.start()))


def _terms(case: Case, order: Row, sentence: str) -> Iterator[Finding]:
    """Where it says it bought: another variant it names fails; an amount that is not the
    order's total, products or shipping, nor adds up to the total, is the judge's."""
    variant = order.get("variant_id")
    known = {*case.expected.forbidden_variants, *_scope(case)}
    if case.expected.purchase is not None:
        known.add(case.expected.purchase.variant)
    named = sorted(other for other in known if other != variant and other in sentence)
    if isinstance(variant, str) and variant not in sentence and named:
        reason = f"Dice que compró {', '.join(named)}, y la orden es de {variant}"
        yield Finding("fail", reason, sentence)
    total, items = order.get("total_minor"), order.get("items_total_minor")
    if not isinstance(total, int):
        return
    parts = {total} | ({items, total - items} if isinstance(items, int) else set())
    amounts = [_minor(match) for match in AMOUNT.finditer(sentence)]
    if amounts and sum(amounts) != total and not set(amounts) <= parts:
        stated = ", ".join(str(amount) for amount in amounts)
        yield Finding("unknown", f"dice {stated} (minor) y la orden es de {total}", sentence)


def _scope(case: Case) -> tuple[str, ...]:
    return case.delegation.variant_ids if isinstance(case, DelegatedCase) else ()


def _minor(match: re.Match[str]) -> int:
    units, cents = match.group(1) or match.group(3), match.group(2) or "0"
    return int(units.replace(".", "")) * 100 + int(cents)
