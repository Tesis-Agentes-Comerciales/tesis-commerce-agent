"""Guard adversarial controls; every SDK attempt uses existing ModelServer, never API."""

import json
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
from pydantic import SecretStr

from commerce_agent.acp import load_product_feed
from commerce_agent.llm import OpenAIConversationRouter
from commerce_agent.llm.delegated_choice import OpenAIDelegatedChooser
from commerce_agent.llm.intent_review import ModelHnpIntentReviewer
from commerce_agent.llm.model_requests import ModelRequestRecorder
from commerce_agent.persistence.model_requests_sqlite import SqliteModelRequests
from commerce_evaluation import exp06_execution as execution
from commerce_evaluation.exp06 import AttemptBudget, Authorization, SpendingBlocked
from tests.support.model_server import ModelServer, failure


def test_authorization_is_required_before_any_network_or_services(tmp_path):
    with pytest.raises(SpendingBlocked, match="authorization"):
        execution.authorize(None, tmp_path / "run")


def test_unsupported_host_rejected_before_claim_or_importing_live_helpers(monkeypatch):
    monkeypatch.setattr(execution, "os", SimpleNamespace(name="nt"))
    with pytest.raises(SpendingBlocked, match="no_claim_consumed"):
        execution.require_live_host()


def test_existing_POSIX_execution_path_is_unchanged(monkeypatch):
    monkeypatch.setattr(execution, "os", SimpleNamespace(name="posix"))
    assert execution.require_live_host() is None


def test_imported_executor_routes_cannot_skip_receipt_claim(tmp_path):
    budget = AttemptBudget(
        Authorization(
            execution.protocol()["model"],
            Decimal("9"),
            execution.ORIGINAL_HASH,
            "smoke",
            "unit-only",
        )
    )
    with pytest.raises(SpendingBlocked, match="claim_missing"):
        execution.ReservedTransport(
            httpx.MockTransport(lambda req: httpx.Response(503)),
            budget,
            execution.Journal(tmp_path / "a.jsonl"),
        )
    with (
        pytest.raises(SpendingBlocked, match="claim_missing"),
        execution.protected_models(budget, execution.Journal(tmp_path / "a.jsonl")),
    ):
        pytest.fail("unclaimed budget must not enter the executor")


def test_infrastructure_transport_itself_rejects_real_network(tmp_path):
    with httpx.HTTPTransport() as inner, pytest.raises(SpendingBlocked, match="MockTransport"):
        execution.ReservedTransport(
            inner,
            AttemptBudget(None, infrastructure_only=True),
            execution.Journal(tmp_path / "a.jsonl"),
        )


@pytest.fixture
def receipt(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(execution, "ROOT", tmp_path)
    monkeypatch.setattr(execution, "git", lambda *args: "unit-sha")
    path = tmp_path / "unit-receipt.json"
    data = {
        "authorized": True,
        "stage": "smoke",
        "executor_sha": "unit-sha",
        "model": execution.protocol()["model"],
        "protocol_sha256": execution.ORIGINAL_HASH,
        "max_usd": "0.89",
        "human_reference": "unit-only-not-real-authorization",
        "authorization_id": "unit-unique",
        "billing_project_confirmed": True,
        "standard_pricing_confirmed": True,
    }
    path.write_text(json.dumps(data))
    return path, data


@pytest.mark.parametrize(
    "field,value",
    [
        ("authorized", False),
        ("stage", "batch"),
        ("model", "gpt-5-mini"),
        ("protocol_sha256", "different"),
        ("max_usd", "10"),
        ("executor_sha", "other"),
        ("standard_pricing_confirmed", False),
    ],
)
def test_changed_authorization_fails_closed(receipt, tmp_path, field, value):
    path, data = receipt
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(SpendingBlocked):
        execution.authorize(path, tmp_path / "run")


def test_claim_prevents_restarting_budget_with_same_receipt(receipt, tmp_path):
    path, _ = receipt
    assert execution.authorize(path, tmp_path / "run").cap == Decimal("0.89")
    with pytest.raises(SpendingBlocked, match="already_claimed"):
        execution.authorize(path, tmp_path / "different-output")


def test_claim_is_shared_between_worktrees(receipt, tmp_path, monkeypatch):
    path, _ = receipt
    common = tmp_path / "common-git"
    monkeypatch.setattr(
        execution,
        "git",
        lambda root, *args: str(common) if args[-1] == "--git-common-dir" else "unit-sha",
    )
    execution.authorize(path, tmp_path / "run")
    monkeypatch.setattr(execution, "ROOT", tmp_path / "another-worktree")
    with pytest.raises(SpendingBlocked, match="already_claimed"):
        execution.authorize(path, tmp_path / "other")


def test_CI_cannot_execute_even_with_receipt(receipt, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(SpendingBlocked, match="outside_CI"):
        execution.authorize(receipt[0], tmp_path / "run")


def sink(tmp_path):
    result = SqliteModelRequests(tmp_path / "ledger.sqlite")
    result.initialize()
    return result


def test_all_three_product_clients_share_protected_factory(tmp_path, monkeypatch):
    model = ModelServer(failure(503))
    model.install(monkeypatch)
    budget = AttemptBudget(None, infrastructure_only=True)
    with execution.protected_models(
        budget, execution.Journal(tmp_path / "attempts.jsonl")
    ) as clients:
        common = {
            "api_key": SecretStr("synthetic-not-real"),
            "model": budget.model,
            "model_requests": sink(tmp_path),
        }
        router = OpenAIConversationRouter(
            **common, feed=load_product_feed(Path(__file__).parents[1] / "fixtures/feed")
        )
        chooser = OpenAIDelegatedChooser(**common)

        # The timer is not used during construction; no HNP operation is executed.
        class Timer:
            def measure(self, stage, usage=None):
                return nullcontext()

        reviewer = ModelHnpIntentReviewer(**common, timer=Timer())
        assert len(clients) == 3
        router.close()
        chooser.close()
        reviewer.close()
    assert not model.requests


def test_SDK_fallback_unrecorded_async_and_alternate_endpoint_rejected(tmp_path, monkeypatch):
    model = ModelServer(failure(503))
    model.install(monkeypatch)
    budget = AttemptBudget(None, infrastructure_only=True)
    with execution.protected_models(budget, execution.Journal(tmp_path / "a.jsonl")):
        with pytest.raises(SpendingBlocked):
            ModelRequestRecorder(None, model=budget.model, reasoning_effort=None).http_client()
        with pytest.raises(SpendingBlocked):
            openai.OpenAI(api_key="synthetic")
        with pytest.raises(SpendingBlocked):
            openai.AsyncOpenAI(api_key="synthetic")
        with (
            httpx.Client(transport=httpx.MockTransport(model.handle)) as rogue,
            pytest.raises(SpendingBlocked),
        ):
            rogue.post("https://api.openai.com/v1/responses", json={})
    assert not model.requests


def test_real_SDK_retries_are_reserved_and_unknown_usage_is_not_free(tmp_path, monkeypatch):
    model = ModelServer(failure(503), failure(503))
    model.install(monkeypatch)
    budget = AttemptBudget(None, infrastructure_only=True)
    budget.cap = Decimal("0.89")
    with execution.protected_models(budget, execution.Journal(tmp_path / "a.jsonl")):
        client = ModelRequestRecorder(
            sink(tmp_path), model=budget.model, reasoning_effort=None
        ).http_client()
        sdk = openai.OpenAI(api_key="synthetic", http_client=client, max_retries=1)
        with pytest.raises(openai.APIStatusError):
            sdk.responses.create(model=budget.model, input="unit transport error", store=False)
        with pytest.raises(openai.APIConnectionError):
            sdk.responses.create(model=budget.model, input="must not send", store=False)
    assert len(model.requests) == budget.attempts == 2
    assert budget.reserved_or_spent == Decimal("0.890")
    events = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert len([e for e in events if e["phase"] == "reserved_before_send"]) == 2


def test_journal_failure_prevents_send(tmp_path, monkeypatch):
    model = ModelServer(failure(503))
    model.install(monkeypatch)
    journal = execution.Journal(tmp_path / "a.jsonl")
    monkeypatch.setattr(journal, "append", lambda event: (_ for _ in ()).throw(OSError("unit")))
    budget = AttemptBudget(None, infrastructure_only=True)
    with execution.protected_models(budget, journal):
        client = ModelRequestRecorder(
            sink(tmp_path), model=budget.model, reasoning_effort=None
        ).http_client()
        sdk = openai.OpenAI(api_key="synthetic", http_client=client, max_retries=1)
        with pytest.raises(openai.APIConnectionError):
            sdk.responses.create(model=budget.model, input="do not send", store=False)
    assert not model.requests


def test_usage_absent_retains_full_reservation(tmp_path):
    budget = AttemptBudget(None, infrastructure_only=True)
    transport = execution.ReservedTransport(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"model": budget.model, "usage": None})
        ),
        budget,
        execution.Journal(tmp_path / "a.jsonl"),
    )
    with httpx.Client(transport=transport) as client:
        client.post(
            "https://api.openai.com/v1/responses", json={"model": budget.model, "store": False}
        )
    assert budget.reserved_or_spent == Decimal("0.445")


def test_factory_failure_cannot_fall_back_to_SDK_default(tmp_path, monkeypatch):
    monkeypatch.setattr(ModelRequestRecorder, "http_client", lambda self: None)
    budget = AttemptBudget(None, infrastructure_only=True)
    with (
        execution.protected_models(budget, execution.Journal(tmp_path / "a.jsonl")),
        pytest.raises(SpendingBlocked, match="no_SDK_fallback"),
    ):
        ModelRequestRecorder(
            sink(tmp_path), model=budget.model, reasoning_effort=None
        ).http_client()


def test_other_product_model_and_transport_replacement_cannot_send(tmp_path, monkeypatch):
    model = ModelServer(failure(503))
    model.install(monkeypatch)
    budget = AttemptBudget(None, infrastructure_only=True)
    with execution.protected_models(budget, execution.Journal(tmp_path / "a.jsonl")):
        with pytest.raises(SpendingBlocked, match="exact_model"):
            ModelRequestRecorder(
                sink(tmp_path), model="another-model", reasoning_effort=None
            ).http_client()
        client = ModelRequestRecorder(
            sink(tmp_path), model=budget.model, reasoning_effort=None
        ).http_client()
        assert client is not None
        client._transport = httpx.MockTransport(model.handle)
        with pytest.raises(SpendingBlocked):
            client.post("https://api.openai.com/v1/responses", json={})
    assert not model.requests


def test_timeout_is_journaled_and_retains_reservation(tmp_path):
    def timeout(request):
        raise httpx.ReadTimeout("synthetic", request=request)

    budget = AttemptBudget(None, infrastructure_only=True)
    journal = execution.Journal(tmp_path / "a.jsonl")
    with (
        httpx.Client(
            transport=execution.ReservedTransport(httpx.MockTransport(timeout), budget, journal)
        ) as client,
        pytest.raises(httpx.ReadTimeout),
    ):
        client.post(
            "https://api.openai.com/v1/responses", json={"model": budget.model, "store": False}
        )
    assert budget.reserved_or_spent == Decimal("0.445")
    assert json.loads(journal.path.read_text().splitlines()[-1])["phase"] == "uncertain"


def test_operator_stop_prevents_next_attempt_without_charge(tmp_path):
    sent = []

    def send(request):
        sent.append(request)
        return httpx.Response(503)

    budget = AttemptBudget(None, infrastructure_only=True)
    journal = execution.Journal(tmp_path / "a.jsonl")
    (tmp_path / "STOP").touch()
    with (
        httpx.Client(
            transport=execution.ReservedTransport(httpx.MockTransport(send), budget, journal)
        ) as client,
        pytest.raises(SpendingBlocked),
    ):
        client.post(
            "https://api.openai.com/v1/responses", json={"model": budget.model, "store": False}
        )
    assert not sent and budget.attempts == 0 and budget.reserved_or_spent == 0


@pytest.mark.parametrize("tier", ["priority", "fast", "flex", "auto", None])
def test_nonstandard_tiers_are_rejected_before_SDK_send(tmp_path, monkeypatch, tier):
    model = ModelServer(failure(503))
    model.install(monkeypatch)
    budget = AttemptBudget(None, infrastructure_only=True)
    with execution.protected_models(budget, execution.Journal(tmp_path / "a.jsonl")):
        client = ModelRequestRecorder(
            sink(tmp_path), model=budget.model, reasoning_effort=None
        ).http_client()
        sdk = openai.OpenAI(api_key="synthetic", http_client=client, max_retries=1)
        with pytest.raises(SpendingBlocked, match="nonstandard"):
            sdk.responses.create(model=budget.model, input="unit", store=False, service_tier=tier)
    assert not model.requests and budget.attempts == 0


def test_raw_paid_transport_cannot_skip_standard_tier(receipt, tmp_path):
    budget = execution.authorize(receipt[0], tmp_path / "out")
    sent = []

    def send(request):
        sent.append(request)
        return httpx.Response(503)

    with (
        httpx.Client(
            transport=execution.ReservedTransport(
                httpx.MockTransport(send), budget, execution.Journal(tmp_path / "a.jsonl")
            )
        ) as client,
        pytest.raises(SpendingBlocked),
    ):
        client.post(
            "https://api.openai.com/v1/responses", json={"model": budget.model, "store": False}
        )
    assert not sent and budget.attempts == 0
