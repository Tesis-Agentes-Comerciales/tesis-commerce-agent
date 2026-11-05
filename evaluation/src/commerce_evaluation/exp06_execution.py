"""Manual-only HP smoke. Default/CI never enables a paid provider."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from contextlib import ExitStack, closing, contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch
from xml.etree import ElementTree as ET

import httpx
import openai
from openai.resources.responses.responses import Responses

from commerce_agent.llm.model_requests import ModelRequestRecorder
from commerce_evaluation.exp06 import (
    ROOT,
    AttemptBudget,
    Authorization,
    SpendingBlocked,
    classify,
    messages,
    protocol,
    protocol_hash,
    review_template,
)
from commerce_evaluation.redaction import redact

ORIGINAL_HASH = "bd813f2541546ddacb1bfd27fdd23f785477ecb95454fdf394b10e56491f2d2b"
CASES = ("UX01", "UX11", "UX12")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(json.dumps(value, indent=2, ensure_ascii=True)) + "\n", encoding="utf-8")


def plan() -> dict[str, Any]:
    return {
        "model": protocol()["model"],
        "mode": "HP",
        "service_tier": "default",
        "cases": list(CASES),
        "repetitions": 1,
        "protocol_sha256": protocol_hash(),
        "theoretical_upper_usd": "6.408",
        "reserved_upper_usd": "8.010",
        "proposed_cap_usd": "9.00",
        "authorized": False,
        "context": protocol()["context"],
        "semantic_status": "not_executed",
    }


class SmokeBudget(AttemptBudget):
    approval: dict[str, Any] | None = None


def approved_budget(budget: AttemptBudget) -> None:
    if budget.infrastructure_only:
        return  # Each transport must still prove it is MockTransport.
    if not isinstance(budget, SmokeBudget) or budget.approval is None:
        raise SpendingBlocked("manual_receipt_claim_missing")
    claim = json.loads(Path(budget.approval["claim"]).read_text(encoding="utf-8"))
    if (
        claim["receipt_sha256"] != budget.approval["receipt_sha256"]
        or Decimal(claim["cap_usd"]) != budget.cap
        or budget.max_attempts != 18
        or budget.cap > Decimal("9")
        or protocol_hash() != ORIGINAL_HASH
        or git(ROOT, "rev-parse", "HEAD") != budget.approval["executor_sha"]
    ):
        raise SpendingBlocked("manual_claim_or_executed_scope_changed")


def authorize(receipt: Path | None, output: Path) -> AttemptBudget:
    # Before environment credentials, services, imports of POSIX helpers or any request.
    if receipt is None:
        raise SpendingBlocked("manual_explicit_authorization_required_outside_CI")
    if protocol_hash() != ORIGINAL_HASH:
        raise SpendingBlocked("historical_protocol_changed")
    data = json.loads(receipt.read_text(encoding="utf-8"))
    if os.environ.get("GITHUB_ACTIONS") == "true":
        from commerce_evaluation.exp06_actions import matches_dispatch

        if not matches_dispatch(data):
            raise SpendingBlocked(
                "manual_explicit_authorization_required_outside_CI_or_approved_dispatch"
            )
    if (
        data.get("authorized") is not True
        or data.get("stage") != "smoke"
        or data.get("executor_sha") != git(ROOT, "rev-parse", "HEAD")
        or data.get("billing_project_confirmed") is not True
        or data.get("standard_pricing_confirmed") is not True
        or not data.get("authorization_id")
        or output.exists()
    ):
        raise SpendingBlocked("receipt_scope_or_output_invalid")
    auth = Authorization(
        data["model"],
        Decimal(data["max_usd"]),
        data["protocol_sha256"],
        data["stage"],
        data["human_reference"],
    )
    budget = SmokeBudget(auth)
    if budget.cap > Decimal("9"):
        raise SpendingBlocked("smoke_cap_exceeds_registered_proposal")
    common = Path(git(ROOT, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = ROOT / common
    claim = (
        common
        / "exp06-claims"
        / (hashlib.sha256(str(data["authorization_id"]).encode()).hexdigest() + ".json")
    )
    claim.parent.mkdir(parents=True, exist_ok=True)
    try:
        with claim.open("x", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                        "output": str(output.resolve()),
                        "cap_usd": str(budget.cap),
                    }
                )
            )
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise SpendingBlocked("authorization_already_claimed_no_automatic_resume") from error
    budget.approval = {
        "claim": str(claim),
        "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        "executor_sha": data["executor_sha"],
        "authorization_id_sha256": hashlib.sha256(
            str(data["authorization_id"]).encode()
        ).hexdigest(),
    }
    return budget


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.case_id: str | None = None

    def append(self, event: dict[str, Any]) -> None:
        # No headers, credentials or raw exception messages. Failure closes the send path.
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"case_id": self.case_id, **event}, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


class ReservedTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, budget: AttemptBudget, journal: Journal):
        approved_budget(budget)
        if budget.infrastructure_only and not isinstance(inner, httpx.MockTransport):
            raise SpendingBlocked("infrastructure_requires_MockTransport")
        self.inner, self.budget, self.journal = inner, budget, journal
        self.stopped = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.stopped or (self.journal.path.parent / "STOP").exists():
            self.stopped = True
            raise SpendingBlocked("latched_or_operator_stop")
        try:
            body = json.loads(request.content)
            if not self.budget.infrastructure_only and body.get("service_tier") != "default":
                raise SpendingBlocked("standard_service_tier_required_before_send")
            self.budget.before(request)
            number = self.budget.attempts
            self.journal.append(
                {
                    "phase": "reserved_before_send",
                    "attempt": number,
                    "at_ns": time.time_ns(),
                    "service_tier": body.get("service_tier"),
                    **self.budget.events[-1],
                }
            )
        except Exception:
            self.stopped = True
            raise
        started = time.perf_counter()
        try:
            response = self.inner.handle_request(request)
            response.request = request
            response_tier = None
            if response.status_code == 200:
                response.read()
                response_tier = response.json().get("service_tier")
                if not self.budget.infrastructure_only and response_tier != "default":
                    raise SpendingBlocked("response_service_tier_unknown_or_nonstandard")
            self.budget.after(response)
            self.journal.append(
                {
                    "phase": "returned",
                    "attempt": number,
                    "http_status": response.status_code,
                    "response_service_tier": response_tier,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "reserved_or_spent_usd": str(self.budget.reserved_or_spent),
                    "settled": self.budget.events[-1].get("attempt_settled", False),
                    "known_usage_upper_cost_usd": self.budget.events[-1].get(
                        "usage_upper_cost_usd"
                    ),
                    "provider_request_id": response.headers.get("x-request-id"),
                }
            )
            return response
        except Exception as error:
            if isinstance(error, SpendingBlocked):
                self.stopped = True
            self.journal.append(
                {
                    "phase": "uncertain",
                    "attempt": number,
                    "error_kind": type(error).__name__,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                }
            )
            raise

    def close(self) -> None:
        self.inner.close()


@contextmanager
def protected_models(budget: AttemptBudget, journal: Journal):
    """Dedicated process only: install before runtime and retain through its shutdown."""
    approved_budget(budget)
    factory, initialize = ModelRequestRecorder.http_client, openai.OpenAI.__init__
    send, async_send = httpx.Client.send, httpx.AsyncClient.send
    guarded: set[httpx.Client] = set()
    parse_response, create_response = Responses.parse, Responses.create

    def standard_parse(self, *args, **kwargs):
        if kwargs.get("service_tier", "default") != "default":
            raise SpendingBlocked("nonstandard_service_tier_forbidden")
        return parse_response(self, *args, **(kwargs | {"service_tier": "default"}))

    def standard_create(self, *args, **kwargs):
        if kwargs.get("service_tier", "default") != "default":
            raise SpendingBlocked("nonstandard_service_tier_forbidden")
        return create_response(self, *args, **(kwargs | {"service_tier": "default"}))

    def client_for(recorder):
        if recorder._model != budget.model or recorder._sink is None:
            raise SpendingBlocked("exact_model_and_persistent_recorder_required")
        client = factory(recorder)
        if client is None:
            raise SpendingBlocked("recorder_client_unavailable_no_SDK_fallback")
        if any(value is not None for value in client._mounts.values()):
            client.close()
            raise SpendingBlocked("alternate_or_proxy_transport_forbidden")
        if budget.infrastructure_only and not isinstance(client._transport, httpx.MockTransport):
            client.close()
            raise SpendingBlocked("infrastructure_requires_MockTransport")
        client._transport = ReservedTransport(client._transport, budget, journal)
        guarded.add(client)
        return client

    def sdk_initialize(self, *args, **kwargs):
        if (
            kwargs.get("http_client") not in guarded
            or args
            or kwargs.get("base_url") not in {None, "https://api.openai.com/v1"}
            or os.environ.get("OPENAI_BASE_URL")
        ):
            raise SpendingBlocked("unregistered_SDK_client_or_endpoint")
        initialize(self, **kwargs)

    def boundary(self, request, *args, **kwargs):
        if request.url.host not in {"127.0.0.1", "localhost", "::1"} and (
            self not in guarded
            or not isinstance(self._transport, ReservedTransport)
            or any(t is not None for t in self._mounts.values())
        ):
            raise SpendingBlocked("outbound_HTTP_requires_registered_model_transport")
        return send(self, request, *args, **kwargs)

    async def async_boundary(self, request, *args, **kwargs):
        if request.url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise SpendingBlocked("external_async_HTTP_forbidden")
        return await async_send(self, request, *args, **kwargs)

    def unavailable(*args, **kwargs):
        raise SpendingBlocked("async_or_Azure_model_client_forbidden")

    with ExitStack() as changes:
        changes.enter_context(patch.object(ModelRequestRecorder, "http_client", client_for))
        changes.enter_context(patch.object(openai.OpenAI, "__init__", sdk_initialize))
        changes.enter_context(patch.object(Responses, "parse", standard_parse))
        changes.enter_context(patch.object(Responses, "create", standard_create))
        changes.enter_context(patch.object(openai.AsyncOpenAI, "__init__", unavailable))
        changes.enter_context(patch.object(openai.AzureOpenAI, "__init__", unavailable))
        changes.enter_context(patch.object(openai.AsyncAzureOpenAI, "__init__", unavailable))
        changes.enter_context(patch.object(httpx.Client, "send", boundary))
        changes.enter_context(patch.object(httpx.AsyncClient, "send", async_boundary))
        try:
            yield guarded
        finally:
            for client in guarded:
                client.close()


def check_account() -> dict[str, Any]:
    """Non-inference GET only; never infer billing quota from model visibility."""
    result: dict[str, Any] = {
        "credential_present": bool(os.environ.get("OPENAI_API_KEY")),
        "project_configured": bool(os.environ.get("OPENAI_PROJECT_ID")),
        "organization_configured": bool(os.environ.get("OPENAI_ORG_ID")),
        "model": protocol()["model"],
        "billing_limit": "not_verified",
        "Responses_permission_and_quota": "not_verified",
        "inference_calls": 0,
    }
    if not result["credential_present"]:
        result["status"] = "blocked_missing_environment_credential"
        return result
    headers = {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}
    for variable, header in [
        ("OPENAI_PROJECT_ID", "OpenAI-Project"),
        ("OPENAI_ORG_ID", "OpenAI-Organization"),
    ]:
        if os.environ.get(variable):
            headers[header] = os.environ[variable]
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = client.get(
                "https://api.openai.com/v1/models/" + protocol()["model"], headers=headers
            )
        result["http_status"] = response.status_code
        result["status"] = (
            "model_visible"
            if response.status_code == 200 and response.json().get("id") == protocol()["model"]
            else "blocked"
        )
    except (httpx.HTTPError, ValueError) as error:
        result.update(status="unknown", error_kind=type(error).__name__)
    return result


def sources() -> dict[str, Any]:
    if protocol_hash() != ORIGINAL_HASH:
        raise SpendingBlocked("historical_protocol_changed")
    result = {}
    for role, directory, expected in [
        ("agent", ROOT, git(ROOT, "rev-parse", "HEAD")),
        ("merchant", Path(os.environ["MERCHANT_REPO"]), protocol()["merchant_sha"]),
        ("psp", Path(os.environ["PAYMENT_REPO"]), protocol()["payment_sha"]),
    ]:
        sha = git(directory, "rev-parse", "HEAD")
        if sha != expected or git(directory, "status", "--porcelain"):
            raise SpendingBlocked("unclean_or_unpinned_source:" + role)
        result[role] = {
            "sha": sha,
            "uv_lock_sha256": hashlib.sha256((directory / "uv.lock").read_bytes()).hexdigest(),
        }
    return result


def require_live_host() -> None:
    if os.name != "posix":
        raise SpendingBlocked("manual_live_stack_requires_prepared_Linux_host_no_claim_consumed")


def run_stack(output: Path, budget: AttemptBudget, key: str) -> dict[str, Any]:
    # Repository-only manual runner: reuse dev harness instead of another service stack.
    require_live_host()
    import secrets

    import pytest
    from pydantic import SecretStr

    from commerce_agent.api import create_app
    from commerce_evaluation.conversation import (
        DETAILS,
        ConversationHTTPError,
        ConversationTransportError,
        request_json,
    )
    from commerce_evaluation.governance_pilot import AuthClient
    from tests.integration.exp01_support import sqlite_snapshot
    from tests.integration.live_browser import AgentServer, ExplicitSettings, NodeServer
    from tests.integration.live_purchase import PurchaseLane
    from tests.integration.live_stack import AGENT, LiveStack, clean_environment, free_port
    from tests.integration.test_exp02_commercial import local_effects, observe
    from tests.integration.test_live_b2_d2_authorization import settings_for

    approved_budget(budget)
    provenance = sources()
    output.mkdir(parents=True, exist_ok=False)
    public = output / "evidence"
    public.mkdir()
    private_workspace = tempfile.TemporaryDirectory(prefix="exp06-private-")
    private = Path(private_workspace.name)
    journal = Journal(public / "attempts.jsonl")
    summary: dict[str, Any] = {
        "plan": plan(),
        "sources": provenance,
        "cases": {},
        "cleanup_verified": False,
        "infrastructure_only": budget.infrastructure_only,
    }
    (private / "stack").mkdir()
    write(
        public / "execution-manifest.json",
        {
            "plan": plan(),
            "sources": provenance,
            "effective_cap_usd": str(budget.cap),
            "execution_authorized": not budget.infrastructure_only,
            "approval": {
                k: v for k, v in (getattr(budget, "approval", None) or {}).items() if k != "claim"
            },
            "created_at_ns": time.time_ns(),
        },
    )
    stack, lane = LiveStack(private / "stack"), None
    try:
        stack.start()
        lane = PurchaseLane(stack, "b0")
        lane.start()
        lane.proxy.routes |= {("POST", "/checkout_sessions/*/cancel")}
        episodes = {}
        # All three frozen before any router exists or provider request can be sent.
        for case_id in CASES:
            episode = lane.episode("exp06-" + case_id)
            episodes[case_id] = episode
            write(
                public / (case_id + "-frozen.json"),
                {
                    "protocol_sha256": ORIGINAL_HASH,
                    "messages": messages(case_id),
                    "oracle": protocol()["cases"][case_id],
                    "context": protocol()["context"],
                    "catalog": {
                        p.name: p.read_text(encoding="utf-8")
                        for p in sorted(episode.feed.iterdir())
                        if p.is_file()
                    },
                    "run_id": episode.run_id,
                    "actor_id": episode.actor_id,
                    "frozen_at_ns": time.time_ns(),
                    "sources": provenance,
                },
            )
        frozen_files = list(public.glob("*-frozen.json"))
        product_hashes = {
            hashlib.sha256((e.feed / "products.jsonl").read_bytes()).hexdigest()
            for e in episodes.values()
        }
        if len(frozen_files) != 3 or len(product_hashes) != 1:
            raise SpendingBlocked("full_catalog_or_episode_equivalence_not_demonstrated")
        summary["catalog_products_sha256"] = next(iter(product_hashes))
        with protected_models(budget, journal):
            for case_id in CASES:
                episode = episodes[case_id]
                directory = private / case_id
                directory.mkdir()
                port = free_port()
                origin = f"http://127.0.0.1:{port}"
                auth_server = NodeServer(
                    directory,
                    "auth",
                    AGENT / "auth",
                    ("server.js",),
                    clean_environment()
                    | {
                        "BETTER_AUTH_URL": origin,
                        "BETTER_AUTH_SECRET": secrets.token_urlsafe(32),
                        "AUTH_DATABASE_PATH": str(directory / "auth.sqlite"),
                        "PORT": str(port),
                    },
                    port,
                    "/api/auth/jwks",
                )
                server = None
                row: dict[str, Any] = {"status": "error", "commercial_truth": "unknown"}
                summary["cases"][case_id] = row
                before, psp = observe(lane, episode), sqlite_snapshot(lane.root / "payments.sqlite")
                journal.case_id = case_id
                budget.begin_episode()
                try:
                    with pytest.MonkeyPatch.context() as environment:
                        base = settings_for(lane, episode, directory, environment)
                    settings = ExplicitSettings(
                        **(
                            base.model_dump()
                            | {
                                "agent_auth_issuer": origin,
                                "agent_hnp_enabled": False,
                                "agent_hnp_reviewer": "off",
                                "openai_api_key": SecretStr(key),
                                "openai_model": budget.model,
                                "openai_reasoning_effort": "minimal",
                                "openai_timeout_seconds": 30,
                            }
                        )
                    )
                    auth_server.start()
                    auth = AuthClient(origin)
                    auth.signup()
                    row["auth_after_signup"] = sqlite_snapshot(directory / "auth.sqlite")
                    server = AgentServer(create_app(settings=settings))
                    server.start()
                    started = time.perf_counter()
                    status, body = 0, {}
                    try:
                        body = request_json(
                            server.origin,
                            "/conversation/turn",
                            {
                                "turn_id": "exp06-" + case_id,
                                "messages": messages(case_id),
                                "details": DETAILS,
                            },
                            auth=auth,
                        )
                        status = 200
                    except ConversationHTTPError as error:
                        status, body = error.status_code, error.body
                    except ConversationTransportError:
                        row["transport_error"] = True
                    # Join/stop before observing: a timed-out HTTP caller may leave work running.
                    server.stop()
                    # Keep the boundary installed until the turn has stopped.
                    server.thread.join()
                    server = None
                    row.update(
                        http_status=status,
                        body=body,
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )
                    with closing(sqlite3.connect(settings.agent_database_path)) as db:
                        db.row_factory = sqlite3.Row
                        ledger = [
                            dict(r)
                            for r in db.execute("SELECT * FROM model_requests ORDER BY sequence")
                        ]
                        purchases = db.execute("SELECT count(*) FROM purchases").fetchone()[0]
                    after, psp_after = (
                        observe(lane, episode),
                        sqlite_snapshot(lane.root / "payments.sqlite"),
                    )
                    counts = local_effects(settings.agent_database_path) | {"purchases": purchases}
                    effects = counts | {
                        "checkouts": len(after["checkouts"]),
                        "psp_changes": int(psp_after != psp),
                    }
                    row.update(
                        model_requests=ledger,
                        effects=effects,
                        merchant_before=before,
                        merchant_after=after,
                        psp_before=psp,
                        psp_after=psp_after,
                        auth_after_requests=sqlite_snapshot(directory / "auth.sqlite"),
                    )
                    complete = (
                        bool(ledger)
                        and sum(1 + r["retries"] for r in ledger) == budget.episode_attempts
                    )
                    usage_known = all(
                        r["outcome"] == "ok"
                        and r["input_tokens"] is not None
                        and r["output_tokens"] is not None
                        and r["response_model"] == budget.model
                        for r in ledger
                    )
                    row.update(
                        classify(
                            case_id,
                            status=status,
                            body=body,
                            effects=effects,
                            ledger_complete=complete
                            and (usage_known or budget.infrastructure_only),
                            simulated=budget.infrastructure_only,
                        )
                    )
                    row["ledger_complete"] = complete
                    row["usage_known"] = usage_known
                    row["effects_verified_zero"] = (
                        all(v == 0 for v in effects.values()) and before == after
                    )
                    if (
                        not row["effects_verified_zero"]
                        or not complete
                        or (not usage_known and not budget.infrastructure_only)
                    ):
                        row["status"] = "failed" if any(effects.values()) else "error"
                    row["failure_origin"] = (
                        "independent_commercial_effect"
                        if any(effects.values())
                        else "evidence_or_harness"
                        if not complete
                        else "provider_or_model_request"
                        if any(r["outcome"] == "error" for r in ledger)
                        else "model_structured_action"
                        if row["status"] == "failed"
                        else "agent_pipeline"
                        if row["status"] == "error"
                        else "pending_human_semantic_review"
                    )
                    write(public / (case_id + "-review.json"), review_template(case_id))
                finally:
                    if server is not None:
                        server.stop()
                        server.thread.join()
                    auth_server.stop()
                    observed = observe(lane, episode)
                    row["before_cleanup"] = observed
                    for checkout in observed["checkouts"]:
                        if checkout[1] != "canceled":
                            lane.rpc(
                                "cancel_fixture",
                                bearer=episode.bearer.get_secret_value(),
                                checkout_id=checkout[0],
                                key="exp06-cleanup-" + checkout[0],
                            )
                    final = observe(lane, episode)
                    row["after_cleanup"] = final
                    row["cleanup_verified"] = (
                        all(c[1] == "canceled" for c in final["checkouts"])
                        and auth_server.process is None
                    )
                    row["psp_after_cleanup"] = sqlite_snapshot(lane.root / "payments.sqlite")
                    write(public / (case_id + "-observed.json"), row)
                if not budget.infrastructure_only and row["status"] != "pending_review":
                    break
    finally:
        for case_id in CASES:
            summary["cases"].setdefault(
                case_id, {"status": "not_executed", "reason": "earlier_stop"}
            )
        try:
            if lane is not None:
                lane.close()
        finally:
            stack.close()
        summary["services_stopped"] = lane is None or (
            lane.merchant.process is None and lane.payment.process is None
        )
        if (stack.root / "evidence.jsonl").exists():
            (public / "evidence.jsonl").write_bytes((stack.root / "evidence.jsonl").read_bytes())
        for index, logfile in enumerate(sorted(private.rglob("*.log"))):
            (public / f"service-{index}.log").write_text(
                redact(logfile.read_text(encoding="utf-8", errors="replace")), encoding="utf-8"
            )
        shutdown = (
            [
                json.loads(line)
                for line in (stack.root / "evidence.jsonl").read_text().splitlines()
                if json.loads(line).get("scenario") == "shutdown"
            ]
            if (stack.root / "evidence.jsonl").exists()
            else []
        )
        journal_rows = (
            [json.loads(line) for line in journal.path.read_text().splitlines()]
            if journal.path.exists()
            else []
        )
        settled = [r for r in journal_rows if r.get("phase") == "returned" and r.get("settled")]
        known_cost = sum((Decimal(r["known_usage_upper_cost_usd"]) for r in settled), Decimal("0"))
        unknown_count = budget.attempts - len(settled)
        summary["accounting"] = {
            "settled_attempts": len(settled),
            "unknown_attempts": unknown_count,
            "known_usage_upper_cost_usd": str(known_cost),
            "unknown_reserved_usd": str(unknown_count * budget.worst),
            "reservation_reconciled": known_cost + unknown_count * budget.worst
            == budget.reserved_or_spent,
            "billing_dashboard_reconciled": False,
        }
        summary.update(
            effective_cap_usd=str(budget.cap),
            approval={
                k: v for k, v in (getattr(budget, "approval", None) or {}).items() if k != "claim"
            },
            attempts=budget.attempts,
            reserved_or_spent_usd=str(budget.reserved_or_spent),
            budget_events=budget.events,
            shutdown=shutdown,
            cleanup_verified=bool(shutdown)
            and all(s["postgres_removed"] and s["proxy_closed"] for s in shutdown),
        )
        private_workspace.cleanup()
        summary["private_removed"] = not private.exists()
        summary["cleanup_verified"] = (
            summary["cleanup_verified"]
            and summary["private_removed"]
            and summary["services_stopped"]
            and all(
                r.get("cleanup_verified", r["status"] == "not_executed")
                for r in summary["cases"].values()
            )
        )
        write(public / "summary.json", summary)
        suite = ET.Element("testsuite", name="EXP06-smoke-technical", tests="3")
        for case_id, result in summary["cases"].items():
            case = ET.SubElement(suite, "testcase", name=case_id)
            # Semantic pending is NEVER encoded as a successful testcase.
            kind = (
                "failure"
                if result["status"] == "failed"
                else "error"
                if result["status"] == "error"
                else "skipped"
            )
            ET.SubElement(case, kind, message=result["status"])
        for kind in ("failure", "error", "skipped"):
            suite.set(
                {"failure": "failures", "error": "errors", "skipped": "skipped"}[kind],
                str(len(suite.findall("testcase/" + kind))),
            )
        ET.ElementTree(suite).write(
            public / "semantic-status.xml", encoding="utf-8", xml_declaration=True
        )
        (public / "SHA256SUMS").write_text(
            "".join(
                hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n"
                for p in sorted(public.iterdir())
                if p.is_file() and p.name != "SHA256SUMS"
            ),
            encoding="utf-8",
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--execute", action="store_true")
    group.add_argument("--check-account", action="store_true")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/exp06-smoke")
    args = parser.parse_args()
    if args.check_account:
        print(json.dumps(check_account(), indent=2))
    elif args.execute:
        if args.authorization is not None:
            require_live_host()
        budget = authorize(args.authorization, args.output)
        if not os.environ.get("OPENAI_API_KEY"):
            raise SpendingBlocked("environment_API_credential_missing")
        print(
            json.dumps(
                {
                    "effective_cap_usd": str(budget.cap),
                    "theoretical_upper_usd": "6.408",
                    "reservation_upper_usd": "8.010",
                    "http_attempts_max": 18,
                }
            ),
            flush=True,
        )
        account = check_account()
        if account["status"] != "model_visible":
            raise SpendingBlocked("account_model_access_not_confirmed")
        result = run_stack(args.output, budget, os.environ["OPENAI_API_KEY"])
        print(
            json.dumps(
                {
                    "evidence": str(args.output / "evidence"),
                    "statuses": {k: v["status"] for k, v in result["cases"].items()},
                }
            )
        )
        if (
            not result["cleanup_verified"]
            or not result["accounting"]["reservation_reconciled"]
            or any(row["status"] != "pending_review" for row in result["cases"].values())
        ):
            raise SystemExit(2)
    else:
        print(json.dumps(plan(), indent=2))


if __name__ == "__main__":
    main()
