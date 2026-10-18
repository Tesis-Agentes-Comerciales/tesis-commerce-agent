"""Explicit D1 operational vault. Caller supplies keys, connection and transaction.

No initialization in off. Explicit shadow callers own the transaction.
Ciphertext is AES-256-GCM, not SQLite/database encryption.
Provisioning, backup, file ACLs and key rotation remain deployment responsibilities.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from functools import wraps
from typing import Any, Literal

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, TypeAdapter

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.domain.intent import PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.evaluator import GovernanceError, verify_artifact
from commerce_agent.governance.models import SCHEMA, Id, IntentFacts, ObjectRef, PolicyBinding
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    ContextData,
    ContextObservation,
    D1Configuration,
    D1EnforceConfiguration,
    ProvenanceKeys,
    RegisteredContext,
    build_d1_policy,
)
from commerce_agent.governance.provenance_artifacts import measure_d1_sources

Kind = Literal[
    "intent",
    "feed",
    "constraint",
    "algorithm",
    "normalization",
    "clock",
    "commercial",
    "policy",
    "context",
]
_ID = TypeAdapter(Id)


def _safe[T, **P](operation: Callable[P, T]) -> Callable[P, T]:
    @wraps(operation)
    def checked(*args: P.args, **kwargs: P.kwargs) -> T:
        connection = getattr(args[0], "connection", None) if args else None
        savepoint = (
            connection
            if isinstance(connection, sqlite3.Connection) and connection.in_transaction
            else None
        )
        try:
            if savepoint is not None:
                savepoint.execute("SAVEPOINT d1_provenance_call")
            result = operation(*args, **kwargs)
            if savepoint is not None:
                savepoint.execute("RELEASE d1_provenance_call")
            return result
        except Exception as error:
            if savepoint is not None:
                try:
                    savepoint.execute("ROLLBACK TO d1_provenance_call")
                    savepoint.execute("RELEASE d1_provenance_call")
                except sqlite3.Error:
                    raise GovernanceError("GOV_EVIDENCE_UNAVAILABLE") from None
            if isinstance(error, GovernanceError):
                raise
            # Neither SQLite errors nor Pydantic errors may echo private input.
            raise GovernanceError("GOV_EVIDENCE_UNAVAILABLE") from None

    return checked


def _model_values(value: Any) -> Any:
    """Rebuild raw Python fields before serialization can coerce constructed values."""
    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        if value.model_config.get("extra") != "allow" and (
            set(value.__dict__) - set(fields) or value.__pydantic_extra__
        ):
            raise GovernanceError("GOV_INPUT_INVALID")
        return {
            **{name: _model_values(getattr(value, name)) for name in fields},
            **{key: _model_values(item) for key, item in (value.__pydantic_extra__ or {}).items()},
        }
    elif isinstance(value, dict):
        return {key: _model_values(item) for key, item in value.items()}
    elif isinstance(value, tuple):
        return tuple(_model_values(item) for item in value)
    elif isinstance(value, list):
        return [_model_values(item) for item in value]
    return value


def _operational_bytes(model: BaseModel) -> bytes:
    """Preserve the exact validated model, including finite float-valued feed extras.

    This is a private Python JSON snapshot, NOT the public policy canonical profile
    or a claim to retain original transport whitespace/bytes.
    """
    _model_values(model)
    return json.dumps(
        model.model_dump(mode="json", warnings="error"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class SqliteD1ProvenanceStore:
    @_safe
    def __init__(self, connection: sqlite3.Connection, keys: ProvenanceKeys):
        self.connection = connection
        self._keys = ProvenanceKeys.model_validate(keys)
        self._cipher = AESGCM(self._keys.encryption_key.get_secret_value())

    def _mac(self, domain: str, value: bytes) -> str:
        return hmac.new(
            self._keys.pseudonym_key.get_secret_value(),
            b"commerce-d1-provenance/v1\0" + domain.encode() + b"\0" + value,
            hashlib.sha256,
        ).hexdigest()

    def _transaction(self) -> None:
        if (
            not self.connection.in_transaction
            or self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        ):
            raise GovernanceError("GOV_EVIDENCE_UNAVAILABLE")
        row = self.connection.execute("SELECT key_check FROM d1_provenance_schema").fetchone()
        if row is None or not hmac.compare_digest(row[0], self._key_check()):
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")

    def _key_check(self) -> str:
        return self._mac("key-check", self._keys.encryption_key.get_secret_value())

    @_safe
    def initialize(self) -> None:
        if (
            not self.connection.in_transaction
            or self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        ):
            raise GovernanceError("GOV_EVIDENCE_UNAVAILABLE")
        # Purchases must already have the real revision migration from EC-GOV-05A.
        self.connection.execute("SELECT run_id, owner_id, state_version FROM purchases LIMIT 0")
        for statement in (
            """CREATE TABLE IF NOT EXISTS d1_provenance_schema (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            version INTEGER NOT NULL CHECK(version=1), key_id TEXT NOT NULL,
            key_check TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS d1_provenance_episodes (
            run_id TEXT PRIMARY KEY REFERENCES purchases(run_id), owner_ref TEXT NOT NULL,
            trace_id TEXT NOT NULL UNIQUE, conversation_id TEXT, experiment_run_id TEXT,
            UNIQUE(run_id, owner_ref))""",
            """CREATE TABLE IF NOT EXISTS d1_provenance_sources (
            source_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>=1),
            run_id TEXT NOT NULL, owner_ref TEXT NOT NULL, kind TEXT NOT NULL,
            digest TEXT NOT NULL, public_body BLOB NOT NULL,
            nonce BLOB NOT NULL, ciphertext BLOB NOT NULL,
            PRIMARY KEY(source_id, version),
            FOREIGN KEY(run_id, owner_ref) REFERENCES d1_provenance_episodes(run_id, owner_ref))""",
            """CREATE TABLE IF NOT EXISTS d1_provenance_policies (
            policy_set_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>=1),
            artifact_digest TEXT NOT NULL, payload BLOB NOT NULL,
            PRIMARY KEY(policy_set_id, version))""",
        ):
            self.connection.execute(statement)
        row = self.connection.execute(
            "SELECT version, key_id, key_check FROM d1_provenance_schema"
        ).fetchall()
        expected = (1, self._keys.key_id, self._key_check())
        if not row:
            self.connection.execute("INSERT INTO d1_provenance_schema VALUES (1,?,?,?)", expected)
        elif len(row) != 1 or tuple(row[0]) != expected:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        for table in (
            "d1_provenance_schema",
            "d1_provenance_episodes",
            "d1_provenance_sources",
            "d1_provenance_policies",
        ):
            for action in ("UPDATE", "DELETE"):
                self.connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'd1_provenance_append_only'); END"
                )
            key = (
                "singleton"
                if table.endswith("schema")
                else (
                    "run_id"
                    if table.endswith("episodes")
                    else "policy_set_id"
                    if table.endswith("policies")
                    else "source_id"
                )
            )
            version = " AND version=NEW.version" if table.endswith(("sources", "policies")) else ""
            self.connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} "
                f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {key}=NEW.{key}{version}) "
                "BEGIN SELECT RAISE(ABORT, 'd1_provenance_identity_conflict'); END"
            )

    def _owner(self, run_id: str, authenticated_owner_id: str) -> str:
        self._transaction()
        _ID.validate_python(run_id, strict=True)
        row = self.connection.execute(
            "SELECT owner_id FROM purchases WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or not hmac.compare_digest(
            str(row[0]).encode("utf-8"), authenticated_owner_id.encode("utf-8")
        ):
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        return "owner:" + self._mac("owner", authenticated_owner_id.encode("utf-8"))

    def _episode(self, run_id: str, owner_id: str) -> tuple[Any, ...]:
        owner_ref = self._owner(run_id, owner_id)
        row = self.connection.execute(
            "SELECT owner_ref,trace_id,conversation_id,experiment_run_id "
            "FROM d1_provenance_episodes WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None or row[0] != owner_ref:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        return tuple(row)

    @_safe
    def register_episode(
        self, run_id: str, authenticated_owner_id: str, *, experiment_run_id: str | None
    ) -> str:
        """Backend must supply the verified principal, never an LLM/body owner claim."""
        owner_ref = self._owner(run_id, authenticated_owner_id)
        if experiment_run_id is not None:
            _ID.validate_python(experiment_run_id, strict=True)
        thread = self.connection.execute(
            "SELECT thread_id FROM purchase_graph_threads WHERE run_id=?", (run_id,)
        ).fetchone()
        conversation = (
            None
            if thread is None
            else "conversation:"
            + self._mac("conversation", canonical_bytes([owner_ref, thread[0]]))
        )
        old = self.connection.execute(
            "SELECT owner_ref,trace_id,conversation_id,experiment_run_id "
            "FROM d1_provenance_episodes WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if old is not None:
            if (old[0], old[2], old[3]) != (owner_ref, conversation, experiment_run_id):
                raise GovernanceError("GOV_STATE_CONFLICT")
            return str(old[1])
        trace_id = "trace:" + secrets.token_hex(16)
        self.connection.execute(
            "INSERT INTO d1_provenance_episodes VALUES (?,?,?,?,?)",
            (run_id, owner_ref, trace_id, conversation, experiment_run_id),
        )
        return trace_id

    def _register(
        self,
        run_id: str,
        owner_id: str,
        kind: Kind,
        payload: bytes,
        *,
        public: bool = False,
        slot: str = "current",
        version: int | None = None,
    ) -> ObjectRef:
        episode = self._episode(run_id, owner_id)
        owner_ref = episode[0]
        source_id = "source:" + self._mac(
            "source-id", canonical_bytes([run_id, owner_ref, kind, slot])
        )
        public_body = (
            payload
            if public
            else canonical_bytes(
                {
                    "schema_version": "d1-private-snapshot-binding/1",
                    "kind": kind,
                    "key_id": self._keys.key_id,
                    "scope": source_id,
                    "private_binding": self._mac(
                        "private-content", canonical_bytes([source_id, payload.hex()])
                    ),
                }
            )
        )
        digest = sha256_digest(public_body)
        old = self.connection.execute(
            "SELECT version,digest FROM d1_provenance_sources WHERE source_id=? "
            "ORDER BY version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if old is not None:
            previous = ObjectRef(schema_version=SCHEMA, id=source_id, version=old[0], digest=old[1])
            self._get(run_id, owner_id, previous, kind)
        if old is not None and old[1] == digest:
            if version is not None and version != old[0]:
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            ref = ObjectRef(schema_version=SCHEMA, id=source_id, version=old[0], digest=digest)
            return ref
        if version is None:
            version = 1 if old is None else int(old[0]) + 1
        elif type(version) is not int or version < 1 or (old and version <= old[0]):
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        ref = ObjectRef(schema_version=SCHEMA, id=source_id, version=version, digest=digest)
        nonce = secrets.token_bytes(12)
        aad = canonical_bytes([run_id, owner_ref, kind, ref.model_dump(mode="json")])
        ciphertext = self._cipher.encrypt(nonce, payload, aad)
        self.connection.execute(
            "INSERT INTO d1_provenance_sources VALUES (?,?,?,?,?,?,?,?,?)",
            (source_id, version, run_id, owner_ref, kind, digest, public_body, nonce, ciphertext),
        )
        return ref

    def _get(self, run_id: str, owner_id: str, ref: ObjectRef, kind: Kind) -> tuple[bytes, bytes]:
        ref = ObjectRef.model_validate(ref)
        owner_ref = self._episode(run_id, owner_id)[0]
        row = self.connection.execute(
            "SELECT digest,public_body,nonce,ciphertext FROM d1_provenance_sources "
            "WHERE source_id=? AND version=? AND run_id=? AND owner_ref=? AND kind=?",
            (ref.id, ref.version, run_id, owner_ref, kind),
        ).fetchone()
        if row is None or row[0] != ref.digest or sha256_digest(row[1]) != ref.digest:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        payload = self._cipher.decrypt(
            row[2], row[3], canonical_bytes([run_id, owner_ref, kind, ref.model_dump(mode="json")])
        )
        if row[1] != payload:
            public = json.loads(row[1])
            expected = self._mac("private-content", canonical_bytes([ref.id, payload.hex()]))
            if public.get("private_binding") != expected or public.get("scope") != ref.id:
                raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        return bytes(payload), bytes(row[1])

    @_safe
    def public_source(self, run_id: str, owner_id: str, ref: ObjectRef, kind: Kind) -> bytes:
        """Authorized resolution; private raw payloads are never returned by this API."""
        return self._get(run_id, owner_id, ref, kind)[1]

    @_safe
    def register_intent(self, run_id: str, owner_id: str, intent: PurchaseIntent) -> ObjectRef:
        validated = PurchaseIntent.model_validate(_model_values(intent))
        payload = _operational_bytes(intent)
        if _operational_bytes(validated) != payload:
            raise GovernanceError("GOV_INPUT_INVALID")
        return self._register(run_id, owner_id, "intent", payload)

    @_safe
    def recover_intent(self, run_id: str, owner_id: str, ref: ObjectRef) -> PurchaseIntent:
        return PurchaseIntent.model_validate_json(self._get(run_id, owner_id, ref, "intent")[0])

    @_safe
    def register_feed(self, run_id: str, owner_id: str, feed: ProductFeed) -> ObjectRef:
        validated = ProductFeed.model_validate(_model_values(feed))
        payload = _operational_bytes(feed)
        if _operational_bytes(validated) != payload:
            raise GovernanceError("GOV_INPUT_INVALID")
        return self._register(run_id, owner_id, "feed", payload)

    @_safe
    def recover_feed(self, run_id: str, owner_id: str, ref: ObjectRef) -> ProductFeed:
        return ProductFeed.model_validate_json(self._get(run_id, owner_id, ref, "feed")[0])

    @_safe
    def register_context(
        self,
        run_id: str,
        owner_id: str,
        configuration: D1Configuration | D1EnforceConfiguration,
        *,
        policy_bytes: bytes | None = None,
        policy_binding: PolicyBinding | None = None,
    ) -> RegisteredContext:
        self._episode(run_id, owner_id)
        configuration = TypeAdapter(D1Configuration | D1EnforceConfiguration).validate_python(
            configuration, strict=True
        )
        # Mode belongs to the episode, not to a replaceable snapshot. This also
        # covers a shadow context registered before its first ledger observation.
        for source_id, version, digest in self.connection.execute(
            "SELECT source_id,version,digest FROM d1_provenance_sources "
            "WHERE run_id=? AND kind='context'",
            (run_id,),
        ).fetchall():
            ref = ObjectRef(schema_version=SCHEMA, id=source_id, version=version, digest=digest)
            previous = parse_model(ContextData, self._get(run_id, owner_id, ref, "context")[0])
            if previous.governance_mode != configuration.governance_mode:
                raise GovernanceError("GOV_INPUT_INVALID")
        measured = measure_d1_sources()
        algorithm = self._register(
            run_id, owner_id, "algorithm", measured["algorithm"], public=True
        )
        normalization = self._register(
            run_id, owner_id, "normalization", measured["normalization"], public=True
        )
        clock = self._register(
            run_id, owner_id, "clock", canonical_bytes(configuration.clock), public=True
        )
        commercial = self._register(run_id, owner_id, "commercial", canonical_bytes(configuration))
        merchant_ref = "merchant:" + self._mac(
            "merchant-origin", configuration.merchant_base_url.encode()
        )
        payload, binding = build_d1_policy(
            policy_set_id=configuration.policy_set_id,
            version=configuration.policy_revision,
            merchant_ref=merchant_ref,
        )
        if policy_bytes is not None or policy_binding is not None:
            if policy_bytes is None or policy_binding is None:
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            artifact = verify_artifact(policy_bytes, policy_binding)
            if (artifact.policy_set_id, artifact.version) != (
                configuration.policy_set_id,
                configuration.policy_revision,
            ) or any(action != "prepare_checkout" for action in artifact.allowed_actions):
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            payload, binding = policy_bytes, PolicyBinding.model_validate(policy_binding)
        known = self.connection.execute(
            "SELECT artifact_digest,payload FROM d1_provenance_policies "
            "WHERE policy_set_id=? AND version=?",
            (binding.policy_set_id, binding.version),
        ).fetchone()
        if known is not None:
            if tuple(known) != (binding.artifact_digest, payload):
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        else:
            latest = self.connection.execute(
                "SELECT MAX(version) FROM d1_provenance_policies WHERE policy_set_id=?",
                (binding.policy_set_id,),
            ).fetchone()[0]
            if latest is not None and binding.version <= latest:
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            self.connection.execute(
                "INSERT INTO d1_provenance_policies VALUES (?,?,?,?)",
                (binding.policy_set_id, binding.version, binding.artifact_digest, payload),
            )
        policy = self._register(
            run_id,
            owner_id,
            "policy",
            payload,
            public=True,
            slot=configuration.policy_set_id,
            version=binding.version,
        )
        episode = self._episode(run_id, owner_id)
        data = ContextData(
            run_id=run_id,
            owner_ref=episode[0],
            trace_id=episode[1],
            conversation_id=episode[2],
            experiment_run_id=episode[3],
            purchase_mode="HP",
            governance_mode=configuration.governance_mode,
            merchant_ref=merchant_ref,
            commercial_ref=commercial,
            clock_profile_ref=clock,
            normalization_ref=normalization,
            eligibility_algorithm_ref=algorithm,
            policy_artifact_ref=policy,
            expected_policy=binding,
        )
        ref = self._register(run_id, owner_id, "context", canonical_bytes(data), public=True)
        return RegisteredContext(ref=ref, data=data)

    @_safe
    def recover_context(self, run_id: str, owner_id: str, ref: ObjectRef) -> RegisteredContext:
        data = parse_model(ContextData, self._get(run_id, owner_id, ref, "context")[0])
        if data.run_id != run_id or data.owner_ref != self._episode(run_id, owner_id)[0]:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        sources: tuple[tuple[ObjectRef, Kind], ...] = (
            (data.commercial_ref, "commercial"),
            (data.clock_profile_ref, "clock"),
            (data.normalization_ref, "normalization"),
            (data.eligibility_algorithm_ref, "algorithm"),
        )
        for source, kind in sources:
            self._get(run_id, owner_id, source, kind)
        policy = self._get(run_id, owner_id, data.policy_artifact_ref, "policy")[0]
        artifact = verify_artifact(policy, data.expected_policy)
        registered = self.connection.execute(
            "SELECT artifact_digest,payload FROM d1_provenance_policies "
            "WHERE policy_set_id=? AND version=?",
            (data.expected_policy.policy_set_id, data.expected_policy.version),
        ).fetchone()
        if registered is None or tuple(registered) != (
            data.expected_policy.artifact_digest,
            policy,
        ):
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        # A verified restrictive D1 artifact may deny this configured merchant/action.
        # That is an observed POL-02 denial, not an incompatible artifact or a permission.
        if any(action != "prepare_checkout" for action in artifact.allowed_actions):
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        return RegisteredContext(ref=ref, data=data)

    @_safe
    def observe_context(
        self, run_id: str, owner_id: str, ref: ObjectRef, clocks: ClockReader
    ) -> ContextObservation:
        context = self.recover_context(run_id, owner_id, ref)
        profile = parse_model(
            ClockProfile, self._get(run_id, owner_id, context.data.clock_profile_ref, "clock")[0]
        )
        if profile != clocks.profile:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        version = self.connection.execute(
            "SELECT state_version FROM purchases WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        return ContextObservation(
            context=context, expected_local_state_version=version, clocks=clocks.read()
        )

    @_safe
    def intent_facts(
        self, run_id: str, owner_id: str, intent_ref: ObjectRef, context_ref: ObjectRef
    ) -> IntentFacts:
        context = self.recover_context(run_id, owner_id, context_ref)
        intent = self.recover_intent(run_id, owner_id, intent_ref)
        if intent.hard.currency not in (None, "COP"):
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        refs: set[str] = set()
        constraints: list[tuple[str, Any]] = [
            (kind, value)
            for kind, values in (
                ("category", intent.hard.categories),
                ("condition", intent.hard.conditions),
                ("term", intent.hard.required_terms),
            )
            for value in values
        ]
        constraints.extend(
            ("option", item.model_dump(mode="json")) for item in intent.hard.required_options
        )
        for kind, value in constraints:
            body = canonical_bytes({"kind": kind, "value": value})
            slot = self._mac("constraint-identity", body)
            constraint = self._register(run_id, owner_id, "constraint", body, slot=slot)
            refs.add(constraint.id)
        normalization = context.data.normalization_ref
        return IntentFacts(
            schema_version=SCHEMA,
            intent_ref=intent_ref,
            currency="COP",
            max_unit_minor=intent.hard.max_unit_amount_minor,
            max_total_minor=intent.hard.max_total_amount_minor,
            quantity=1,
            hard_constraint_refs=tuple(sorted(refs)),
            normalization_version=f"{normalization.id}:v{normalization.version}",
        )

    @_safe
    def recover_constraint(self, run_id: str, owner_id: str, constraint_id: str) -> dict[str, Any]:
        self._episode(run_id, owner_id)
        row = self.connection.execute(
            "SELECT version,digest FROM d1_provenance_sources WHERE source_id=? AND run_id=? "
            "AND kind='constraint'",
            (constraint_id, run_id),
        ).fetchall()
        if len(row) != 1:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        ref = ObjectRef(
            schema_version=SCHEMA, id=constraint_id, version=row[0][0], digest=row[0][1]
        )
        return json.loads(self._get(run_id, owner_id, ref, "constraint")[0])
