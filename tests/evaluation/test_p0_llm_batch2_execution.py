"""The batch-two cost guard, offline (docs/design/p0-llm-batch2.md §7 and §8).

Every SDK attempt goes to the scripted transport of tests/support/model_server.py: no provider,
no credential, no cost. The guard is batch 1's (`exp06_execution.protected_models`); these tests
show it holds with this batch's budget, its receipt and its three model clients.
"""

import json
import subprocess
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from pydantic import SecretStr

from commerce_agent.acp import load_product_feed
from commerce_agent.domain import PurchaseIntent, rank_candidates
from commerce_agent.llm import ConversationMessage, OpenAIConversationRouter
from commerce_agent.llm.delegated_choice import OpenAIDelegatedChooser
from commerce_agent.llm.intent_review import ModelHnpIntentReviewer
from commerce_agent.llm.model_requests import ModelRequestRecorder
from commerce_agent.persistence.model_requests_sqlite import SqliteModelRequests
from commerce_evaluation import exp06_execution
from commerce_evaluation import p0_llm_batch2_execution as execution
from commerce_evaluation.exp06 import AttemptBudget, SpendingBlocked
from commerce_evaluation.p0_llm_batch2 import protocol, protocol_hash
from tests.support.model_server import Answer, ModelServer, failure, structured

PLAN = protocol()
FEED = Path(__file__).parents[1] / "fixtures/feed"


def as_model(answer: Answer, model: str) -> Answer:
    """A scripted answer reporting `model`, as the guard requires of every response."""

    def respond(body: dict[str, Any]) -> httpx.Response:
        response = answer(body)
        return httpx.Response(response.status_code, json=response.json() | {"model": model})

    return respond


def test_the_executor_pins_the_merged_preregistration_and_spec():
    assert protocol_hash() == execution.FROZEN_PROTOCOL_SHA256
    assert PLAN["spec"]["sha256"] == execution.FROZEN_SPEC_SHA256
    assert execution.preregistration_frozen()


def test_stage_limits_are_the_preregistered_ones():
    smoke, batch = execution.limits("smoke"), execution.limits("batch")
    assert (smoke["episodes"], smoke["http_attempts_max"], smoke["cap_usd"]) == (11, 50, "23.00")
    assert (batch["episodes"], batch["http_attempts_max"], batch["cap_usd"]) == (55, 250, "115.00")
    for bounds in (smoke, batch):
        worst = Decimal(bounds["worst_reserved_usd_per_attempt"])
        assert bounds["http_attempts_max"] * worst == Decimal(bounds["reserved_upper_usd"])
        assert Decimal(bounds["reserved_upper_usd"]) <= Decimal(bounds["cap_usd"])
    with pytest.raises(SpendingBlocked, match="stage"):
        execution.limits("hnp")


def test_the_budget_takes_this_plan_not_batch_one():
    budget = execution.Batch2Budget(None, infrastructure_only=True)
    assert budget.model == PLAN["model"]
    assert budget.worst == Decimal(PLAN["cost"]["worst_reserved_usd_per_attempt"])
    assert (budget.max_attempts, budget.cap, budget.stage) == (50, Decimal("23.00"), "smoke")
    with pytest.raises(SpendingBlocked, match="authorization"):
        execution.Batch2Budget(None)


def test_authorization_is_required_before_any_network_or_services(tmp_path):
    with pytest.raises(SpendingBlocked, match="authorization"):
        execution.authorize(None, tmp_path / "run")


def unit_git(common: Path | None = None, changed: str = ""):
    """`git` of a checkout at the receipt's commit whose product equals `PRODUCT_BASE`."""

    def answer(root, *args):
        if args[0] == "diff":
            assert args[1:4] == ("--name-only", execution.PRODUCT_BASE, "HEAD")
            assert args[5:] == execution.PRODUCT_PATHS
            return changed
        if args[-1] == "--git-common-dir" and common is not None:
            return str(common)
        return "unit-sha"

    return answer


def test_the_run_base_is_main_before_190():
    assert execution.PRODUCT_BASE == "4c60aae9d7e7336ada6242d3d31a754402b07ef3"
    assert {"src", "ui", "uv.lock"} <= set(execution.PRODUCT_PATHS)


@pytest.fixture
def receipt(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(execution, "ROOT", tmp_path)
    monkeypatch.setattr(execution, "git", unit_git())
    path = tmp_path / "unit-receipt.json"
    data = {
        "authorized": True,
        "stage": "smoke",
        "executor_sha": "unit-sha",
        "model": PLAN["model"],
        "protocol_sha256": execution.FROZEN_PROTOCOL_SHA256,
        "max_usd": "23.00",
        "human_reference": "unit-only-not-real-authorization",
        "authorization_id": "unit-unique",
        "billing_project_confirmed": True,
        "standard_pricing_confirmed": True,
    }
    path.write_text(json.dumps(data))
    return path, data


def test_a_smoke_receipt_opens_only_the_smoke_scope(receipt, tmp_path):
    budget = execution.authorize(receipt[0], tmp_path / "run")
    assert (budget.stage, budget.cap, budget.max_attempts) == ("smoke", Decimal("23.00"), 50)
    assert budget.approval is not None and budget.approval["stage"] == "smoke"
    execution.approved(budget)
    claim = Path(budget.approval["claim"])
    assert claim.parent.name == execution.CLAIMS
    assert "unit-unique" not in claim.read_text()


@pytest.mark.parametrize(
    "field,value",
    [
        ("authorized", False),
        ("stage", "batch"),
        ("model", "gpt-5-mini"),
        ("protocol_sha256", "bd813f2541546ddacb1bfd27fdd23f785477ecb95454fdf394b10e56491f2d2b"),
        ("max_usd", "23.01"),
        ("max_usd", "0"),
        ("executor_sha", "other"),
        ("billing_project_confirmed", False),
        ("standard_pricing_confirmed", False),
        ("authorization_id", ""),
        ("human_reference", ""),
    ],
)
def test_changed_authorization_fails_closed(receipt, tmp_path, field, value):
    path, data = receipt
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(SpendingBlocked):
        execution.authorize(path, tmp_path / "run")


def test_an_existing_output_is_refused(receipt, tmp_path):
    (tmp_path / "run").mkdir()
    with pytest.raises(SpendingBlocked, match="output"):
        execution.authorize(receipt[0], tmp_path / "run")


def test_the_claim_prevents_restarting_with_the_same_receipt(receipt, tmp_path):
    execution.authorize(receipt[0], tmp_path / "run")
    with pytest.raises(SpendingBlocked, match="already_claimed"):
        execution.authorize(receipt[0], tmp_path / "other")


def test_the_claim_is_shared_between_worktrees(receipt, tmp_path, monkeypatch):
    common = tmp_path / "common-git"
    monkeypatch.setattr(execution, "git", unit_git(common))
    execution.authorize(receipt[0], tmp_path / "run")
    monkeypatch.setattr(execution, "ROOT", tmp_path / "another-worktree")
    with pytest.raises(SpendingBlocked, match="already_claimed"):
        execution.authorize(receipt[0], tmp_path / "other")


@pytest.mark.parametrize("variable", ["GITHUB_ACTIONS", "CI"])
def test_CI_never_executes_even_with_a_receipt(receipt, tmp_path, monkeypatch, variable):
    monkeypatch.setenv(variable, "true")
    with pytest.raises(SpendingBlocked, match="outside_CI"):
        execution.authorize(receipt[0], tmp_path / "run")


def test_a_changed_preregistration_blocks_the_receipt(receipt, tmp_path, monkeypatch):
    monkeypatch.setattr(execution, "FROZEN_PROTOCOL_SHA256", "0" * 64)
    with pytest.raises(SpendingBlocked, match="preregistration_changed"):
        execution.authorize(receipt[0], tmp_path / "run")


def test_a_product_change_blocks_the_receipt_before_the_claim(receipt, tmp_path, monkeypatch):
    """A checkout with #190 (or any product change over 4c60aae) cannot open a paid run."""
    changed = "src/commerce_agent/llm/conversation_router.py"
    monkeypatch.setattr(execution, "git", unit_git(changed=changed))
    with pytest.raises(SpendingBlocked, match="product_code_differs"):
        execution.authorize(receipt[0], tmp_path / "run")
    assert not list(tmp_path.rglob(execution.CLAIMS))


def test_a_product_change_after_the_claim_cannot_send(receipt, tmp_path, monkeypatch):
    budget = execution.authorize(receipt[0], tmp_path / "run")
    monkeypatch.setattr(execution, "git", unit_git(changed="ui/src/lib/agent-contract.ts"))
    with pytest.raises(SpendingBlocked, match="scope_changed"):
        execution.approved(budget)


@pytest.fixture
def batch_receipt(receipt):
    path, data = receipt
    data |= {"stage": "batch", "max_usd": "115.00", "authorization_id": "unit-batch"}
    path.write_text(json.dumps(data))
    return path, data


def test_a_batch_receipt_opens_only_the_batch_scope(batch_receipt, tmp_path):
    budget = execution.authorize(batch_receipt[0], tmp_path / "run", stage="batch")
    assert (budget.stage, budget.cap, budget.max_attempts) == ("batch", Decimal("115.00"), 250)
    execution.approved(budget)
    with pytest.raises(SpendingBlocked):
        execution.authorize(batch_receipt[0], tmp_path / "again", stage="smoke")


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_attempts", 251),
        ("cap", Decimal("115.01")),
        ("stage", "smoke"),
        ("worst", Decimal("0.001")),
        ("model", "gpt-5-mini"),
    ],
)
def test_a_budget_changed_after_the_claim_cannot_send(batch_receipt, tmp_path, field, value):
    budget = execution.authorize(batch_receipt[0], tmp_path / "run", stage="batch")
    setattr(budget, field, value)
    with pytest.raises(SpendingBlocked, match="scope_changed"):
        execution.approved(budget)


def test_an_unclaimed_or_foreign_budget_cannot_enter_the_guard(tmp_path):
    journal = exp06_execution.Journal(tmp_path / "a.jsonl")
    with (
        pytest.raises(SpendingBlocked, match="batch2_budget_required"),
        execution.protected_models(AttemptBudget(None, infrastructure_only=True), journal),
    ):
        pytest.fail("a batch-1 budget must not enter this executor")
    unclaimed = execution.Batch2Budget(None, infrastructure_only=True)
    unclaimed.infrastructure_only = False
    with (
        pytest.raises(SpendingBlocked, match="claim_missing"),
        execution.protected_models(unclaimed, journal),
    ):
        pytest.fail("an unclaimed paid budget must not enter the executor")


def test_the_batch_one_approval_check_is_restored_after_the_guard(tmp_path):
    original = exp06_execution.approved_budget
    budget = execution.Batch2Budget(None, infrastructure_only=True)
    with execution.protected_models(budget, exp06_execution.Journal(tmp_path / "a.jsonl")):
        assert exp06_execution.approved_budget is execution.approved
    assert exp06_execution.approved_budget is original


def sink(tmp_path: Path) -> SqliteModelRequests:
    result = SqliteModelRequests(tmp_path / "ledger.sqlite")
    result.initialize()
    return result


class Timer:
    def measure(self, stage, usage=None):
        return nullcontext()


def test_the_three_agent_model_clients_send_only_through_the_guard(tmp_path, monkeypatch):
    """Spec §8: the router, the delegated chooser and the intent reviewer each send one request,
    and each is reserved, journaled and counted in the episode before it leaves."""
    budget = execution.Batch2Budget(None, infrastructure_only=True)
    model = ModelServer(
        as_model(
            structured(lambda body: {"action": "clarify", "assistant_message": "¿Cuál?"}),
            budget.model,
        ),
        as_model(
            structured(
                lambda body: {
                    "action": "purchase",
                    "assistant_message": "Elijo el primero.",
                    "selected_variant_id": body["text"]["format"]["schema"]["properties"][
                        "selected_variant_id"
                    ]["enum"][0],
                }
            ),
            budget.model,
        ),
        as_model(
            structured(lambda body: {"decision": "confirmed", "reason": "matches_request"}),
            budget.model,
        ),
    )
    model.install(monkeypatch)
    journal = exp06_execution.Journal(tmp_path / "attempts.jsonl")
    feed = load_product_feed(FEED)
    intent = PurchaseIntent.model_validate({"hard": {"currency": "COP"}})
    shortlist = rank_candidates(feed, intent).ranked[:3]
    with execution.protected_models(budget, journal) as clients:
        common = {
            "api_key": SecretStr("synthetic-not-real"),
            "model": budget.model,
            "model_requests": sink(tmp_path),
        }
        router = OpenAIConversationRouter(**common, feed=feed)
        chooser = OpenAIDelegatedChooser(**common)
        reviewer = ModelHnpIntentReviewer(**common, timer=Timer())
        assert len(clients) == 3
        budget.begin_episode(3)
        decision = router.route((ConversationMessage(role="user", content="Quiero algo"),))
        choice = chooser.choose(intent, shortlist, "Prefiero la opción más barata.")
        review = reviewer.review(shortlist, intent, None, choice.variant_id)
        router.close()
        chooser.close()
        reviewer.close()
    assert decision.action == "clarify" and review.decision == "confirmed"
    assert len(model.requests) == budget.attempts == budget.episode_attempts == 3
    assert all(body["service_tier"] == "default" for body in model.bodies())
    events = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert [e["phase"] for e in events] == ["reserved_before_send", "returned"] * 3
    assert all(e["settled"] for e in events if e["phase"] == "returned")


def test_nothing_is_sent_before_an_episode_opens(tmp_path, monkeypatch):
    model = ModelServer(failure(503))
    model.install(monkeypatch)
    budget = execution.Batch2Budget(None, infrastructure_only=True)
    with execution.protected_models(budget, exp06_execution.Journal(tmp_path / "a.jsonl")):
        client = ModelRequestRecorder(
            sink(tmp_path), model=budget.model, reasoning_effort=None
        ).http_client()
        sdk = openai.OpenAI(api_key="synthetic", http_client=client, max_retries=0)
        with pytest.raises(openai.APIConnectionError):
            sdk.responses.create(model=budget.model, input="no episode", store=False)
    assert not model.requests and budget.attempts == 0


@pytest.mark.parametrize("limit", [None, 0, 7])
def test_an_episode_limit_outside_the_plan_is_refused(limit):
    with pytest.raises(SpendingBlocked, match="episode_limit"):
        execution.Batch2Budget(None, infrastructure_only=True).begin_episode(limit)


def test_the_cell_limit_stops_the_episode_before_send(tmp_path, monkeypatch):
    """CT21-HNP in B0-HNP has 2 attempts: the selector and its one SDK retry. A third request
    in the same episode is refused before it leaves and latches that client, as batch 1 does;
    the next episode's Agent builds new clients and sends again."""
    model = ModelServer(*(failure(503) for _ in range(4)))
    model.install(monkeypatch)
    budget = execution.Batch2Budget(None, infrastructure_only=True)

    def sdk() -> openai.OpenAI:
        client = ModelRequestRecorder(
            sink(tmp_path), model=budget.model, reasoning_effort=None
        ).http_client()
        return openai.OpenAI(api_key="synthetic", http_client=client, max_retries=1)

    with execution.protected_models(budget, exp06_execution.Journal(tmp_path / "a.jsonl")):
        first = sdk()
        budget.begin_episode(2)
        with pytest.raises(openai.APIStatusError):
            first.responses.create(model=budget.model, input="two attempts", store=False)
        with pytest.raises(openai.APIConnectionError):
            first.responses.create(model=budget.model, input="third attempt", store=False)
        assert len(model.requests) == budget.attempts == 2
        budget.begin_episode(2)
        with pytest.raises(openai.APIConnectionError):
            first.responses.create(model=budget.model, input="latched", store=False)
        assert len(model.requests) == 2
        with pytest.raises(openai.APIStatusError):
            sdk().responses.create(model=budget.model, input="next episode", store=False)
    assert len(model.requests) == budget.attempts == 4
    assert budget.reserved_or_spent == 4 * budget.worst


def test_the_stage_cap_stops_before_send(tmp_path, monkeypatch):
    model = ModelServer(failure(503))
    model.install(monkeypatch)
    budget = execution.Batch2Budget(None, infrastructure_only=True)
    budget.cap = budget.worst - Decimal("0.001")
    with execution.protected_models(budget, exp06_execution.Journal(tmp_path / "a.jsonl")):
        client = ModelRequestRecorder(
            sink(tmp_path), model=budget.model, reasoning_effort=None
        ).http_client()
        sdk = openai.OpenAI(api_key="synthetic", http_client=client, max_retries=0)
        budget.begin_episode(6)
        with pytest.raises(openai.APIConnectionError):
            sdk.responses.create(model=budget.model, input="over the cap", store=False)
    assert not model.requests and budget.reserved_or_spent == 0


def test_the_plan_cli_never_reaches_the_executor_without_execute(monkeypatch, capsys):
    from commerce_evaluation import p0_llm_batch2

    monkeypatch.setattr(execution, "execute", lambda *args: pytest.fail("executor reached"))
    p0_llm_batch2.main(["--stage", "batch"])
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 0


def test_execute_without_a_receipt_stops_before_any_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-unit-never-used")
    monkeypatch.setattr(exp06_execution, "check_account", lambda: pytest.fail("account read"))
    with pytest.raises(SpendingBlocked, match="authorization"):
        execution.execute(None, tmp_path / "run", "smoke")


# Amendment A1


AMENDMENT = json.loads(
    (execution.REPOSITORY / "evaluation/scenarios/p0-llm-batch2-a1.json").read_text()
)
# What the run executes besides the product: the executor, its live episode and its controls.
EXECUTOR = (
    "evaluation/src/commerce_evaluation/p0_llm_batch2.py",
    "evaluation/src/commerce_evaluation/p0_llm_batch2_execution.py",
    "tests/integration/p0_llm_batch2_live.py",
    "tests/integration/test_p0_llm_batch2_controls.py",
    "tests/integration/live_merchant_control.py",
)


def test_amendment_a1_fixes_the_run_ref_and_keeps_the_frozen_bytes():
    assert AMENDMENT["id"] == "A1"
    assert AMENDMENT["amends"]["preregistration"]["sha256"] == execution.FROZEN_PROTOCOL_SHA256
    assert AMENDMENT["amends"]["spec"]["sha256"] == execution.FROZEN_SPEC_SHA256
    # The preregistration keeps its bytes, run_ref null included: A1 lives in its own file.
    assert protocol_hash() == execution.FROZEN_PROTOCOL_SHA256 and PLAN["run_ref"] is None
    assert PLAN["amendments"]["allowed_before_first_live_call"][0].startswith("A1: fix run_ref")
    assert AMENDMENT["agent_base"] == PLAN["agent_base"]
    base = AMENDMENT["product_base"]
    assert base["commit"] == execution.PRODUCT_BASE
    assert base["paths"] == list(execution.PRODUCT_PATHS)
    run_ref = AMENDMENT["run_ref"]
    assert run_ref["tag"] == "p0-llm-batch2-run" and len(run_ref["commit"]) == 40
    assert AMENDMENT["controls"]["on"] == run_ref["commit"]
    included = [item["pr"] for item in AMENDMENT["included"]]
    assert included == [181, 157, 182, 185, 184, 183, 187, 188, 186]
    assert [item["pr"] for item in AMENDMENT["included"] if item["product_code"]] == [185, 187]
    assert AMENDMENT["included"][-1]["merge"] == execution.PRODUCT_BASE
    [excluded] = AMENDMENT["excluded"]
    assert excluded["pr"] == 190 and "prompt" in excluded["changes"]


def in_clone(commit: str) -> bool:
    found = subprocess.run(
        ["git", "-C", str(execution.REPOSITORY), "cat-file", "-e", commit + "^{commit}"],
        capture_output=True,
    )
    return found.returncode == 0


def test_the_run_ref_is_the_product_base_plus_this_executor():
    """Where the clone has the run_ref: its product is the base's, and the executor this
    branch carries is the one it runs. A shallow checkout without it skips."""
    commit = AMENDMENT["run_ref"]["commit"]
    if not in_clone(commit) or not in_clone(execution.PRODUCT_BASE):
        pytest.skip("run_ref or product_base not in this clone")
    repository = str(execution.REPOSITORY)
    product = ["git", "-C", repository, "diff", "--quiet", execution.PRODUCT_BASE, commit, "--"]
    assert subprocess.run([*product, *execution.PRODUCT_PATHS]).returncode == 0
    executor = ["git", "-C", repository, "diff", "--quiet", commit, "HEAD", "--", *EXECUTOR]
    assert subprocess.run(executor).returncode == 0
