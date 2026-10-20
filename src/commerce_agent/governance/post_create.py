"""Experimental post-create effects, never an authorization or recovery transport."""

from collections.abc import Callable
from contextlib import closing, suppress
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from commerce_agent.acp.models import CheckoutSession

from .dispatch_envelope import EnvelopeModel
from .models import Digest, Id, UtcTime

Effect = Literal["verify_creation_get", "update_details", "get_current", "cancel_budget"]
Phase = Literal[
    "creation_observed",
    "intent",
    "observed",
    "creation_confirmed",
    "awaiting_confirmation",
    "canceled",
    "uncertain",
]


class PostCreateEvent(EnvelopeModel):
    schema_version: Literal["commerce-post-create/1"] = "commerce-post-create/1"
    operation_id: Id
    run_id: Id
    owner_ref: Id
    checkout_id: Id
    version: int = Field(ge=1)
    phase: Phase
    effect: Effect | None
    request_digest: Digest | None
    key_digest: Digest | None
    snapshot_digest: Digest | None
    previous_digest: Digest | None
    evidence_id: Id
    evidence_digest: Digest
    envelope_digest: Digest
    recorded_at: UtcTime
    binding_status: Literal["unavailable", "backend_expected_unverified"]
    replay_allowed: Literal[False] = False
    continuation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self):
        if self.phase in {"intent", "observed"}:
            if self.effect is None:
                raise ValueError("Missing effect")
        elif self.effect is not None:
            raise ValueError("Unexpected effect")
        if self.phase == "intent":
            if self.request_digest is None or self.snapshot_digest is not None:
                raise ValueError("Missing intent binding")
            if (self.effect in {"update_details", "cancel_budget"}) != (
                self.key_digest is not None
            ):
                raise ValueError("Invalid idempotency key binding")
        elif self.request_digest is not None or self.key_digest is not None:
            raise ValueError("Unexpected request facts")
        if (self.phase in {"creation_observed", "observed"}) != (self.snapshot_digest is not None):
            raise ValueError("Invalid snapshot binding")
        return self


class ExperimentalPostCreateDispatch(Protocol):
    """Backend port, never a human authority claim. Production: runtime_dispatch.py."""

    def perform_once(
        self, effect: Effect, call: Callable[[], CheckoutSession]
    ) -> CheckoutSession: ...


class PostCreateFlow:
    def __init__(self, connect, keys, run_id: str, owner_id: str, port):
        self.connect, self.keys = connect, keys
        self.run_id, self.owner_id, self.port = run_id, owner_id, port
        self.version: int | None = None

    def _store(self, connection):
        from commerce_agent.persistence.post_create_sqlite import SqlitePostCreateStore

        return SqlitePostCreateStore(connection, self.keys)

    def inspect(self) -> tuple[PostCreateEvent, ...]:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            try:
                return self._store(connection).history(self.run_id, self.owner_id)
            finally:
                connection.rollback()

    def _append(self, phase: Phase, effect: Effect | None = None, **facts) -> PostCreateEvent:
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            store = self._store(connection)
            store.initialize()
            history = store.history(self.run_id, self.owner_id)
            expected = len(history) if self.version is None else self.version
            event = store.append(
                self.run_id,
                self.owner_id,
                expected_version=expected,
                phase=phase,
                effect=effect,
                recorded_at=datetime.now(UTC),
                **facts,
            )
        self.version = event.version
        return event

    def begin(self, checkout: CheckoutSession) -> None:
        self._append("creation_observed", checkout_id=checkout.id, snapshot=checkout)

    def effect(
        self,
        effect: Effect,
        body: bytes,
        key: str | None,
        call: Callable[[], CheckoutSession],
    ) -> CheckoutSession:
        from .canonical import sha256_digest
        from .evidence import EvidenceError

        if self.port is None:
            raise EvidenceError("GOV_FACTS_UNAVAILABLE")
        self._append(
            "intent",
            effect,
            request_digest=sha256_digest(body),
            key_digest=sha256_digest(key.encode()) if key is not None else None,
        )
        try:
            result = self.port.perform_once(effect, call)
            # Revalidate even instances constructed with model_copy/model_construct.
            from commerce_agent.persistence.provenance_sqlite import _model_values

            result = CheckoutSession.model_validate(_model_values(result))
            history = self.inspect()
            if not history or result.id != history[-1].checkout_id:
                raise EvidenceError("GOV_STATE_CONFLICT")
            self._append("observed", effect, snapshot=result)
            return result
        except BaseException:
            self.fail()
            raise

    def confirm_creation(self, connection) -> None:
        event = self._store(connection).append(
            self.run_id,
            self.owner_id,
            expected_version=self.version,
            phase="creation_confirmed",
            effect=None,
            recorded_at=datetime.now(UTC),
        )
        self.version = event.version

    def finish(self, *, canceled: bool = False) -> None:
        self._append("canceled" if canceled else "awaiting_confirmation")

    def fail(self) -> None:
        # A failed diagnostic never replaces the durable intent or creation facts.
        with suppress(Exception):
            self._append("uncertain")
