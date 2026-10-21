"""The port between the HNP executor and D2-HNP / D3-HNP (docs/design/hnp-governed.md §3.4).

The executor programs against `HnpPolicyGate`, with a null gate in `off`; the governed gate
records the evidence. It assembles every fact from the backend's store and the authoritative
checkout, never from the model, exactly as D2-HP does.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from commerce_agent.acp.models import CheckoutSession

from .hnp_models import ProofStatus
from .models import PolicyDecision


@dataclass(frozen=True)
class HnpGateDecision:
    """One D2-HNP or D3-HNP decision, as the executor applies or classifies it (§8.1).

    - `decision` is `None` only in `off` (B0-HNP), which evaluates nothing and fabricates no
      decision; in `enforce` it is always the policy result or a runtime guard deny.
    - `proof` is the status of the open proof (D2) or the closed proof (D3) the gate resolved, or
      `None` if it resolved none: an `invalid` proof is `failed(mandate_proof_invalid)`.
    - `evidence_id` is generated before the transaction: `d2_evidence_id` of the reservation or
      `d3_evidence_id` of the outbox row (§4.3).
    - `hooks` run inside the ledger unit of work that applies the decision (§4.5): UoW-1 for a
      D2 allow, UoW-2 for a D3 allow, the terminal one for a deny. A hook that raises rolls the
      whole unit back.
    - `conflict_hooks` replace `hooks` when that UoW-1 or UoW-2 comes back `blocked` (a lost
      CAS): they run in the terminal unit of work and keep the allow as `blocked` plus its
      `GOV_STATE_CONFLICT` application failure (§4.5, «En B2 el perdedor deja evidencia»).
    - `transition_ref` is what the allow names as its transition: the `outbox_id` of the D3
      outbox row, which the executor must commit under that id; `None` in D2 (the
      `reservation_id` it was given) and in `off`.
    """

    decision: PolicyDecision | None
    proof: ProofStatus | None
    evidence_id: str | None
    hooks: tuple[Callable[[sqlite3.Connection], None], ...] = ()
    conflict_hooks: tuple[Callable[[sqlite3.Connection], None], ...] = ()
    transition_ref: str | None = None


class HnpPolicyGate(Protocol):
    """Backend injection only: no model-facing tool reaches it."""

    def decide_d2(
        self,
        *,
        run_id: str,
        grant_id: str,
        checkout: CheckoutSession,
        reservation_id: str,
        operation_id: str,
    ) -> HnpGateDecision:
        """POL-03..09 after the `close` freshness check, before the reservation and the close."""
        ...

    def decide_d3(
        self,
        *,
        run_id: str,
        grant_id: str,
        checkout: CheckoutSession,
        reservation_id: str,
        operation_id: str,
    ) -> HnpGateDecision:
        """POL-09 and POL-10 after the close, before the outbox row is committed."""
        ...
