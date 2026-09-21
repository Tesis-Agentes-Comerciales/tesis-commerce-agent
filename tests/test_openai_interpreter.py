from types import SimpleNamespace
from typing import Any

from pydantic import SecretStr

from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.llm import OpenAIIntentInterpreter
from commerce_agent.llm import openai_interpreter as module


class FakeResponses:
    def __init__(self, intent: PurchaseIntent) -> None:
        self.intent = intent
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.intent)


class FakeOpenAI:
    def __init__(self, intent: PurchaseIntent, **kwargs: Any) -> None:
        self.options = kwargs
        self.responses = FakeResponses(intent)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_openai_interpreter_uses_structured_output_without_authority(monkeypatch) -> None:
    expected = PurchaseIntent(hard=HardConstraints(max_total_amount_minor=80_000))
    fake = FakeOpenAI(expected, api_key="unused")
    monkeypatch.setattr(module, "OpenAI", lambda **_: fake)
    interpreter = OpenAIIntentInterpreter(
        api_key=SecretStr("openai-test-key"),
        model="test-model",
    )

    result = interpreter.interpret("Quiero audifonos por maximo 800 dolares con envio")
    interpreter.close()

    assert result == expected
    assert fake.responses.calls[0]["text_format"] is PurchaseIntent
    assert fake.responses.calls[0]["store"] is False
    assert fake.responses.calls[0]["input"].startswith("Quiero audifonos")
    instructions = fake.responses.calls[0]["instructions"]
    for forbidden in ("Bearer", "idempotency", "payment data", "approval evidence"):
        assert forbidden.casefold() in instructions.casefold()
    assert fake.closed
