from commerce_agent.workflow import PurchaseStage


def test_human_confirmation_is_an_explicit_state_boundary() -> None:
    assert PurchaseStage.AWAITING_CONFIRMATION.value == "awaiting_confirmation"
    assert PurchaseStage.APPROVED.value == "approved"
    assert PurchaseStage.COMPLETING.value == "completing"
    assert PurchaseStage.UNCERTAIN.value == "uncertain"
