"""What a trial leaves behind, read once the Agent stopped (docs/design/eval-suite.md §4).

- The final state: the episode's checkouts and orders from the Merchant (`suite_state`), and
  the PSP's `approved` payments of those checkouts.
- The agent's usage, from its `model_requests` ledger: each row's tokens and the `model` each
  response reported. The ledger's `input_tokens` counts every input token, the cache's included
  (src/commerce_agent/llm/model_requests.py); a `TokenUsage` bills the uncached ones apart from
  the cache reads (`cached_input_tokens`) and writes (`cache_write_tokens`).
- The infrastructure failures that are no one's design: an Agent API answer 5xx, a Merchant or
  PSP answer 5xx (or no answer) that FaultProxy did not inject, and a model request whose
  provider answered 5xx on every attempt. `suite/reduce.py` decides whether the case injected it.
"""

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from commerce_agent.persistence import SqliteAgentStore
from commerce_evaluation.flow_trace import build_flow_trace, build_hp_flow_trace
from commerce_evaluation.llm.cost import NO_USAGE, TokenUsage
from commerce_evaluation.stack.fault_proxy import Call, FaultProxy
from commerce_evaluation.stack.purchase_lane import Episode, PurchaseLane
from commerce_evaluation.suite.graders.evidence import verified
from commerce_evaluation.suite.live.settings import TrialAgent
from commerce_evaluation.suite.trial import Ending, FinalState, InfraFailure, Row, Turn

PAYMENTS = "payments.sqlite"
# The ledger's columns for each TokenUsage field.
USAGE_COLUMNS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cached_input_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "reasoning_tokens": "reasoning_tokens",
}


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
    # The Agent API's answers 5xx, as "METHOD path status".
    api_5xx: tuple[str, ...]


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


def agent_usage(database: Path) -> tuple[TokenUsage, tuple[str, ...], int]:
    """The ledger's tokens summed, the models that answered (in order) and how many requests
    the provider answered 5xx on every attempt."""
    if not database.exists():
        return NO_USAGE, (), 0
    columns = ", ".join(USAGE_COLUMNS.values())
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'model_requests'").fetchone():
            return NO_USAGE, (), 0
        rows = db.execute(
            f"SELECT {columns}, response_model, outcome, http_status FROM model_requests "
            "ORDER BY sequence"
        ).fetchall()
    usage = NO_USAGE
    for row in rows:
        counts = {name: row[index] or 0 for index, name in enumerate(USAGE_COLUMNS)}
        cached = counts["cache_read_tokens"] + counts["cache_write_tokens"]
        counts["input_tokens"] = max(counts["input_tokens"] - cached, 0)
        usage += TokenUsage(**counts)
    width = len(USAGE_COLUMNS)
    models = tuple(dict.fromkeys(row[width] for row in rows if row[width]))
    failed = sum(row[width + 1] == "error" and (row[width + 2] or 0) >= 500 for row in rows)
    return usage, models, failed


def upstream_failures(proxies: Iterable[FaultProxy | None]) -> list[str]:
    """Merchant and PSP answers 5xx, or none, that no fault of the case armed."""
    calls: list[Call] = [call for proxy in proxies if proxy is not None for call in proxy.calls]
    return [
        f"{call.method} {call.path} {call.status or call.fault}"
        for call in calls
        if call.fault == "merchant_unreachable"
        or (call.fault == "pass" and (call.status or 0) >= 500)
    ]


def infra_failure(
    api: Sequence[str], upstream: Sequence[str], provider_5xx: int
) -> InfraFailure | None:
    parts = [f"API del agente: {', '.join(api)}"] if api else []
    if upstream:
        parts.append(f"comercio o PSP: {', '.join(upstream)}")
    if provider_5xx:
        parts.append(f"proveedor del modelo: {provider_5xx} solicitudes con 5xx")
    return InfraFailure(cause="api_5xx", detail="; ".join(parts)) if parts else None


def json_row(value: Any) -> Row:
    """A JSON object, as the record keeps it."""
    return json.loads(json.dumps(value))
