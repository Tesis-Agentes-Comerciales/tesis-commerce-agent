from pathlib import Path

from commerce_agent.persistence import SqliteAgentStore


def store(tmp_path: Path) -> SqliteAgentStore:
    result = SqliteAgentStore(tmp_path / "agent.db")
    result.initialize()
    return result


def test_purchase_and_idempotency_keys_are_durable(tmp_path: Path) -> None:
    first = store(tmp_path)
    first.create_purchase("run-1", "buy headphones")
    key = first.idempotency_key("run-1", "checkout.create")
    assert first.idempotency_key("run-1", "checkout.create") == key

    second = SqliteAgentStore(tmp_path / "agent.db")
    second.initialize()
    assert second.idempotency_key("run-1", "checkout.create") == key
    second.create_purchase("run-1", "buy headphones")
    second.update_purchase(
        "run-1",
        stage="checkout_prepared",
        selected_variant_id="SON-01",
        checkout_id="cs_1",
    )
    assert second.purchase("run-1") == {
        "run_id": "run-1",
        "request_text": "buy headphones",
        "stage": "checkout_prepared",
        "selected_variant_id": "SON-01",
        "checkout_id": "cs_1",
        "order_id": None,
        "created_at": second.purchase("run-1")["created_at"],  # type: ignore[index]
        "updated_at": second.purchase("run-1")["updated_at"],  # type: ignore[index]
    }


def test_conversation_turns_are_cached_and_reject_conflicting_reuse(tmp_path: Path) -> None:
    agent_store = store(tmp_path)

    assert agent_store.claim_conversation_turn("turn-1", "a" * 64) is None
    agent_store.save_conversation_turn("turn-1", "a" * 64, {"action": "clarify"})
    assert agent_store.claim_conversation_turn("turn-1", "a" * 64) == {"action": "clarify"}

    try:
        agent_store.claim_conversation_turn("turn-1", "b" * 64)
    except ValueError as error:
        assert "conflicts" in str(error)
    else:
        raise AssertionError("conflicting turn reuse was accepted")


def test_completion_reservation_is_compare_and_swap(tmp_path: Path) -> None:
    first = store(tmp_path)
    first.create_purchase("run-1", "buy")
    first.update_purchase("run-1", stage="approved", checkout_id="cs_1")
    second = SqliteAgentStore(tmp_path / "agent.db")
    second.initialize()

    assert first.claim_completion("run-1")
    assert not second.claim_completion("run-1")
    assert first.purchase("run-1")["stage"] == "completing"  # type: ignore[index]

    first.release_completion("run-1", uncertain=True)
    assert second.claim_completion("run-1")


def test_webhook_deduplicates_body_independently_of_request_id(tmp_path: Path) -> None:
    agent_store = store(tmp_path)
    agent_store.create_purchase("run-1", "buy")
    agent_store.update_purchase("run-1", stage="approved", checkout_id="cs_1")
    payload = {
        "type": "order_create",
        "data": {
            "id": "ord_1",
            "checkout_session_id": "cs_1",
            "status": "confirmed",
        },
    }
    assert agent_store.record_webhook(
        request_id="whd_1",
        event_type="order_create",
        order_id="ord_1",
        payload_sha256="a" * 64,
        payload=payload,
    )
    assert not agent_store.record_webhook(
        request_id="whd_2",
        event_type="order_create",
        order_id="ord_1",
        payload_sha256="a" * 64,
        payload=payload,
    )


def test_order_projection_does_not_regress(tmp_path: Path) -> None:
    agent_store = store(tmp_path)
    agent_store.create_purchase("run-1", "buy")
    agent_store.update_purchase("run-1", stage="approved", checkout_id="cs_1")
    processing = {
        "type": "order_update",
        "data": {
            "id": "ord_1",
            "checkout_session_id": "cs_1",
            "status": "processing",
        },
    }
    confirmed = {
        "type": "order_update",
        "data": {
            "id": "ord_1",
            "checkout_session_id": "cs_1",
            "status": "confirmed",
        },
    }
    assert agent_store.record_webhook(
        request_id="whd_1",
        event_type="order_update",
        order_id="ord_1",
        payload_sha256="a" * 64,
        payload=processing,
    )
    assert agent_store.record_webhook(
        request_id="whd_2",
        event_type="order_update",
        order_id="ord_1",
        payload_sha256="b" * 64,
        payload=confirmed,
    )
    assert agent_store.order("ord_1") == processing


def test_authenticated_event_for_unknown_checkout_is_quarantined(tmp_path: Path) -> None:
    agent_store = store(tmp_path)
    payload = {
        "type": "order_create",
        "data": {
            "id": "ord_unknown",
            "checkout_session_id": "cs_unknown",
            "status": "confirmed",
        },
    }

    assert agent_store.record_webhook(
        request_id="whd_unknown",
        event_type="order_create",
        order_id="ord_unknown",
        payload_sha256="c" * 64,
        payload=payload,
    )
    assert agent_store.order("ord_unknown") is None
    assert not agent_store.record_webhook(
        request_id="whd_unknown_replay",
        event_type="order_create",
        order_id="ord_unknown",
        payload_sha256="c" * 64,
        payload=payload,
    )
