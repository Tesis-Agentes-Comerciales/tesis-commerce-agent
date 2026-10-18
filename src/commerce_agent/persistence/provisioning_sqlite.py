"""Private trusted test issuer and append-only registry. No HTTP or buyer tools.

Keys and credential hashes are private custody data. Public refs resolve to
different issuance/source/grant records, never to the receipt itself.
"""

import hashlib
import hmac
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, SecretBytes, SecretStr

from commerce_agent.acp.authority_observation import AuthoritySession, ObservationBlocked
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.models import SCHEMA, ObjectRef
from commerce_agent.governance.preselection import AuthoritySelection, now
from commerce_agent.governance.provisioning import (
    CatalogReceipt,
    CatalogSource,
    CredentialIssuance,
    EpisodeGrant,
    ProvisioningResolution,
)


class SqliteTestProvisioningRegistry:
    """Backend-only test issuer. Not an adapter to a real production registry."""

    def __init__(self, path: Path, key: SecretBytes):
        if len(key.get_secret_value()) < 32:
            raise ValueError("Persistent independent registry key required")
        self._path, self._key = path, key

    def _connect(self):
        return sqlite3.connect(self._path, timeout=10)

    def initialize(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS q_registry_objects(
                id TEXT PRIMARY KEY,kind TEXT NOT NULL,body BLOB NOT NULL,
                credential_hash TEXT NOT NULL,seal TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS q_registry_grants(
                episode_id TEXT PRIMARY KEY,object_id TEXT NOT NULL UNIQUE);
              CREATE TABLE IF NOT EXISTS q_registry_revoked(id TEXT PRIMARY KEY);
              CREATE UNIQUE INDEX IF NOT EXISTS q_registry_credential_identity
                ON q_registry_objects(credential_hash) WHERE kind='CredentialIssuance';
            """)
            for table in ("q_registry_objects", "q_registry_grants", "q_registry_revoked"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{action} BEFORE {action} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT,'registry immutable'); END"
                    )
                keys = ("episode_id", "object_id") if table == "q_registry_grants" else ("id",)
                conflict = " OR ".join(f"{key}=NEW.{key}" for key in keys)
                if table == "q_registry_objects":
                    conflict += (
                        " OR (NEW.kind='CredentialIssuance' AND kind='CredentialIssuance' "
                        "AND credential_hash=NEW.credential_hash)"
                    )
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {conflict}) "
                    "BEGIN SELECT RAISE(ABORT,'registry immutable'); END"
                )

    def _mac(self, kind, body, private):
        return hmac.new(
            self._key.get_secret_value(),
            canonical_bytes([kind, body.hex(), private]),
            hashlib.sha256,
        ).hexdigest()

    def _append(self, obj: BaseModel, *, credential_hash="") -> ObjectRef:
        body = canonical_bytes(obj)
        ref = ObjectRef(
            schema_version=SCHEMA,
            id="registry:" + str(uuid4()),
            version=1,
            digest=sha256_digest(body),
        )
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO q_registry_objects VALUES(?,?,?,?,?)",
                (
                    ref.id,
                    type(obj).__name__,
                    body,
                    credential_hash,
                    self._mac(type(obj).__name__, body, credential_hash),
                ),
            )
        return ref

    def _get[T: BaseModel](self, ref: ObjectRef, model: type[T]) -> tuple[T, str]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT kind,body,credential_hash,seal FROM q_registry_objects WHERE id=?",
                (ref.id,),
            ).fetchone()
        if (
            row is None
            or ref.version != 1
            or row[0] != model.__name__
            or sha256_digest(row[1]) != ref.digest
            or not hmac.compare_digest(self._mac(row[0], row[1], row[2]), row[3])
        ):
            raise ObservationBlocked("PROVISIONING_UNAVAILABLE")
        return parse_model(model, row[1]), row[2]

    def issue_test_session(self, source: CatalogSource, bearer: SecretStr) -> AuthoritySession:
        """Trusted test setup, before discovery; never derive source from a buyer seal."""
        source = parse_model(CatalogSource, canonical_bytes(source))
        source_ref = self._append(source)
        credential = CredentialIssuance(
            event_id=str(uuid4()), catalog_source_ref=source_ref, issued_at=now()
        )
        credential_ref = self._append(
            credential,
            credential_hash=hashlib.sha256(bearer.get_secret_value().encode()).hexdigest(),
        )
        receipt = CatalogReceipt(
            event_id=str(uuid4()),
            source=source,
            source_ref=source_ref,
            credential_ref=credential_ref,
        )
        receipt_ref = self._append(receipt)
        return AuthoritySession(receipt.binding(receipt_ref), bearer)

    def grant_test_episode(self, grant: EpisodeGrant) -> ObjectRef:
        grant = parse_model(EpisodeGrant, canonical_bytes(grant))
        if grant.episode_id != grant.d1_run_id:
            raise ObservationBlocked("LOCAL_EPISODE_CONFLICT")
        self._get(grant.receipt_ref, CatalogReceipt)
        ref = self._append(grant)
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO q_registry_grants VALUES(?,?)", (grant.episode_id, ref.id))
        return ref

    def revoke_test_credential(self, credential_ref: ObjectRef):
        self._get(credential_ref, CredentialIssuance)
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO q_registry_revoked VALUES(?)", (credential_ref.id,))

    def resolve(
        self,
        selection: AuthoritySelection,
        *,
        run_id: str,
        owner_id: str,
        store_id: str,
        bearer: SecretStr | None,
    ) -> ProvisioningResolution:
        episode = selection.episode
        receipt_ref = episode.binding.source_ref
        receipt, _ = self._get(receipt_ref, CatalogReceipt)
        source, _ = self._get(receipt.source_ref, CatalogSource)
        issuance, credential_hash = self._get(receipt.credential_ref, CredentialIssuance)
        if (
            source != receipt.source
            or issuance.catalog_source_ref != receipt.source_ref
            or receipt.binding(receipt_ref) != episode.binding
            or bearer is None
            or not hmac.compare_digest(
                credential_hash, hashlib.sha256(bearer.get_secret_value().encode()).hexdigest()
            )
        ):
            raise ObservationBlocked("PROVISIONING_CONFLICT")
        with closing(self._connect()) as db:
            if db.execute(
                "SELECT 1 FROM q_registry_revoked WHERE id=?", (receipt.credential_ref.id,)
            ).fetchone():
                raise ObservationBlocked("SESSION_REVOKED")
            row = db.execute(
                "SELECT object_id FROM q_registry_grants WHERE episode_id=?", (episode.episode_id,)
            ).fetchone()
            if row is None:
                raise ObservationBlocked("EPISODE_GRANT_UNAVAILABLE")
            raw = db.execute("SELECT body FROM q_registry_objects WHERE id=?", row).fetchone()
        if raw is None:
            raise ObservationBlocked("EPISODE_GRANT_UNAVAILABLE")
        grant_ref = ObjectRef(
            schema_version=SCHEMA, id=row[0], version=1, digest=sha256_digest(raw[0])
        )
        grant, _ = self._get(grant_ref, EpisodeGrant)
        if (
            grant.episode_id != episode.episode_id
            or grant.owner_id != owner_id
            or episode.owner_id != owner_id
            or grant.d1_run_id != run_id
            or run_id != episode.episode_id
            or grant.d1_store_id != store_id
            or grant.receipt_ref != receipt_ref
            or grant.selection_digest != sha256_digest(canonical_bytes(selection))
        ):
            raise ObservationBlocked("LOCAL_GRANT_CONFLICT")
        return ProvisioningResolution(
            receipt_ref=receipt_ref,
            credential_ref=receipt.credential_ref,
            catalog_source_ref=receipt.source_ref,
            grant_ref=grant_ref,
            verified_at=now(),
        )
