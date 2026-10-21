"""H2: whether a purchase with an effect has its whole evidence chain, as P2's verifier reports it.

The verifier is P2's `commerce_evaluation.verify_evidence` (coordination decision 4). The runner
calls its `verify_database` once per repetition, after the agent stopped, and the summary adds its
`h2_summary`. Until that module is in the checkout, `verifier_readiness` says so and a live run
refuses to start, so it is imported here, lazily, and nowhere else.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

VERIFIER_MODULE = "commerce_evaluation.verify_evidence"
VERIFIER_API = ("verify_database", "h2_summary")
STATUSES = frozenset({"ok", "missing", "mismatch", "not_applicable"})

type VerifyRun = Callable[[Path, str, Path | None], list[dict[str, Any]]]


class EvidenceChainError(RuntimeError):
    pass


def verifier() -> ModuleType:
    try:
        return importlib.import_module(VERIFIER_MODULE)
    except ModuleNotFoundError as error:
        raise EvidenceChainError(f"{VERIFIER_MODULE} is not in this checkout") from error


def verifier_readiness(links: Sequence[str]) -> dict[str, Any]:
    """Ready when the verifier imports, has its API and reports exactly the registered links."""
    try:
        module = verifier()
    except EvidenceChainError:
        return {"ready": False, "module": VERIFIER_MODULE, "links_match": None}
    has_api = all(callable(getattr(module, name, None)) for name in VERIFIER_API)
    links_match = list(getattr(module, "LINKS", ())) == list(links)
    return {"ready": has_api and links_match, "module": VERIFIER_MODULE, "links_match": links_match}


def _verify_database(
    database: Path, run_id: str, psp_database: Path | None
) -> list[dict[str, Any]]:
    return verifier().verify_database(database, run_id=run_id, psp_database=psp_database)


def h2_summary(chains: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """P2's H2 summary of the recorded chains: effects, all runs and statuses per link."""
    return verifier().h2_summary(list(chains))


def link_statuses(chain: Mapping[str, Any]) -> dict[str, str]:
    """`{link: status}` of one chain as the verifier documents it."""
    links = chain.get("links")
    if not isinstance(links, Mapping):
        raise EvidenceChainError("The verifier document has no links")
    statuses = {
        str(name): str(link.get("status")) if isinstance(link, Mapping) else str(link)
        for name, link in links.items()
    }
    unknown = {name for name, status in statuses.items() if status not in STATUSES}
    if unknown:
        raise EvidenceChainError(f"Links with an unknown status: {', '.join(sorted(unknown))}")
    return statuses


def chain_complete(chain: Mapping[str, Any] | None) -> int | None:
    """For a purchase with an effect (an order): 1 only if every registered link is ok.

    `not_applicable` does not complete a purchase that reached an order (B2-PILOT-v2's H2). A
    purchase without an order has no H2 value.
    """
    if chain is None or chain.get("order_id") is None:
        return None
    statuses = link_statuses(chain)
    return int(all(statuses.get(link) == "ok" for link in chain["required_links"]))


@dataclass(frozen=True)
class EvidenceChainCheck:
    """The registered links and how to ask the verifier about one purchase."""

    links: tuple[str, ...]
    verify_run: VerifyRun = field(default=_verify_database, repr=False)

    def verify(self, database: Path, run_id: str, psp_database: Path | None) -> dict[str, Any]:
        """The row's `evidence_chain`: the verifier's document of the purchase, without refs."""
        runs = self.verify_run(database, run_id, psp_database)
        if len(runs) != 1:
            raise EvidenceChainError(f"The verifier found {len(runs)} purchases for the run")
        run = runs[0]
        link_statuses(run)
        return {
            "required_links": list(self.links),
            "run_id": run["run_id"],
            "stage": run["stage"],
            "order_id": run["order_id"],
            "complete": run["complete"],
            "links": {
                name: {"status": link["status"], "detail": link.get("detail")}
                for name, link in run["links"].items()
            },
        }


def evidence_chain_check(manifest: Mapping[str, Any]) -> EvidenceChainCheck | None:
    chain = manifest.get("evidence_chain")
    return None if chain is None else EvidenceChainCheck(tuple(chain["links"]))
