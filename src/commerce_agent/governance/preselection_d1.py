"""Auxiliary links to committed shadow D1. No reservation or commercial operation."""

from typing import Literal

from commerce_agent.acp.authority_observation import CatalogBinding
from commerce_agent.acp.offer_snapshot import SnapshotModel
from commerce_agent.governance.models import ObjectRef, PolicyBinding
from commerce_agent.governance.provisioning import ProvisioningResolution


class PreselectionD1Request(SnapshotModel):
    schema_version: Literal["commerce-preselection-d1-request/1"] = (
        "commerce-preselection-d1-request/1"
    )
    episode_id: str
    run_id: str
    owner_id: str
    seal_digest: str
    feed_digest: str
    intent_digest: str
    selected_variant_id: str


class PreselectionD1Binding(SnapshotModel):
    schema_version: Literal["commerce-preselection-d1-binding/1"] = (
        "commerce-preselection-d1-binding/1"
    )
    evaluation_id: str
    store_id: str
    request: PreselectionD1Request
    episode_digest: str
    observation_id: str
    observation_digest: str
    offer_id: str
    offer_revision: int
    offer_snapshot_profile: Literal["merchant-offer-snapshot/1"] = "merchant-offer-snapshot/1"
    offer_snapshot_sha256: str
    merchant: CatalogBinding
    provisioning: ProvisioningResolution
    context_ref: ObjectRef
    feed_snapshot_ref: ObjectRef
    intent_ref: ObjectRef
    eligibility_algorithm_ref: ObjectRef
    policy_binding: PolicyBinding
    policy_artifact_ref: ObjectRef
    engine_artifact_ref: ObjectRef
    decision_id: str
    evidence_id: str
    evidence_digest: str
    outcome: Literal["allow", "deny"]
    governance_mode: Literal["shadow"] = "shadow"
    application: Literal["observed_shadow"] = "observed_shadow"
    operation_id: None = None
    dispatch_capability: Literal[False] = False
    bound_at: str
