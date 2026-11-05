"""Manual-only executor of the second P0 real-model batch (docs/design/p0-llm-batch2.md §7-§9).

Default and CI never enable a paid provider. The cost guard is batch 1's, reused as it is
(`exp06_execution.protected_models`: a reservation before every HTTP attempt, the standard tier,
the recorder's client factory, the SDK and HTTP boundaries, the STOP file). This module only gives
it a budget with this preregistration's caps and per-episode limits, and its own human receipt,
claimed once in the Git common directory. exp06.py and exp06_execution.py are not edited: the
batch-1 approval check is swapped for this one only while the guard is installed.

The key comes from the environment and goes only to the Agent's settings in this process
(tests/integration/p0_llm_batch2_live.py); it is never printed, written or logged.
"""

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any
from unittest.mock import patch

import httpx

from commerce_evaluation import exp06_execution
from commerce_evaluation.exp06 import AttemptBudget, Authorization, SpendingBlocked
from commerce_evaluation.exp06_execution import Journal, git
from commerce_evaluation.hnp.llm.cost import TokenPrices
from commerce_evaluation.p0_llm_batch2 import (
    ROOT as REPOSITORY,
)
from commerce_evaluation.p0_llm_batch2 import (
    Slot,
    classify,
    ledger_check,
    plan_summary,
    protocol,
    protocol_hash,
    schedule,
    sha256_file,
    stop_reason,
)
from commerce_evaluation.redaction import redact

ROOT = REPOSITORY
# The preregistration and spec merged in #186 (org/main 4c60aae). A changed byte stops the run.
FROZEN_PROTOCOL_SHA256 = "d1e56d17cc8f8e4690c90b941cd468d2d875c57f143e0174cd81013afdea12e5"
FROZEN_SPEC_SHA256 = "f025beb258261e556bf5db070ec905157698589ee1e7ef977ce0b5d6f27538fb"
# The run's product code is main just before #190 (coordinator's decision of 2026-11-05: #190
# changes the router's prompt and output schema, and HP does not read `conditional` yet). Every
# product path of a paid run equals this commit; only the executor and its evaluation differ.
PRODUCT_BASE = "4c60aae9d7e7336ada6242d3d31a754402b07ef3"
PRODUCT_PATHS = ("src", "ui", "auth", "pyproject.toml", "uv.lock", "langgraph.json", "Dockerfile")
STAGES = ("smoke", "batch")
CLAIMS = "p0-llm-batch2-claims"
# The per-episode ceiling `AttemptBudget.before` keeps; every cell's limit is at most this.
EPISODE_CEILING = 6


def preregistration_frozen() -> bool:
    spec = protocol()["spec"]
    return (
        protocol_hash() == FROZEN_PROTOCOL_SHA256
        and spec["sha256"] == FROZEN_SPEC_SHA256
        and sha256_file(REPOSITORY / spec["path"]) == FROZEN_SPEC_SHA256
    )


def product_changes(root: Path) -> list[str]:
    """The product files of HEAD that differ from `PRODUCT_BASE`; empty for a valid run_ref."""
    changed = git(root, "diff", "--name-only", PRODUCT_BASE, "HEAD", "--", *PRODUCT_PATHS)
    return changed.splitlines()


def limits(stage: str) -> dict[str, Any]:
    """Episodes, attempts and USD bounds of a stage, from the schedule and `cost`."""
    if stage not in STAGES:
        raise SpendingBlocked("stage_not_preregistered")
    cost = protocol()["cost"]
    slots = schedule(stage)
    attempts = sum(slot.max_http_attempts for slot in slots)
    if attempts != cost[stage + "_max_attempts"]:
        raise SpendingBlocked("schedule_attempts_differ_from_preregistration")
    return {
        "episodes": len(slots),
        "http_attempts_max": attempts,
        "worst_reserved_usd_per_attempt": cost["worst_reserved_usd_per_attempt"],
        "reserved_upper_usd": cost[stage + "_reservation_bound_usd"],
        "cap_usd": cost["proposed_" + stage + "_cap_usd"],
    }


class Batch2Budget(AttemptBudget):
    """`AttemptBudget` with this preregistration's prices, caps and per-episode limits.

    Each episode opens with the cell's own limit (`begin_episode`); a request beyond it, beyond
    the stage's attempts or beyond the cap is refused before it is sent. Without an open episode
    nothing can be sent.
    """

    approval: dict[str, Any] | None = None

    def __init__(self, authorization: Authorization | None, *, infrastructure_only: bool = False):
        # The base reads batch 1's plan; everything it set from it is replaced below.
        super().__init__(None, infrastructure_only=True)
        plan = protocol()
        cost = plan["cost"]
        self.prices = TokenPrices(
            Decimal(cost["input_usd_per_million"]), Decimal(cost["output_usd_per_million"])
        )
        self.worst = Decimal(cost["worst_reserved_usd_per_attempt"])
        self.model = plan["model"]
        self.infrastructure_only = infrastructure_only
        self.stage = "smoke"
        self.max_attempts = limits("smoke")["http_attempts_max"]
        self.cap = Decimal(limits("smoke")["cap_usd"]) if infrastructure_only else Decimal("0")
        self.episode_limit = 0
        self._episode_lock = Lock()
        if infrastructure_only:
            return
        if authorization is None:
            raise SpendingBlocked("explicit_model_and_USD_authorization_missing")
        if (
            not preregistration_frozen()
            or authorization.model != self.model
            or authorization.protocol_sha256 != FROZEN_PROTOCOL_SHA256
            or authorization.stage not in STAGES
            or not authorization.human_reference
            or not authorization.max_usd.is_finite()
            or authorization.max_usd <= 0
        ):
            raise SpendingBlocked("authorization_does_not_match_frozen_protocol")
        bounds = limits(authorization.stage)
        if authorization.max_usd > Decimal(bounds["cap_usd"]):
            raise SpendingBlocked(authorization.stage + "_cap_exceeds_registered_proposal")
        self.cap = authorization.max_usd
        self.stage = authorization.stage
        self.max_attempts = bounds["http_attempts_max"]

    def begin_episode(self, limit: int | None = None) -> None:
        if limit is None or not 1 <= limit <= EPISODE_CEILING:
            raise SpendingBlocked("episode_limit_not_preregistered")
        with self._episode_lock:
            super().begin_episode()
            self.episode_limit = limit

    def before(self, request: httpx.Request) -> None:
        # One lock around the check and the reservation: no two requests take the last slot.
        with self._episode_lock:
            if self.episode_attempts >= self.episode_limit:
                raise SpendingBlocked("episode_attempt_limit_reached_before_send")
            super().before(request)


def approved(budget: AttemptBudget) -> None:
    """The batch-1 approval check (`exp06_execution.approved_budget`) for this batch."""
    if not isinstance(budget, Batch2Budget):
        raise SpendingBlocked("batch2_budget_required")
    if budget.infrastructure_only:
        return  # Each transport must still prove it is MockTransport.
    if budget.approval is None:
        raise SpendingBlocked("manual_receipt_claim_missing")
    claim = json.loads(Path(budget.approval["claim"]).read_text(encoding="utf-8"))
    stage = budget.approval["stage"]
    bounds = limits(stage)
    if (
        claim["receipt_sha256"] != budget.approval["receipt_sha256"]
        or claim["stage"] != stage
        or budget.stage != stage
        or Decimal(claim["cap_usd"]) != budget.cap
        or budget.max_attempts != bounds["http_attempts_max"]
        or budget.cap > Decimal(bounds["cap_usd"])
        or budget.worst != Decimal(bounds["worst_reserved_usd_per_attempt"])
        or budget.model != protocol()["model"]
        or not preregistration_frozen()
        or git(ROOT, "rev-parse", "HEAD") != budget.approval["executor_sha"]
        or product_changes(ROOT)
    ):
        raise SpendingBlocked("manual_claim_or_executed_scope_changed")


def authorize(receipt: Path | None, output: Path, stage: str = "smoke") -> Batch2Budget:
    """The stage's human receipt (batch 1's schema), claimed once; before any credential,
    service or request."""
    if receipt is None:
        raise SpendingBlocked("manual_explicit_authorization_required_outside_CI")
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI"):
        raise SpendingBlocked("paid_batch_runs_manually_outside_CI_only")
    if not preregistration_frozen():
        raise SpendingBlocked("preregistration_changed")
    if product_changes(ROOT):
        raise SpendingBlocked("product_code_differs_from_run_base")
    data = json.loads(receipt.read_text(encoding="utf-8"))
    if (
        data.get("authorized") is not True
        or data.get("stage") != stage
        or data.get("executor_sha") != git(ROOT, "rev-parse", "HEAD")
        or data.get("billing_project_confirmed") is not True
        or data.get("standard_pricing_confirmed") is not True
        or not data.get("authorization_id")
        or output.exists()
    ):
        raise SpendingBlocked("receipt_scope_or_output_invalid")
    budget = Batch2Budget(
        Authorization(
            data["model"],
            Decimal(data["max_usd"]),
            data["protocol_sha256"],
            data["stage"],
            data["human_reference"],
        )
    )
    common = Path(git(ROOT, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = ROOT / common
    identifier = hashlib.sha256(str(data["authorization_id"]).encode()).hexdigest()
    receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
    claim = common / CLAIMS / (identifier + ".json")
    claim.parent.mkdir(parents=True, exist_ok=True)
    try:
        with claim.open("x", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "receipt_sha256": receipt_sha256,
                        "output": str(output.resolve()),
                        "cap_usd": str(budget.cap),
                        "stage": stage,
                    }
                )
            )
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise SpendingBlocked("authorization_already_claimed_no_automatic_resume") from error
    budget.approval = {
        "claim": str(claim),
        "receipt_sha256": receipt_sha256,
        "executor_sha": data["executor_sha"],
        "authorization_id_sha256": identifier,
        "stage": stage,
    }
    return budget


@contextmanager
def protected_models(budget: AttemptBudget, journal: Journal) -> Iterator[set[httpx.Client]]:
    """Batch 1's guard with this batch's approval check; dedicated process only. It covers the
    three model clients of the Agent: `OpenAIConversationRouter`, `OpenAIDelegatedChooser` and
    `ModelHnpIntentReviewer` all build theirs with `ModelRequestRecorder.http_client()`."""
    approved(budget)
    with (
        patch.object(exp06_execution, "approved_budget", approved),
        exp06_execution.protected_models(budget, journal) as guarded,
    ):
        yield guarded


# The run


def write(path: Path, value: Any) -> None:
    path.write_text(
        redact(json.dumps(value, indent=2, ensure_ascii=False, default=str)) + "\n",
        encoding="utf-8",
    )


def sources(budget: Batch2Budget) -> dict[str, Any]:
    """The three checkouts: the Merchant and the PSP at their pinned SHAs, clean; the Agent at
    the receipt's commit and clean when the run is paid."""
    plan = protocol()
    result = {}
    for role, directory, expected in [
        ("agent", ROOT, None),
        ("merchant", Path(os.environ["MERCHANT_REPO"]), plan["merchant_sha"]),
        ("psp", Path(os.environ["PAYMENT_REPO"]), plan["payment_sha"]),
    ]:
        sha = git(directory, "rev-parse", "HEAD")
        clean = not git(directory, "status", "--porcelain")
        if expected is not None and (sha != expected or not clean):
            raise SpendingBlocked("unclean_or_unpinned_source:" + role)
        if role == "agent" and not budget.infrastructure_only and not clean:
            raise SpendingBlocked("unclean_or_unpinned_source:agent")
        result[role] = {
            "sha": sha,
            "clean": clean,
            "uv_lock_sha256": hashlib.sha256((directory / "uv.lock").read_bytes()).hexdigest(),
        }
    return result


def episode_name(repetition: int, cell_id: str, condition: str) -> str:
    return f"r{repetition}-{cell_id}-{condition}".lower()


def run_episode(
    lanes: Mapping[str, Any],
    slot: Slot,
    access: Any,
    root: Path,
    budget: Batch2Budget,
    journal: Journal,
    name: str,
) -> dict[str, Any]:
    """One slot behind the installed guard: its episode opens with the cell's limit, and its
    ledger must account for every attempt the guard reserved. The executor and the scripted
    controls both judge a row here."""
    from tests.integration.p0_llm_batch2_live import run_slot

    journal.case_id = name
    budget.begin_episode(slot.max_http_attempts)
    result = run_slot(lanes, slot, access, root, name=name)
    obs = result["obs"]
    issue = ledger_check(
        result["model_requests"],
        budget.episode_attempts,
        budget.model,
        simulated=budget.infrastructure_only,
    )
    if issue is not None and not obs.get("technical"):
        obs["technical"] = issue
    return classify(slot.cell_id, slot.condition, obs) | {
        "repetition": slot.repetition,
        "episode": name,
        "http_attempts": budget.episode_attempts,
        "max_http_attempts": slot.max_http_attempts,
        "observed": obs,
        "evidence": result["evidence"],
        "model_requests": result["model_requests"],
    }


def run_stack(output: Path, budget: Batch2Budget, key: str) -> dict[str, Any]:
    """Every slot of the stage in order, each on a fresh episode, behind the guard; the stop rule
    ends the run and the remaining slots stay `not_executed`. Evidence goes to output/evidence."""
    exp06_execution.require_live_host()
    from pydantic import SecretStr

    from tests.integration.live_purchase import PurchaseLane
    from tests.integration.live_stack import LiveStack
    from tests.integration.p0_llm_batch2_live import CANCEL_ROUTE, ModelAccess

    approved(budget)
    provenance = sources(budget)
    slots = schedule(budget.stage)
    output.mkdir(parents=True, exist_ok=False)
    public = output / "evidence"
    public.mkdir()
    private_workspace = tempfile.TemporaryDirectory(prefix="p0-llm2-private-")
    private = Path(private_workspace.name)
    journal = Journal(public / "attempts.jsonl")
    access = ModelAccess(SecretStr(key), budget.model)
    summary: dict[str, Any] = {
        "plan": plan_summary(),
        "stage": budget.stage,
        "protocol_sha256": FROZEN_PROTOCOL_SHA256,
        "spec_sha256": FROZEN_SPEC_SHA256,
        "sources": provenance,
        "infrastructure_only": budget.infrastructure_only,
        "rows": {},
        "stopped": None,
    }
    write(
        public / "execution-manifest.json",
        {
            "plan": plan_summary(),
            "stage": budget.stage,
            "sources": provenance,
            "effective_cap_usd": str(budget.cap),
            "execution_authorized": not budget.infrastructure_only,
            "approval": {k: v for k, v in (budget.approval or {}).items() if k != "claim"},
            "created_at_ns": time.time_ns(),
        },
    )
    (private / "stack").mkdir()
    stack = LiveStack(private / "stack")
    lanes: dict[str, PurchaseLane] = {}
    try:
        stack.start()
        for profile in ("b0", "hnp"):
            lanes[profile] = PurchaseLane(stack, profile)
            lanes[profile].start()
        # The B0' budget guard cancels its checkout through the proxy, as in batch 1.
        lanes["b0"].proxy.routes |= {CANCEL_ROUTE}
        with protected_models(budget, journal):
            for slot in slots:
                name = episode_name(slot.repetition, slot.cell_id, slot.condition)
                row = run_episode(lanes, slot, access, private / name, budget, journal, name)
                summary["rows"][name] = {
                    column: row[column]
                    for column in (
                        "cell_id",
                        "condition",
                        "repetition",
                        "status",
                        "reason",
                        "clarify_source",
                        "agent_obeyed",
                        "governance_contained",
                        "http_attempts",
                    )
                } | {"cleanup_verified": row["evidence"].get("cleanup_verified", False)}
                write(public / (name + ".json"), row)
                reason = stop_reason(slot.cell_id, row, row["observed"])
                if reason is not None:
                    summary["stopped"] = {"episode": name, "reason": reason}
                    break
    finally:
        for slot in slots:
            name = episode_name(slot.repetition, slot.cell_id, slot.condition)
            summary["rows"].setdefault(
                name,
                {
                    "cell_id": slot.cell_id,
                    "condition": slot.condition,
                    "repetition": slot.repetition,
                    "status": "not_executed",
                    "reason": "earlier_stop",
                },
            )
        try:
            for lane in lanes.values():
                lane.close()
        finally:
            stack.close()
        summary["services_stopped"] = all(
            lane.merchant.process is None and lane.payment.process is None
            for lane in lanes.values()
        )
        if (stack.root / "evidence.jsonl").exists():
            (public / "evidence.jsonl").write_bytes((stack.root / "evidence.jsonl").read_bytes())
        for index, logfile in enumerate(sorted(private.rglob("*.log"))):
            (public / f"service-{index}.log").write_text(
                redact(logfile.read_text(encoding="utf-8", errors="replace")), encoding="utf-8"
            )
        summary["accounting"] = accounting(budget, journal)
        private_workspace.cleanup()
        summary["private_removed"] = not private.exists()
        summary["cleanup_verified"] = (
            summary["services_stopped"]
            and summary["private_removed"]
            and all(
                row.get("cleanup_verified", row["status"] == "not_executed")
                for row in summary["rows"].values()
            )
        )
        summary.update(
            effective_cap_usd=str(budget.cap),
            attempts=budget.attempts,
            reserved_or_spent_usd=str(budget.reserved_or_spent),
            budget_events=budget.events,
        )
        write(public / "summary.json", summary)
        (public / "SHA256SUMS").write_text(
            "".join(
                hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n"
                for p in sorted(public.iterdir())
                if p.is_file() and p.name != "SHA256SUMS"
            ),
            encoding="utf-8",
        )
    return summary


def accounting(budget: Batch2Budget, journal: Journal) -> dict[str, Any]:
    """Known usage plus the full reservation of every attempt without it; never a bill."""
    rows = (
        [json.loads(line) for line in journal.path.read_text().splitlines()]
        if journal.path.exists()
        else []
    )
    settled = [r for r in rows if r.get("phase") == "returned" and r.get("settled")]
    known = sum((Decimal(r["known_usage_upper_cost_usd"]) for r in settled), Decimal("0"))
    unknown = budget.attempts - len(settled)
    return {
        "settled_attempts": len(settled),
        "unknown_attempts": unknown,
        "known_usage_upper_cost_usd": str(known),
        "unknown_reserved_usd": str(unknown * budget.worst),
        "reservation_reconciled": known + unknown * budget.worst == budget.reserved_or_spent,
        "billing_dashboard_reconciled": False,
    }


def execute(receipt: Path | None, output: Path, stage: str) -> int:
    """`--execute`: receipt, credential, account, then the run. 0 only if every executed row is
    technically complete, the cleanup verified and the reservation reconciled."""
    if receipt is not None:
        exp06_execution.require_live_host()
    budget = authorize(receipt, output, stage)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SpendingBlocked("environment_API_credential_missing")
    bounds = limits(stage)
    print(
        json.dumps(
            {
                "stage": stage,
                "effective_cap_usd": str(budget.cap),
                "reservation_upper_usd": bounds["reserved_upper_usd"],
                "http_attempts_max": bounds["http_attempts_max"],
            }
        ),
        flush=True,
    )
    # A non-inference GET; batch 1 and this batch share the model snapshot.
    if exp06_execution.protocol()["model"] != budget.model:
        raise SpendingBlocked("account_check_model_differs")
    if exp06_execution.check_account()["status"] != "model_visible":
        raise SpendingBlocked("account_model_access_not_confirmed")
    result = run_stack(output, budget, key)
    print(
        json.dumps(
            {
                "evidence": str(output / "evidence"),
                "stopped": result["stopped"],
                "statuses": {k: v["status"] for k, v in result["rows"].items()},
            }
        )
    )
    executed = [row for row in result["rows"].values() if row["status"] != "not_executed"]
    if (
        result["stopped"] is not None
        or not result["cleanup_verified"]
        or not result["accounting"]["reservation_reconciled"]
        or any(row["status"] in {"error", "blocked"} for row in executed)
    ):
        return 2
    return 0
