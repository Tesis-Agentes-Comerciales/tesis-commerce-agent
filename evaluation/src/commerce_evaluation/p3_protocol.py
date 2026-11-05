"""Fail-closed experiment gates. No product entry point imports this module."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from .scenarios_p0 import CELL_EXPECTATIONS, validate_cell

ORACLE_SHA = "697b04ac157a94c848a2f8db5228e2161b61b1e317f50d85ac3a31d66cb18621"
Clone = dict[str, Any]


class ProtocolBlocked(ValueError):
    """Evidence is insufficient to open the next experimental phase."""


def require(condition: object, reason: str) -> None:
    if not condition:
        raise ProtocolBlocked(reason)


def digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def authoritative_total(
    wire: dict, persisted: dict, offer: dict, *, run_id: str, actor_id: str
) -> int:
    """Compare the ACP global total with independently witnessed scope and pricing."""
    try:
        totals = wire["totals"]
        by_type = {t["type"]: t["amount"] for t in totals}
        pricing = offer["pricing"]
        items, shipping, total = (
            pricing[k] for k in ("items_total_minor", "shipping_total_minor", "total_minor")
        )
        require(
            len(by_type) == len(totals)
            and all(type(n) is int and n >= 0 for n in (*by_type.values(), items, shipping, total))
            and by_type["total"] == total == items + shipping
            and by_type["fulfillment"] == shipping,
            "Invalid ACP global total or shipping",
        )
        lines = wire["line_items"]
        require(
            len(lines) == 1
            and type(lines[0]["quantity"]) is int
            and lines[0]["quantity"] == 1
            and lines[0]["item"]["id"] == offer["id"]
            and wire["currency"] == pricing["currency"]
            and wire["expires_at"] == offer["expires_at"]
            and (persisted["run_id"], persisted["actor_id"]) == (run_id, actor_id)
            and bool(run_id and actor_id and wire["id"])
            and persisted["id"] == wire["id"]
            and persisted["offer_id"] == offer["id"]
            and type(persisted["quantity"]) is int
            and persisted["quantity"] == 1
            and persisted["offer_revision"] == offer["revision"]
            and digest(persisted["pricing"]) == digest(pricing)
            and persisted["delivery_context"] == offer["delivery_context"],
            "Unbound checkout identity, scope or terms",
        )
    except (KeyError, TypeError, IndexError) as exc:
        raise ProtocolBlocked("Incomplete ACP or persistence witness") from exc
    return total


def equivalent_clones(clones: dict[str, Clone]) -> str:
    require(set(clones) == {"A-cal", "B-cal", "V", "E"}, "Missing or extra clone")
    values = list(clones.values())
    for key in ("run_id", "actor_id", "session_alias", "agent_store", "thread", "operation_scope"):
        ids = [clone.get(key) for clone in values]
        require(all(ids) and len(set(ids)) == len(values), f"Reused clone {key}")
    required = {"offers", "feed_sha256", "clock", "configuration"}
    require(all(required <= clone.keys() for clone in values), "Incomplete clone snapshot")
    signatures = [digest({key: clone[key] for key in required}) for clone in values]
    require(len(set(signatures)) == 1 and values[0]["offers"], "Non-equivalent clone state")
    return signatures[0]


@dataclass(frozen=True)
class CheckoutWitness:
    wire: dict
    persisted: dict
    offer: dict
    run_id: str
    actor_id: str

    def total(self) -> int:
        return authoritative_total(
            self.wire, self.persisted, self.offer, run_id=self.run_id, actor_id=self.actor_id
        )


@dataclass(frozen=True)
class Creation:
    scope: str
    key_digest: str
    result: Literal["accepted", "replay", "lost", "rejected"]
    checkout: str | None
    witness: Literal["response", "lookup", "none"]


def verified_cleanup(
    attempts: list[Creation],
    canceled_readbacks: dict[str, str],
    persisted: dict[str, tuple[str, str]],
) -> str:
    """One logical checkout per scoped key, including a recovered response loss."""
    require(attempts, "Missing creation ledger")
    keys: dict[tuple[str, str], str] = {}
    sessions: dict[str, str] = {}
    for attempt in attempts:
        key = (attempt.scope, attempt.key_digest)
        require(all(key), "Missing creation scope or key")
        if attempt.result == "rejected":
            require(attempt.checkout is None and attempt.witness == "none", "Invalid rejection")
            continue
        checkout = attempt.checkout
        if checkout is None:
            raise ProtocolBlocked("Unreconciled checkout creation")
        expected = "lookup" if attempt.result == "lost" else "response"
        require(attempt.witness == expected, "Unreconciled checkout creation")
        if attempt.result == "replay":
            require(keys.get(key) == checkout, "Replay changed checkout")
            continue
        require(key not in keys and checkout not in sessions, "Duplicate logical checkout")
        keys[key] = checkout
        sessions[checkout] = attempt.scope
    require(bool(sessions), "Missing witnessed checkout")
    require(canceled_readbacks == dict.fromkeys(sessions, "canceled"), "Missing cancel readback")
    require(
        persisted == {checkout: (scope, "canceled") for checkout, scope in sessions.items()},
        "Unaccounted persistent checkout or incomplete cleanup",
    )
    return digest([(scope, key, checkout) for (scope, key), checkout in sorted(keys.items())])


class ProtocolGate:
    """The verifier only receives an external proposal after a checked seal."""

    def __init__(self, oracle: bytes) -> None:
        require(hashlib.sha256(oracle).hexdigest() == ORACLE_SHA, "Historical oracle hash changed")
        cells = json.loads(oracle)["cells"]
        require([cell["id"] for cell in cells] == list(CELL_EXPECTATIONS), "Incomplete cases")
        for cell in cells:
            validate_cell(cell)
        self._expected = {cell["id"]: cell["expected"] for cell in cells}
        self._phase = "design"
        self._seal: str | None = None
        self._witnesses: dict[str, Any] = {}
        self._observed: dict[str, str] = {}

    def calibrate(self, observed: dict[str, tuple[CheckoutWitness, CheckoutWitness]]) -> None:
        require(self._phase == "design" and set(observed) == {"A", "B"}, "Calibration out of phase")
        a, va = observed["A"]
        b, vb = observed["B"]
        require(
            len({a.run_id, b.run_id, va.run_id}) == 3
            and va.run_id == vb.run_id
            and va.actor_id == vb.actor_id
            and a.offer["id"] != b.offer["id"],
            "Calibration scopes or alternatives reused",
        )
        for first, repeat in observed.values():
            require(
                first.total() == repeat.total() and digest(first.offer) == digest(repeat.offer),
                "Unstable authoritative total",
            )
        self._witnesses["totals"] = {
            branch: [digest(vars(witness)) for witness in pair] for branch, pair in observed.items()
        }
        self._calibration_scopes = {
            role: (w.run_id, w.actor_id) for role, w in (("A-cal", a), ("B-cal", b), ("V", va))
        }
        self._calibrated_offers = {
            "A-cal": [digest(a.offer)],
            "B-cal": [digest(b.offer)],
            "V": [digest(va.offer), digest(vb.offer)],
        }
        self._calibrated_checkouts = {
            witness.wire["id"]: witness.run_id for pair in observed.values() for witness in pair
        }
        require(len(self._calibrated_checkouts) == 4, "Calibration reused a checkout identity")
        self._phase = "calibrated"

    def cleanup(self, attempts, readbacks, persisted) -> None:
        require(self._phase == "calibrated", "Cleanup out of phase")
        require(
            all(
                persisted.get(checkout) == (scope, "canceled")
                for checkout, scope in self._calibrated_checkouts.items()
            ),
            "Calibration checkout missing from ledger",
        )
        self._witnesses["ledger"] = verified_cleanup(attempts, readbacks, persisted)
        self._phase = "cleaned"

    def compare(self, clones: dict[str, Clone], evaluation_before: dict, evaluation_after: dict):
        require(self._phase == "cleaned", "Equivalence out of phase")
        require(evaluation_before and evaluation_before == evaluation_after, "E state changed")
        self._witnesses["clones"] = equivalent_clones(clones)
        require(
            all(
                (clones[role]["run_id"], clones[role]["actor_id"]) == scope
                for role, scope in self._calibration_scopes.items()
            ),
            "Calibration clone binding changed",
        )
        for role, witnessed in self._calibrated_offers.items():
            require(
                set(witnessed) <= {digest(offer) for offer in clones[role]["offers"]},
                "Calibration Offer absent from frozen clone",
            )
        require(evaluation_before.get("run_id") == clones["E"]["run_id"], "Unbound E snapshot")
        self._witnesses["evaluation"] = digest(evaluation_before)
        self._phase = "equivalent"

    def seal(self, inputs: dict[str, Any]) -> str:
        require(self._phase == "equivalent", "Seal out of phase")
        require(
            set(inputs) == set(self._expected) and all(inputs.values()), "Incomplete case inputs"
        )
        self._seal = digest({"inputs": inputs, "oracle": ORACLE_SHA, "witnesses": self._witnesses})
        self._phase = "sealed"
        return self._seal

    def observe(self, case: str, proposal: str, inputs: dict[str, Any]) -> None:
        require(self._phase == "sealed", "Observation before seal")
        require(
            self._seal
            == digest({"inputs": inputs, "oracle": ORACLE_SHA, "witnesses": self._witnesses}),
            "Sealed inputs changed",
        )
        require(case in self._expected and case not in self._observed, "Unknown or duplicate case")
        # An unexpected product clarification is an observed decision, not invalid evidence.
        # The frozen oracle does not prescribe "clarify": scoring must record a failure.
        require(proposal in {"A", "B", "none", "undetermined", "clarify"}, "Invalid proposal label")
        self._observed[case] = proposal

    def score(self, case: str) -> bool:
        require(case in self._observed, "Cannot score before observation")
        return self._observed[case] == self._expected[case]
