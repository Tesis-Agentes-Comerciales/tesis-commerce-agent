"""07B-B: the guarded D1 dispatch, parked at its cutpoints."""

import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.authority_observation import AuthorityObservationClient, AuthoritySession
from commerce_agent.acp.guarded_creation import HttpGuardedCheckout
from commerce_agent.acp.offer_snapshot import DeliveryContext
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.models import SCHEMA, D1PolicyArtifact, PolicyBinding
from commerce_agent.governance.preselection import PreselectionAuthority
from commerce_agent.governance.preselection_d1 import PreselectionD1Request
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    D1Configuration,
    D1EnforceConfiguration,
    ProvenanceKeys,
)
from commerce_agent.governance.provisioning import CatalogSource, EpisodeGrant
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.guarded_d1_sqlite import SqliteGuardedD1
from commerce_agent.persistence.preselection_d1_sqlite import SqlitePreselectionD1
from commerce_agent.persistence.preselection_sqlite import SqlitePreselectionStore
from commerce_agent.persistence.provisioning_sqlite import SqliteTestProvisioningRegistry
from commerce_evaluation.stack.agent_worker.runtime import ROOT, halt

POLICY_SET = "b-live-policy"


class CutpointTransport(HttpGuardedCheckout):
    """The guarded HTTP port, parked before the send or after the Merchant's answer."""

    cutpoint: str | None = None

    def create_once(self, guard, legacy):
        if self.cutpoint == "claimed_before_http_send":
            halt(self.cutpoint)
        observation = super().create_once(guard, legacy)
        if self.cutpoint == "response_before_record":
            halt(self.cutpoint)
        return observation


class Guarded:
    """07B-B: authoritative P, Q shadow, fresh applied D1 and one guarded D17 POST."""

    def __init__(self, data: dict[str, Any], *, fresh: bool):
        root = Path(data["directory"]).resolve()
        assert root.is_relative_to(ROOT)
        root.mkdir(parents=True, exist_ok=True)
        seed = bytes.fromhex(data["ephemeral_secret"])

        def key(domain: str) -> SecretBytes:
            return SecretBytes(hashlib.sha256(seed + domain.encode()).digest())

        origin = data["origin"]
        source = SqlitePreselectionStore(root / "source.sqlite", key("p"))
        registry = SqliteTestProvisioningRegistry(root / "registry.sqlite", key("registry"))
        profile = ClockProfile(
            authority_source="system_utc",
            commercial_source="system_utc",
            authority_provider=None,
            commercial_provider=None,
            fixed_commercial_at=None,
        )
        common = dict(
            merchant_base_url=origin,
            policy_set_id=POLICY_SET,
            policy_revision=1,
            currency="COP",
            quantity=1,
            clock=profile,
        )
        keys = ProvenanceKeys(
            key_id="b-live", pseudonym_key=key("pseudonym"), encryption_key=key("encryption")
        )
        self.shadow = SqlitePreselectionD1(
            root / "shadow.sqlite",
            source=source,
            registry=registry,
            bearer=SecretStr(data["token"]),
            custody_key=key("q"),
            keys=keys,
            configuration=D1Configuration.model_validate({**common, "governance_mode": "shadow"}),
            clocks=ClockReader(profile),
        )
        self.shadow_binding = None
        if fresh:
            source.initialize()
            registry.initialize()
            session = registry.issue_test_session(
                CatalogSource(
                    event_id=str(uuid4()),
                    merchant_origin=origin,
                    merchant_id=data["merchant_id"],
                    remote_run_id=data["run_id"],
                    remote_actor_id=data["actor_id"],
                ),
                SecretStr(data["token"]),
            )
            observation = AuthorityObservationClient(session)
            try:
                flow = PreselectionAuthority(observation, source)
                episode = flow.begin(
                    owner_id="b-live-owner",
                    intent=PurchaseIntent(),
                    destination=DeliveryContext(country="CO", city="Bogota", postal_code="110111"),
                )
                seal = flow.capture(episode, owner_id="b-live-owner")
            finally:
                observation.close()
            store = self.shadow.initialize()
            registry.grant_test_episode(
                EpisodeGrant(
                    event_id=str(uuid4()),
                    episode_id=episode,
                    owner_id=seal.episode.owner_id,
                    d1_run_id=episode,
                    d1_store_id=store,
                    selection_digest=sha256_digest(canonical_bytes(seal)),
                    receipt_ref=session.binding.source_ref,
                )
            )
            self.request = self.shadow.request(episode, seal.episode.owner_id)
            self.shadow_binding = self.shadow.evaluate(self.request).model_dump(mode="json")
            (root / "request.json").write_bytes(canonical_bytes(self.request))
        else:
            self.request = parse_model(PreselectionD1Request, (root / "request.json").read_bytes())
            seal = source.recover(self.request.episode_id, self.request.owner_id)
            session = AuthoritySession(seal.episode.binding, SecretStr(data["token"]))
        self.transport = CutpointTransport(session, timeout_seconds=data["timeout"])
        policy: dict[str, Any] = {}
        if data["policy"] == "deny_foreign_merchant":
            # I05/I06: a trusted constructor policy whose allowlist excludes this Merchant.
            raw = canonical_bytes(
                D1PolicyArtifact(
                    schema_version=SCHEMA,
                    policy_set_id=POLICY_SET,
                    version=1,
                    policy_ids=("POL-01", "POL-02"),
                    allowed_actions=("prepare_checkout",),
                    allowed_merchant_refs=("merchant:forbidden",),
                    currency="COP",
                    quantity=1,
                )
            )
            policy = dict(
                policy_bytes=raw,
                policy_binding=PolicyBinding(
                    schema_version=SCHEMA,
                    policy_set_id=POLICY_SET,
                    version=1,
                    artifact_digest=sha256_digest(raw),
                ),
            )
        self.applied = SqliteGuardedD1(
            root / "applied.sqlite",
            shadow=self.shadow,
            custody_key=key("b"),
            keys=keys,
            configuration=D1EnforceConfiguration.model_validate(
                {**common, "governance_mode": "enforce"}
            ),
            clocks=ClockReader(profile),
            transport=self.transport,
            **policy,
        )
        self.applied.initialize()

    def opened(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"pid": os.getpid(), "run_id": self.request.run_id, "shadow": self.shadow_binding}

    def prepare(self, _: dict[str, Any]) -> dict[str, Any]:
        return self.applied.prepare(self.request).model_dump(mode="json")

    def dispatch(self, value: dict[str, Any]) -> dict[str, Any]:
        self.transport.cutpoint = value.get("cutpoint")
        return self.applied.dispatch(self.request).model_dump(mode="json")

    def recover(self, _: dict[str, Any]) -> dict[str, Any]:
        return self.applied.recover(self.request).model_dump(mode="json")

    def history(self, _: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self.applied.history(self.request))

    def decisions(self, _: dict[str, Any]) -> list[dict[str, Any]]:
        """Applied D1 ledger for this run; read-only."""
        with closing(sqlite3.connect(self.applied.path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            records = SqliteGovernanceEvidenceStore(db).for_run(self.request.run_id)
        return [
            {
                "outcome": record.decision.outcome,
                "governance_mode": record.policy_input().governance_mode,
                "disposition": record.application.disposition,
            }
            for record in records
            if isinstance(record, DecisionRecord)
        ]

    def close(self) -> None:
        self.transport.close()
