"""An in-memory fictitious destination, never an HTTP or payment adapter."""

from dataclasses import dataclass

from .evidence import EffectReceipt


@dataclass(frozen=True)
class _Permit:
    proposal_id: str
    record_digest: str
    issuer: object


class SimulatedEffectSink:
    def __init__(self):
        self.events: list[EffectReceipt] = []
        self._issuer: object | None = None
        self._seen: set[str] = set()

    def _bind(self, issuer: object) -> None:
        if self._issuer is not None:
            raise ValueError("Destination already bound")
        self._issuer = issuer

    def _emit(self, permit: _Permit) -> EffectReceipt:
        if (
            self._issuer is None
            or type(permit) is not _Permit
            or permit.issuer is not self._issuer
            or permit.proposal_id in self._seen
        ):
            raise ValueError("No coordinator permission or duplicate effect")
        event = EffectReceipt(proposal_id=permit.proposal_id, record_digest=permit.record_digest)
        self._seen.add(permit.proposal_id)
        self.events.append(event)
        return event
