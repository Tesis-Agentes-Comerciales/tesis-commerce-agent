"""Test-backend receipts, not remote signatures or production authentication."""

from typing import Literal

from commerce_agent.acp.authority_observation import CatalogBinding
from commerce_agent.acp.offer_snapshot import SnapshotModel
from commerce_agent.governance.models import ObjectRef
from commerce_agent.governance.remote_creation import SessionScope


class CatalogSource(SnapshotModel):
    schema_version: Literal["commerce-test-catalog-source/1"] = "commerce-test-catalog-source/1"
    event_id: str
    issuer_profile: Literal["test_backend_issuer/1"] = "test_backend_issuer/1"
    merchant_origin: str
    merchant_id: str
    remote_run_id: str
    remote_actor_id: str
    catalog_kind: Literal["merchant"] = "merchant"
    feed_id: Literal["feed_merchant_catalog"] = "feed_merchant_catalog"
    deployment_attestation: None = None


class CredentialIssuance(SnapshotModel):
    schema_version: Literal["commerce-test-credential-issuance/1"] = (
        "commerce-test-credential-issuance/1"
    )
    event_id: str
    catalog_source_ref: ObjectRef
    issued_at: str


class CatalogReceipt(SnapshotModel):
    schema_version: Literal["commerce-test-catalog-receipt/1"] = "commerce-test-catalog-receipt/1"
    event_id: str
    source: CatalogSource
    source_ref: ObjectRef
    credential_ref: ObjectRef

    def binding(self, ref: ObjectRef) -> CatalogBinding:
        return CatalogBinding(
            assurance="backend_managed_catalog_registry/1",
            merchant_origin=self.source.merchant_origin,
            merchant_id=self.source.merchant_id,
            catalog_kind=self.source.catalog_kind,
            feed_id=self.source.feed_id,
            source_ref=ref,
            scope=SessionScope(
                assurance="backend_session_registry/1",
                remote_run_id=self.source.remote_run_id,
                remote_actor_id=self.source.remote_actor_id,
                source_ref=self.credential_ref,
            ),
        )


class EpisodeGrant(SnapshotModel):
    schema_version: Literal["commerce-test-episode-grant/1"] = "commerce-test-episode-grant/1"
    event_id: str
    episode_id: str
    owner_id: str
    d1_run_id: str
    d1_store_id: str
    selection_digest: str
    receipt_ref: ObjectRef


class ProvisioningResolution(SnapshotModel):
    schema_version: Literal["commerce-provisioning-resolution/1"] = (
        "commerce-provisioning-resolution/1"
    )
    assurance: Literal["test_backend_receipt_not_remote_authentication"] = (
        "test_backend_receipt_not_remote_authentication"
    )
    receipt_ref: ObjectRef
    credential_ref: ObjectRef
    catalog_source_ref: ObjectRef
    grant_ref: ObjectRef
    verified_at: str
    deployment_attestation: None = None
