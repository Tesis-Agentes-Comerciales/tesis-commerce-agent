"""The presente user (docs/design/eval-suite.md §3): scripted in regression, simulated by a model
otherwise, and never shown the case's oracle. Only the offline fake answers."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.llm.budget import BudgetedTransport, RunBudget
from commerce_evaluation.llm.cost import NO_USAGE
from commerce_evaluation.llm.transport import (
    FakeReply,
    FakeTransport,
    FundsExhausted,
    TransportError,
    TransportTimeout,
)
from commerce_evaluation.paths import DATA
from commerce_evaluation.suite.cases import PresentCase, load_catalog
from commerce_evaluation.suite.trial import Turn
from commerce_evaluation.suite.users import (
    ScriptedUser,
    SimulatedUser,
    User,
    UserReply,
    instructions,
)

CATALOG = DATA / "suite" / "cases"
UX12 = CATALOG / "pedido-del-usuario/entrega-no-verificable/presente.json"
REPLY = {"message": "  No sé, la verdad. ¿Me puedes decir cuándo llega?  ", "done": False}


def case(**user: Any) -> PresentCase:
    raw = json.loads(UX12.read_text(encoding="utf-8"))
    raw["user"] |= user
    return PresentCase.model_validate_json(json.dumps(raw))


def turns(*texts: str) -> list[Turn]:
    roles = ("user", "assistant")
    return [Turn(role=roles[index % 2], text=text) for index, text in enumerate(texts)]


def simulated(*replies: FakeReply, **options: Any) -> tuple[SimulatedUser, FakeTransport]:
    script = iter(replies)
    fake = FakeTransport(lambda _: next(script))
    return SimulatedUser(case(), fake, **options), fake


def test_the_scripted_user_says_its_lines_in_order_then_ends():
    user: User = ScriptedUser(case(script=["Quiero el A15.", "Mejor el negro."]))

    assert user.next_message([]) == "Quiero el A15."
    assert user.next_message(turns("Quiero el A15.", "¿De qué color?")) == "Mejor el negro."
    assert user.next_message(turns("Quiero el A15.", "¿Color?", "Mejor el negro.", "Listo")) is None


def test_the_simulated_user_opens_with_the_script_word_for_word_at_no_cost(tmp_path: Path):
    run_budget = RunBudget(max_usd=Decimal(1), run_dir=tmp_path)
    fake = FakeTransport(lambda _: FakeReply(output=REPLY))
    transport = BudgetedTransport(fake, budget=run_budget, role="simulator")
    user: User = SimulatedUser(case(), transport)

    assert user.next_message([]) == case().user.script[0]
    assert fake.requests == []
    assert run_budget.spent_usd == 0 and not run_budget.ledger_path.exists()


def test_the_simulated_user_asks_the_model_with_the_transcript_and_adds_up_its_usage():
    user, fake = simulated(FakeReply(output=REPLY), FakeReply(output=REPLY))
    opening = case().user.script[0]
    transcript = turns(opening, "Puedo prepararlo, pero no sé cuándo llega.")

    first = user.next_message(transcript)
    second = user.next_message(turns(opening, "No sé cuándo llega.", str(first), "Tampoco."))

    assert first == second == "No sé, la verdad. ¿Me puedes decir cuándo llega?"
    request = fake.requests[0]
    assert (request.model, request.effort) == ("claude-sonnet-5-5", "low")
    assert request.instructions == instructions(case())
    assert request.input_text.splitlines()[2:4] == [
        f"Tú: {opening}",
        "Asistente: Puedo prepararlo, pero no sé cuándo llega.",
    ]
    expected = NO_USAGE
    for recorded in fake.requests:
        usage = FakeTransport(lambda _: FakeReply(output=REPLY)).parse(recorded, UserReply).usage
        assert usage is not None
        expected += usage
    assert (user.usage, user.failure) == (expected, None)


def test_the_runner_chooses_the_simulator_model():
    user, fake = simulated(FakeReply(output=REPLY), model="claude-opus-5-5")

    user.next_message(turns("Hola", "¿Qué buscas?"))

    assert fake.requests[0].model == "claude-opus-5-5"


def test_done_ends_the_conversation_for_good():
    user, fake = simulated(FakeReply(output={"message": "", "done": True}))

    assert user.next_message(turns("Hola", "Listo, quedó comprado.")) is None
    assert user.next_message(turns("Hola", "Listo, quedó comprado.", "¿Algo más?")) is None
    assert len(fake.requests) == 1 and user.failure is None


@pytest.mark.parametrize(
    ("reply", "failure"),
    [
        (FakeReply(failure="refusal"), "refusal"),
        (FakeReply(output=REPLY, failure="incomplete"), "incomplete"),
        (FakeReply(output={"message": "   ", "done": False}), "schema_invalid"),
        (FakeReply(output={"text": "Hola"}), "schema_invalid"),
    ],
    ids=["refusal", "truncated", "silent", "off-schema"],
)
def test_an_unusable_answer_ends_the_conversation_without_a_made_up_message(
    reply: FakeReply, failure: str
):
    user, fake = simulated(reply)

    assert user.next_message(turns("Hola", "¿Qué buscas?")) is None

    assert user.failure == failure and user.usage.input_tokens > 0
    assert user.next_message(turns("Hola", "¿Qué buscas?", "", "¿Sigues ahí?")) is None
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    "error",
    [
        FundsExhausted("CreditBalanceExhausted"),
        TransportError("RateLimitError"),
        TransportTimeout("APITimeoutError"),
    ],
    ids=["funds", "provider", "timeout"],
)
def test_transport_failures_reach_the_runner(error: BaseException):
    user, _ = simulated(FakeReply(raises=error))

    with pytest.raises(type(error)) as raised:
        user.next_message(turns("Hola", "¿Qué buscas?"))

    assert raised.value is error


def oracle(present: PresentCase) -> list[str]:
    expected = present.expected
    texts = [*expected.must_communicate, *expected.must_not_claim, *expected.forbidden_variants]
    if expected.purchase is not None:
        texts += [expected.purchase.variant, str(expected.purchase.total_minor)]
    return [*texts, present.reference.agent_script]


PRESENT = [case for case in load_catalog(CATALOG) if isinstance(case, PresentCase)]


@pytest.mark.parametrize("present", PRESENT, ids=[case.id for case in PRESENT])
def test_the_instructions_carry_the_user_and_never_the_oracle(present: PresentCase):
    text = instructions(present)

    user = present.user
    assert all(item in text for item in (user.goal, *user.known_info, *user.unknown_info))
    assert [leak for leak in oracle(present) if leak in text] == []
