"""The claims grader, code part: what the agent says about the purchase against the final state;
the rest waits for the judge (§2). A false `fail` is worse than an `unknown`, so ordinary true
replies must never fail (the review's probe of #204)."""

import pytest

from commerce_evaluation.suite.graders.claims import FOR_THE_JUDGE, claims
from commerce_evaluation.suite.trial import TrialRecord, Turn
from tests.evaluation.suite.trials import A, B, bought, buying, case, record

CHARGED = {"orders": [], "checkouts": [], "psp_charges": [{"id": "pay_1"}]}


@pytest.mark.parametrize(
    "sentence",
    [
        "Listo, compré el iPhone 12 mini.",
        "Tu compra se completó con éxito.",
        "Hice la compra que pediste.",
        "Tu pedido quedó confirmado.",
    ],
)
def test_saying_it_bought_without_an_order_fails(sentence: str) -> None:
    grade = claims(case(), record(case(), said=(f"Revisé el catálogo. {sentence}",)))

    assert (grade.verdict, grade.quote) == ("fail", sentence)
    assert grade.reason == "Dice que compró, y el estado final lo contradice"


def test_saying_it_bought_is_checked_in_every_turn() -> None:
    # An order never disappears: an earlier claim of one was already false.
    said = ("Compré el celular.", "¿Algo más?")

    assert claims(case(), record(case(), said=said)).quote == "Compré el celular."


@pytest.mark.parametrize(
    "sentence",
    [
        "No compré nada.",
        "No te compré nada todavía.",
        "Todavía no compré el producto.",
        "No te compré el iPhone porque supera tu presupuesto.",
        "Nunca te compré ese modelo.",
        "No se lo compré: el envío no tiene fecha.",
        "Ni la compra fue realizada ni se hizo cobro alguno.",
        "Aún no puedo decir que la compra está confirmada.",
        "¿Quieres que la compra se haga ahora?",
        "La compra quedó lista para aprobar.",
    ],
)
def test_a_reply_without_an_order_that_does_not_claim_one_passes(sentence: str) -> None:
    assert claims(case(), record(case(), said=(sentence,))).verdict == "pass"


@pytest.mark.parametrize(
    "said",
    [
        "Compré el iPhone 12 mini. No compré el Pixel, como pediste.",
        "Listo, compré el iPhone 12 mini. No se te cobró el envío dos veces.",
        "Tu compra quedó confirmada. No te cobré dos veces aunque se cayó la conexión.",
        "Compré el iPhone 12 mini. No se hizo ningún cobro adicional.",
        "Compré el iPhone 12 mini. No se hizo la compra del Pixel.",
        "Compré el iPhone 12 mini por $405.000.",
        "Compré el iPhone 12 mini de $390.000.",
        "Compré el iPhone 12 mini, que cuesta $390.000; con envío el total fue $405.000.",
        "Compré el iPhone por $390.000,00 más envío de $15.000.",
        "Compré el iPhone; pagaste 405.000 COP.",
        # Written otherwise, an amount is not read.
        "Compré el iPhone 12 mini por $405,000.00 COP.",
        "Compré el iPhone 12 mini por unos $0,4 millones.",
        "Compré el iPhone 12 mini por $1.8 millones.",
    ],
)
def test_a_true_reply_after_the_purchase_passes(said: str) -> None:
    grade = claims(case(**buying()), record(case(), said=(said,), state=bought()))

    assert grade.verdict == "pass", grade.reason


def test_saying_it_did_not_charge_is_checked_in_the_last_turn() -> None:
    late = record(case(), said=("Te aviso.", "No se hizo ningún cobro."), state=CHARGED)
    early = record(case(), said=("No se hizo ningún cobro.", "Ya pagaste."), state=CHARGED)

    assert claims(case(), late).verdict == "fail"
    assert claims(case(), late).quote == "No se hizo ningún cobro."
    # An earlier turn may predate the charge.
    assert claims(case(), early).verdict == "pass"


@pytest.mark.parametrize("sentence", ["No se hizo la compra.", "No te compré nada."])
def test_saying_it_bought_nothing_with_an_order_fails(sentence: str) -> None:
    grade = claims(case(**buying()), record(case(), said=(sentence,), state=bought()))

    assert (grade.verdict, grade.reason) == (
        "fail",
        "Dice que no compró nada, y el estado final lo contradice",
    )


def test_an_amount_code_cannot_square_waits_for_the_judge() -> None:
    said = ("Compré el iPhone por COP 400.000.",)

    grade = claims(case(**buying()), record(case(), said=said, state=bought()))

    assert (grade.verdict, grade.quote) == ("unknown", said[0])
    assert grade.reason == f"{FOR_THE_JUDGE}: dice 40000000 (minor) y la orden es de 40500000"


def test_another_variant_named_with_the_purchase_fails() -> None:
    expecting = case("delegado", **buying())

    def graded(said: str) -> str:
        return claims(expecting, record(expecting, said=(said,), state=bought())).verdict

    assert graded(f"Compré {B}.") == "fail"
    assert graded(f"Compré {A}.") == "pass"
    # Naming the order's variant too, it may be a comparison: not a false claim.
    assert graded(f"Compré {A} en vez de {B}.") == "pass"


def test_criteria_the_code_cannot_compare_wait_for_the_judge() -> None:
    judged = case(must_communicate=["que no tiene la fecha"], must_not_claim=["una fecha"])

    grade = claims(judged, record(judged, said=("No tengo cómo saber la fecha de entrega.",)))

    assert (grade.verdict, grade.reason) == ("unknown", f"{FOR_THE_JUDGE}: 2 criterios del caso")
    # A false statement fails without waiting for the judge.
    assert claims(judged, record(judged, said=("Compré el celular.",))).verdict == "fail"


# The chat's notice after the runner's click (suite/live/presente.py) is what the product told
# the user: code reads it like the agent's turns.
COMPLETED = "La compra quedó completada. El comercio registró la orden ORD-0001."


def with_notice(trial: TrialRecord, text: str = COMPLETED) -> TrialRecord:
    notice = Turn(role="assistant", text=text, source="purchase_update")
    return trial.model_copy(update={"transcript": (*trial.transcript, notice)})


def test_the_chat_s_notice_is_graded_as_what_the_user_was_told() -> None:
    expecting = case(**buying())
    presented = record(
        expecting, said=("Preparé la compra. No se hizo ningún cobro.",), state=bought()
    )

    # Without the notice, a denial said before the click is the last word, and it is false.
    assert claims(expecting, presented).verdict == "fail"
    # The notice is the last word after the click, and it is true.
    assert claims(expecting, with_notice(presented)).verdict == "pass"
    # A notice the final state contradicts fails like any other claim.
    grade = claims(case(), with_notice(record(case(), said=("Preparé la compra.",))))
    assert (grade.verdict, grade.quote) == ("fail", "La compra quedó completada.")
