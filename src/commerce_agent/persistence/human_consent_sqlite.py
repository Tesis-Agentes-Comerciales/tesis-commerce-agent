"""Isolated D2 custody store. It cannot invoke a commercial transport or ApprovalService."""

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import SecretBytes, SecretStr

from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.dispatch_envelope import EnvelopeModel
from commerce_agent.governance.human_consent import (
    Clock,
    ConsentBlocked,
    ConsentCommand,
    ConsentReceipt,
    HumanAuthenticator,
    HumanIdentity,
    Presentation,
    Presented,
    TermsAlreadyDecided,
    timestamp,
)
from commerce_agent.governance.preselection import instant
from commerce_agent.persistence.d2_d17_source import VerifiedD17Source


class _StoredPresentation(EnvelopeModel):
    presentation: Presentation
    recipient: HumanIdentity
    challenge_sha256: str


def _immutable(*tables: str) -> tuple[str, ...]:
    return tuple(
        f"CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable D2'); END"
        for table in tables
        for action in ("UPDATE", "DELETE")
    ) + tuple(
        f"CREATE TRIGGER {table}_replace BEFORE INSERT ON {table} "
        f"WHEN EXISTS(SELECT 1 FROM {table} WHERE id=NEW.id) "
        "BEGIN SELECT RAISE(ABORT,'immutable D2'); END"
        for table in tables
    )


# Original 07C store. Recognized only to migrate it, never operated on directly.
_LEGACY_SCHEMA = (
    "CREATE TABLE d2_meta(id INTEGER PRIMARY KEY CHECK(id=1),"
    "body BLOB NOT NULL,seal TEXT NOT NULL)",
    "CREATE TABLE d2_presentations(id TEXT PRIMARY KEY,body BLOB NOT NULL,seal TEXT NOT NULL)",
    "CREATE TABLE d2_decisions(id TEXT PRIMARY KEY REFERENCES d2_presentations(id),"
    "event_id TEXT UNIQUE NOT NULL,body BLOB NOT NULL,seal TEXT NOT NULL)",
    *_immutable("d2_meta", "d2_presentations", "d2_decisions"),
)
# Additive: one terminal decision per checkout terms across presentations. Every receipt
# is projected here in its own transaction; SQL rejects the opposite decision even if
# a writer skips the domain check. The run scope is the store itself (d2_meta request).
_TERMS_SCHEMA = (
    "CREATE TABLE d2_terms_decisions(id TEXT PRIMARY KEY REFERENCES d2_decisions(id),"
    "checkout_id TEXT NOT NULL,terms_fingerprint TEXT NOT NULL,"
    "decision TEXT NOT NULL CHECK(decision IN ('accept','reject')))",
    *_immutable("d2_terms_decisions"),
    "CREATE TRIGGER d2_terms_decisions_terminal BEFORE INSERT ON d2_terms_decisions "
    "WHEN EXISTS(SELECT 1 FROM d2_terms_decisions WHERE checkout_id=NEW.checkout_id "
    "AND terms_fingerprint=NEW.terms_fingerprint AND decision IS NOT NEW.decision) "
    "BEGIN SELECT RAISE(ABORT,'terminal D2 terms'); END",
)
_SCHEMA = (*_LEGACY_SCHEMA, *_TERMS_SCHEMA)


def _layout(schema: tuple[str, ...]) -> list[tuple[str]]:
    return [(sql,) for sql in sorted(schema)]


class SqliteHumanConsent:
    def __init__(
        self,
        path: Path,
        *,
        source: VerifiedD17Source,
        custody_key: SecretBytes,
        authenticator: HumanAuthenticator,
        clock: Clock | None = None,
    ):
        protected = (
            source.applied.path,
            source.applied.shadow.path,
            source.applied.shadow.source._path,
            source.applied.shadow.registry._path,
        )
        if len(custody_key.get_secret_value()) < 32 or path.resolve() in {
            p.resolve() for p in protected
        }:
            raise ValueError("Independent D2 database and persistent custody key required")
        self.path, self.source = path, source
        self._key, self._auth = custody_key, authenticator
        self._clock = clock or (lambda: datetime.now(UTC))

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _mac(self, kind: str, body: bytes) -> str:
        return hmac.new(
            self._key.get_secret_value(),
            b"commerce-d2-custody/1\0" + kind.encode() + b"\0" + body,
            hashlib.sha256,
        ).hexdigest()

    def _configuration(self) -> bytes:
        return canonical_bytes(
            {
                "schema": "commerce-d2-store/1",
                "request": self.source.request.model_dump(mode="json"),
                "applied_store": str(self.source.applied.path.resolve()),
            }
        )

    @staticmethod
    def _schema(db):
        return db.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND type IN ('table','trigger') ORDER BY sql"
        ).fetchall()

    def _custody(self, db):
        body = self._configuration()
        if db.execute("SELECT id,body,seal FROM d2_meta").fetchall() != [
            (1, body, self._mac("meta", body))
        ]:
            raise ConsentBlocked("D2_CUSTODY_CONFLICT")

    def _check(self, db):
        if self._schema(db) != _layout(_SCHEMA):
            raise ConsentBlocked("D2_SCHEMA_CONFLICT")
        self._custody(db)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            names = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not names:
                for sql in _SCHEMA:
                    db.execute(sql)
                body = self._configuration()
                db.execute("INSERT INTO d2_meta VALUES(1,?,?)", (body, self._mac("meta", body)))
            elif self._schema(db) == _layout(_LEGACY_SCHEMA):
                self._migrate_terms(db)
            self._check(db)

    def _migrate_terms(self, db):
        """Project existing sealed receipts in the caller transaction; never rewrite them.

        A historical accept and reject of the same terms cannot be projected. The
        transaction rolls back, the legacy store stays intact, and it remains closed.
        """
        self._custody(db)
        for sql in _TERMS_SCHEMA:
            db.execute(sql)
        for presentation_id, event_id, body, seal in db.execute(
            "SELECT id,event_id,body,seal FROM d2_decisions ORDER BY rowid"
        ).fetchall():
            if not hmac.compare_digest(seal, self._mac("decision", body)):
                raise ConsentBlocked("CONSENT_CUSTODY_CONFLICT")
            receipt = parse_model(ConsentReceipt, body)
            if (
                canonical_bytes(receipt) != body
                or receipt.presentation_id != presentation_id
                or receipt.event_id != event_id
            ):
                raise ConsentBlocked("CONSENT_CONTEXT_CONFLICT")
            try:
                db.execute(
                    "INSERT INTO d2_terms_decisions VALUES(?,?,?,?)",
                    (
                        presentation_id,
                        receipt.checkout_id,
                        receipt.terms_fingerprint,
                        receipt.decision,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConsentBlocked("D2_TERMS_HISTORY_CONFLICT") from None

    def _human(self, credential: SecretStr) -> HumanIdentity:
        try:
            human = parse_model(HumanIdentity, canonical_bytes(self._auth.authenticate(credential)))
            request = self.source.request
            now = instant(timestamp(self._clock()))
            if (
                human.owner_id != request.owner_id
                or human.run_id != request.run_id
                or not instant(human.authenticated_at) <= now < instant(human.expires_at)
            ):
                raise ValueError
            return human
        except Exception:
            raise ConsentBlocked("HUMAN_SCOPE_OR_AUTHENTICATION_INVALID") from None

    @staticmethod
    def _same_human(first: HumanIdentity, second: HumanIdentity) -> bool:
        fields = ("principal_id", "owner_id", "run_id", "issuer", "assurance", "kind")
        return all(getattr(first, field) == getattr(second, field) for field in fields)

    def present(self, credential: SecretStr) -> Presented:
        human = self._human(credential)
        terms = self.source.terms()
        challenge = secrets.token_urlsafe(32)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._check(db)
            now = instant(timestamp(self._clock()))
            deadline = min(
                now + timedelta(minutes=5),
                instant(terms.offer_expires_at),
                instant(terms.checkout_expires_at),
                instant(human.expires_at),
            )
            if deadline <= now or now < instant(human.authenticated_at):
                raise ConsentBlocked("TERMS_OR_SESSION_EXPIRED")
            view = Presentation(
                presentation_id=str(uuid4()),
                episode_id=self.source.request.episode_id,
                owner_id=human.owner_id,
                run_id=human.run_id,
                terms=terms,
                terms_fingerprint=terms.fingerprint(),
                presented_at=timestamp(now),
                valid_until=timestamp(deadline),
            )
            stored = _StoredPresentation(
                presentation=view,
                recipient=human,
                challenge_sha256=hashlib.sha256(challenge.encode()).hexdigest(),
            )
            body = canonical_bytes(stored)
            db.execute(
                "INSERT INTO d2_presentations VALUES(?,?,?)",
                (view.presentation_id, body, self._mac("presentation", body)),
            )
        # Only after SQLite commit; an exception produces no successful presentation.
        return Presented(presentation=view, challenge=SecretStr(challenge))

    def _presentation(self, db, presentation_id, human, terms):
        row = db.execute(
            "SELECT body,seal FROM d2_presentations WHERE id=?", (presentation_id,)
        ).fetchone()
        if row is None or not hmac.compare_digest(row[1], self._mac("presentation", row[0])):
            raise ConsentBlocked("PRESENTATION_NOT_REGISTERED_OR_CORRUPT")
        stored = parse_model(_StoredPresentation, row[0])
        view = stored.presentation
        request = self.source.request
        if (
            canonical_bytes(stored) != row[0]
            or view.presentation_id != presentation_id
            or (view.run_id, view.owner_id, view.episode_id)
            != (request.run_id, request.owner_id, request.episode_id)
            or view.terms != terms
            or not self._same_human(stored.recipient, human)
        ):
            raise ConsentBlocked("PRESENTATION_CONTEXT_CHANGED")
        return stored

    def _receipt(self, db, stored):
        view = stored.presentation
        row = db.execute(
            "SELECT d.event_id,d.body,d.seal,t.checkout_id,t.terms_fingerprint,t.decision "
            "FROM d2_decisions d LEFT JOIN d2_terms_decisions t ON t.id=d.id WHERE d.id=?",
            (view.presentation_id,),
        ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(row[2], self._mac("decision", row[1])):
            raise ConsentBlocked("CONSENT_CUSTODY_CONFLICT")
        receipt = parse_model(ConsentReceipt, row[1])
        if (
            canonical_bytes(receipt) != row[1]
            or receipt.event_id != row[0]
            or row[3:] != (receipt.checkout_id, receipt.terms_fingerprint, receipt.decision)
            or receipt.presentation_id != view.presentation_id
            or receipt.presentation_digest != sha256_digest(canonical_bytes(view))
            or receipt.terms_fingerprint != view.terms_fingerprint
            or receipt.checkout_id != view.terms.checkout_id
            or receipt.affirmative != (receipt.decision == "accept")
            or not self._same_human(receipt.identity, stored.recipient)
            or not instant(view.presented_at)
            <= instant(receipt.decided_at)
            < instant(view.valid_until)
            or not instant(receipt.identity.authenticated_at)
            <= instant(receipt.decided_at)
            < instant(receipt.identity.expires_at)
        ):
            raise ConsentBlocked("CONSENT_CONTEXT_CONFLICT")
        return receipt

    def _terminal(self, db, view):
        """Sealed terminal decision of these checkout terms on any presentation, or None."""
        decisions = set()
        for presentation_id, decision, body, seal in db.execute(
            "SELECT t.id,t.decision,d.body,d.seal FROM d2_terms_decisions t "
            "JOIN d2_decisions d ON d.id=t.id WHERE t.checkout_id=? AND t.terms_fingerprint=?",
            (view.terms.checkout_id, view.terms_fingerprint),
        ):
            if not hmac.compare_digest(seal, self._mac("decision", body)):
                raise ConsentBlocked("CONSENT_CUSTODY_CONFLICT")
            receipt = parse_model(ConsentReceipt, body)
            if (
                receipt.presentation_id,
                receipt.checkout_id,
                receipt.terms_fingerprint,
                receipt.decision,
            ) != (presentation_id, view.terms.checkout_id, view.terms_fingerprint, decision):
                raise ConsentBlocked("CONSENT_CONTEXT_CONFLICT")
            decisions.add(decision)
        if len(decisions) > 1:
            raise ConsentBlocked("CONSENT_CONTEXT_CONFLICT")
        return next(iter(decisions), None)

    def decide(self, credential: SecretStr, command: ConsentCommand) -> ConsentReceipt:
        # Reparse: model_copy/model_construct are not validation boundaries.
        command = parse_model(
            ConsentCommand,
            canonical_bytes(
                {
                    **command.model_dump(mode="json"),
                    "challenge": command.challenge.get_secret_value(),
                }
            ),
        )
        human = self._human(credential)
        terms = self.source.terms()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._check(db)
            stored = self._presentation(db, command.presentation_id, human, terms)
            view = stored.presentation
            now = instant(timestamp(self._clock()))
            if self._receipt(db, stored) is not None:
                raise ConsentBlocked("PRESENTATION_ALREADY_DECIDED")
            if (
                not instant(view.presented_at)
                <= now
                < min(instant(view.valid_until), instant(human.expires_at))
            ):
                raise ConsentBlocked("CONSENT_EXPIRED")
            if (
                command.checkout_id != terms.checkout_id
                or command.terms_fingerprint != view.terms_fingerprint
                or command.future_action != terms.future_action
                or not hmac.compare_digest(
                    stored.challenge_sha256,
                    hashlib.sha256(command.challenge.get_secret_value().encode()).hexdigest(),
                )
            ):
                raise ConsentBlocked("CONSENT_TERMS_OR_CHALLENGE_CONFLICT")
            # Each presentation still decides once. Across presentations, the same
            # decision is an idempotent replay of the terms; the opposite one is blocked.
            if self._terminal(db, view) not in {None, command.decision}:
                raise TermsAlreadyDecided()
            receipt = ConsentReceipt(
                event_id=str(uuid4()),
                presentation_id=view.presentation_id,
                presentation_digest=sha256_digest(canonical_bytes(view)),
                terms_fingerprint=view.terms_fingerprint,
                checkout_id=terms.checkout_id,
                identity=human,
                decision=command.decision,
                affirmative=command.affirmative,
                decided_at=timestamp(now),
            )
            body = canonical_bytes(receipt)
            db.execute(
                "INSERT INTO d2_decisions VALUES(?,?,?,?)",
                (view.presentation_id, receipt.event_id, body, self._mac("decision", body)),
            )
            db.execute(
                "INSERT INTO d2_terms_decisions VALUES(?,?,?,?)",
                (
                    view.presentation_id,
                    receipt.checkout_id,
                    receipt.terms_fingerprint,
                    receipt.decision,
                ),
            )
        return receipt

    def recover(
        self, credential: SecretStr, presentation_id: str
    ) -> tuple[Presentation, ConsentReceipt | None]:
        """Historical receipt recovery; expired terms stay historical, never executable."""
        human = self._human(credential)
        terms = self.source.terms()
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            self._check(db)
            stored = self._presentation(db, presentation_id, human, terms)
            return stored.presentation, self._receipt(db, stored)
