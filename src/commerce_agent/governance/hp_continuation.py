"""Governed Human Present continuation (AGENT_HP_FLOW=governed).

Enforce used to refuse every step after a D1 checkout (`d1_continuation_blocked`). In the
governed flow, presenting terms, approving, canceling and completing continue only for the
checkout D1 itself created and verified. Any other purchase keeps the refusal.
"""

import logging
from typing import TYPE_CHECKING

from commerce_agent.config import Settings
from commerce_agent.persistence import SqliteAgentStore

if TYPE_CHECKING:
    from .enforce import SqliteD1EnforceGate

logger = logging.getLogger(__name__)


class GovernedContinuation:
    """Read-only rule over the gate's durable facts; it performs no commercial effect."""

    def __init__(self, gate: "SqliteD1EnforceGate", store: SqliteAgentStore) -> None:
        self._gate = gate
        self._store = store

    def admits(self, run_id: str) -> bool:
        # Fail closed: an unreadable or tampered fact refuses rather than raising. The warning
        # names only the failure type: never the run, the buyer or a stored fact.
        try:
            return self._facts_hold(run_id)
        except Exception as error:
            logger.warning("governed_continuation_unreadable error=%s", type(error).__name__)
            return False

    def _facts_hold(self, run_id: str) -> bool:
        purchase = self._store.purchase(run_id)
        if purchase is None:
            return False
        checkout_id, owner_id = purchase["checkout_id"], purchase["owner_id"]
        if not isinstance(checkout_id, str) or not isinstance(owner_id, str):
            return False
        operation = self._confirmed_operation(run_id, checkout_id)
        return (
            operation is not None
            and self._gate.committed_allow(run_id) == operation
            and self._awaits_confirmation(run_id, owner_id, checkout_id)
        )

    def _confirmed_operation(self, run_id: str, checkout_id: str) -> str | None:
        """The dispatch operation, if it verified this very checkout."""
        status = self._gate.recovery_status(run_id)
        if status is None or status[0:2] != ("confirmed", checkout_id):
            return None
        return status[2]

    def _awaits_confirmation(self, run_id: str, owner_id: str, checkout_id: str) -> bool:
        """The post-create journal ended handing the checkout to the human."""
        history = self._gate.post_create_flow(run_id, owner_id).inspect()
        return (
            bool(history)
            and history[-1].phase == "awaiting_confirmation"
            and history[-1].checkout_id == checkout_id
        )


def governed_continuation(
    settings: Settings, gate: "SqliteD1EnforceGate | None", store: SqliteAgentStore
) -> GovernedContinuation | None:
    """Only enforce with the governed flow continues; blocked keeps every refusal."""
    if gate is None or settings.agent_hp_flow != "governed":
        return None
    return GovernedContinuation(gate, store)
