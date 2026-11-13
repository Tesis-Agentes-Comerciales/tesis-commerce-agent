"""What a trial leaves behind, read once the Agent stopped (docs/design/eval-suite.md §4).

- The final state: the episode's checkouts and orders from the Merchant (`suite_state`), and
  the PSP's `approved` payments of those checkouts.
- The infrastructure failures that are no one's design: an Agent API answer 502, 503 or 504, a
  Merchant or PSP answer 502, 503 or 504 (or no answer) that FaultProxy did not inject, a model
  request whose provider answered 5xx on every attempt, and a delegado task still running when
  the wait ends. `suite/reduce.py` decides whether the case injected it. A bare 500 is an
  unhandled exception, not the infrastructure's: the trial is graded and its trace keeps it.
"""

import json
import re
import secrets
import sqlite3
import traceback
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from commerce_agent.persistence import SqliteAgentStore
from commerce_evaluation.flow_trace import build_flow_trace, build_hp_flow_trace
from commerce_evaluation.stack.fault_proxy import Call, FaultProxy
from commerce_evaluation.stack.purchase_lane import Episode, PurchaseLane
from commerce_evaluation.suite.cases import Governance, Suite
from commerce_evaluation.suite.graders.evidence import verified
from commerce_evaluation.suite.live.settings import TrialAgent
from commerce_evaluation.suite.trial import (
    Agent,
    Ending,
    FinalState,
    InfraFailure,
    Row,
    TrialRecord,
    Turn,
    Usage,
)

PAYMENTS = "payments.sqlite"
# The answers the product and the stack give when a dependency is down or slow (api.py maps its
# known failures to them, with a code); a 500 is an unhandled exception instead.
INFRA_STATUSES = frozenset({502, 503, 504})


@dataclass(frozen=True)
class Played:
    """What a mode's runner saw of its trial; the executor reads the rest from the stack."""

    agent: TrialAgent
    episode: Episode
    run_id: str
    transcript: tuple[Turn, ...]
    final: Ending
    # The flow trace, plus what the runner did (`suite`): JSON once in the record.
    trace: dict[str, Any]
    # The Agent API's answers 502, 503 or 504, as "METHOD path status".
    api_5xx: tuple[str, ...]
    # A delegado task still running when the wait ended: infrastructure, never the agent.
    unfinished: str | None = None


def deliver_webhooks(lane: PurchaseLane, agent: TrialAgent) -> None:
    """The Merchant's due webhooks go to the trial's Agent database, where the order evidence
    expects them; each trial delivers its own before the next starts."""
    with lane.receiver.delivering_to(SqliteAgentStore(agent.database)):
        lane.rpc("dispatch_webhooks", limit=50)


def final_state(lane: PurchaseLane, episode: Episode) -> FinalState:
    state = lane.rpc("suite_state", run_id=episode.run_id)
    checkouts = tuple(state["checkouts"])
    return FinalState(
        orders=tuple(state["orders"]),
        checkouts=checkouts,
        psp_charges=approved_payments(
            lane.root / PAYMENTS, {row["id"] for row in checkouts if isinstance(row["id"], str)}
        ),
    )


def approved_payments(path: Path, checkouts: set[str]) -> tuple[Row, ...]:
    """The provider's confirmed payments of these checkouts that it approved."""
    if not path.exists():
        return ()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT checkout_id, result_json FROM delegations WHERE result_json IS NOT NULL "
            "ORDER BY created_at"
        ).fetchall()
    charges = []
    for checkout, result_json in rows:
        result = json.loads(result_json)
        if checkout in checkouts and result.get("status") == "approved":
            charges.append(
                {
                    "id": result["id"],
                    "checkout_id": checkout,
                    "amount_minor": result["amount"],
                    "currency": result["currency"],
                }
            )
    return tuple(charges)


def evidence(lane: PurchaseLane, agent: TrialAgent) -> tuple[Row, ...]:
    return verified(agent.database, lane.root / PAYMENTS)


def flow_trace(mode: Literal["HP", "HNP"], agent: TrialAgent, feed: Path) -> dict[str, Any]:
    """build_hp_flow_trace's document in presente, build_flow_trace's in delegado, swept of the
    trial's secrets; a block that cannot be built says only its error's class."""
    database = agent.database
    graph = database.with_name(f"{database.stem}.graph.db")
    try:
        if mode == "HP":
            return build_hp_flow_trace(database, graph, agent.hidden())
        return build_flow_trace(database, graph, agent.hidden(), feed=feed)
    except Exception as error:
        return {"flow_trace": None, "error": type(error).__name__}


def upstream_failures(proxies: Iterable[FaultProxy | None]) -> list[str]:
    """Merchant and PSP answers 502, 503 or 504, or none, that no fault of the case armed."""
    calls: list[Call] = [call for proxy in proxies if proxy is not None for call in proxy.calls]
    return [
        f"{call.method} {call.path} {call.status or call.fault}"
        for call in calls
        if call.fault == "merchant_unreachable"
        or (call.fault == "pass" and call.status in INFRA_STATUSES)
    ]


def infra_failure(
    api: Sequence[str], upstream: Sequence[str], provider_5xx: int, unfinished: str | None = None
) -> InfraFailure | None:
    parts = [f"API del agente: {', '.join(api)}"] if api else []
    if upstream:
        parts.append(f"comercio o PSP: {', '.join(upstream)}")
    if provider_5xx:
        parts.append(f"proveedor del modelo: {provider_5xx} solicitudes con 5xx")
    if unfinished:
        parts.append(unfinished)
    return InfraFailure(cause="api_5xx", detail="; ".join(parts)) if parts else None


def json_row(value: Any) -> Row:
    """A JSON object, as the record keeps it."""
    return json.loads(json.dumps(value))


def start_failure(error: Exception, root: Path) -> str:
    """Why the stack or a lane did not start: the error's class and the harness's own marker,
    never a value; the logs stay under `root`."""
    text = "".join(traceback.format_exception_only(error))
    markers = re.findall(r"LIVE_STACK_[A-Z_]+(?::[a-z0-9_]+)?", text)
    return " ".join([type(error).__name__, *markers, f"(registros en {root})"])


def unobserved(
    key: tuple[str, Suite, Governance, int, int], agent: Agent, failure: InfraFailure
) -> TrialRecord:
    """The record of a trial whose stack failed before anything could be observed; `key` is
    its case, suite, governance, trial and seed."""
    case_id, suite, governance, trial, seed = key
    return TrialRecord(
        case_id=case_id,
        suite=suite,
        governance=governance,
        trial=trial,
        seed=seed,
        run_id=f"unobserved-{secrets.token_hex(8)}",
        agent=agent,
        transcript=(),
        trace={},
        evidence=(),
        final=Ending(),
        final_state=FinalState(orders=(), checkouts=(), psp_charges=()),
        usage=Usage(agent={}, user={}, judge={}, usd=0.0),
        infra_failure=failure,
    )
