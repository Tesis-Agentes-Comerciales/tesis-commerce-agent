"""Dedicated auxiliary SQLite; no migration/promotion of operational history.

Append-only HMAC seals protect snapshots and bindings. Retain the backend key
across restart; loss/wrong key fails closed. HMAC is local custody, not a Merchant
signature or proof of current authority. No automatic resume of claimed episodes.
"""

import hashlib
import hmac
import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel, SecretBytes

from commerce_agent.acp.authority_observation import CatalogBinding, ObservationBlocked
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.preselection import AuthoritySelection, Episode, OfferObservation


class SqlitePreselectionStore:
    def __init__(self, path: Path, key: SecretBytes):
        if len(key.get_secret_value()) < 32:
            raise ValueError("Persistent custody key required")
        self._path, self._key = path, key

    def _connect(self):
        connection = sqlite3.connect(self._path, timeout=10)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names - {"pa_episodes", "pa_observations", "pa_selections"}:
                raise ObservationBlocked("DEDICATED_AUXILIARY_STORE_REQUIRED")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pa_episodes(
                  id TEXT PRIMARY KEY, payload BLOB NOT NULL, seal TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('open','capturing','blocked','sealed')));
                CREATE TABLE IF NOT EXISTS pa_observations(
                  id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES pa_episodes(id),
                  variant_id TEXT NOT NULL, payload BLOB NOT NULL, seal TEXT NOT NULL,
                  UNIQUE(episode_id,variant_id));
                CREATE TABLE IF NOT EXISTS pa_selections(
                  episode_id TEXT PRIMARY KEY REFERENCES pa_episodes(id),
                  payload BLOB NOT NULL, seal TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS pa_episode_guard BEFORE UPDATE ON pa_episodes
                  WHEN OLD.id IS NOT NEW.id OR OLD.payload IS NOT NEW.payload
                  OR OLD.seal IS NOT NEW.seal OR NOT (
                    (OLD.state='open' AND NEW.state='capturing') OR
                    (OLD.state='capturing' AND NEW.state IN ('blocked','sealed')))
                  BEGIN SELECT RAISE(ABORT,'immutable episode or invalid transition'); END;
                CREATE TRIGGER IF NOT EXISTS pa_episode_delete BEFORE DELETE ON pa_episodes
                  BEGIN SELECT RAISE(ABORT,'immutable episode'); END;
                CREATE TRIGGER IF NOT EXISTS pa_observation_insert BEFORE INSERT ON pa_observations
                  WHEN (SELECT state FROM pa_episodes WHERE id=NEW.episode_id) != 'capturing'
                  BEGIN SELECT RAISE(ABORT,'capture not claimed'); END;
                CREATE TRIGGER IF NOT EXISTS pa_selection_insert BEFORE INSERT ON pa_selections
                  WHEN (SELECT state FROM pa_episodes WHERE id=NEW.episode_id) != 'capturing'
                  BEGIN SELECT RAISE(ABORT,'capture not claimed'); END;
            """)
            for table in ("pa_observations", "pa_selections"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{action} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'immutable evidence'); END"
                    )
            db.commit()

    def _seal(self, kind: str, payload: bytes) -> str:
        return hmac.new(
            self._key.get_secret_value(), kind.encode() + b"\0" + payload, hashlib.sha256
        ).hexdigest()

    def _parse[T: BaseModel](self, model: type[T], row) -> T:
        if row is None or not hmac.compare_digest(self._seal(model.__name__, row[0]), row[1]):
            raise ObservationBlocked("EVIDENCE_UNAVAILABLE")
        try:
            result = parse_model(model, row[0])
            if canonical_bytes(result) != row[0]:
                raise ValueError("Noncanonical evidence")
            return result
        except Exception:
            raise ObservationBlocked("EVIDENCE_INVALID") from None

    def open(self, episode: Episode) -> None:
        payload = canonical_bytes(parse_model(Episode, canonical_bytes(episode)))
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO pa_episodes VALUES(?,?,?,'open')",
                (episode.episode_id, payload, self._seal("Episode", payload)),
            )

    def claim(self, episode_id: str, owner: str, binding: CatalogBinding) -> Episode:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload,seal,state FROM pa_episodes WHERE id=?", (episode_id,)
            ).fetchone()
            episode = self._parse(Episode, row[:2] if row else None)
            if (
                episode.episode_id != episode_id
                or episode.owner_id != owner
                or episode.binding != binding
                or row[2] != "open"
            ):
                raise ObservationBlocked("NEW_EPISODE_REQUIRED")
            db.execute("UPDATE pa_episodes SET state='capturing' WHERE id=?", (episode_id,))
            return episode

    def append(self, observation: OfferObservation) -> None:
        payload = canonical_bytes(parse_model(OfferObservation, canonical_bytes(observation)))
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO pa_observations VALUES(?,?,?,?,?)",
                (
                    observation.observation_id,
                    observation.episode_id,
                    observation.candidate.variant_id,
                    payload,
                    self._seal("OfferObservation", payload),
                ),
            )

    def _observations(self, db, episode_id):
        return tuple(
            self._parse(OfferObservation, row)
            for row in db.execute(
                "SELECT payload,seal FROM pa_observations WHERE episode_id=? ORDER BY rowid",
                (episode_id,),
            )
        )

    def seal(self, selection: AuthoritySelection) -> None:
        selection = parse_model(AuthoritySelection, canonical_bytes(selection))
        episode = selection.episode
        payload = canonical_bytes(selection)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            stored = self._parse(
                Episode,
                db.execute(
                    "SELECT payload,seal FROM pa_episodes WHERE id=?", (episode.episode_id,)
                ).fetchone(),
            )
            if (
                stored != episode
                or self._observations(db, episode.episode_id) != selection.observations
            ):
                raise ObservationBlocked("EVIDENCE_SUBSTITUTION")
            db.execute(
                "INSERT INTO pa_selections VALUES(?,?,?)",
                (episode.episode_id, payload, self._seal("AuthoritySelection", payload)),
            )
            if (
                db.execute(
                    "UPDATE pa_episodes SET state='sealed' WHERE id=? AND state='capturing'",
                    (episode.episode_id,),
                ).rowcount
                != 1
            ):
                raise ObservationBlocked("SEAL_CONFLICT")

    def block(self, episode_id: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE pa_episodes SET state='blocked' WHERE id=? AND state='capturing'",
                (episode_id,),
            )

    def recover(self, episode_id: str, owner: str) -> AuthoritySelection:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN")
            episode_row = db.execute(
                "SELECT payload,seal,state FROM pa_episodes WHERE id=?", (episode_id,)
            ).fetchone()
            episode = self._parse(Episode, episode_row[:2] if episode_row else None)
            selection = self._parse(
                AuthoritySelection,
                db.execute(
                    "SELECT payload,seal FROM pa_selections WHERE episode_id=?", (episode_id,)
                ).fetchone(),
            )
            if (
                episode.owner_id != owner
                or episode.episode_id != episode_id
                or episode_row[2] != "sealed"
                or selection.episode != episode
                or selection.observations != self._observations(db, episode_id)
            ):
                raise ObservationBlocked("EVIDENCE_BINDING_CONFLICT")
            return selection
