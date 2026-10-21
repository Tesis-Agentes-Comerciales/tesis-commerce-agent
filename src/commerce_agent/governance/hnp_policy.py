"""Policy set `b2-hnp-v1` of Human Not Present purchases (docs/design/hnp-governed.md §7).

Built in code, never from `AGENT_D1_POLICY_*`: every HNP decision evaluates it. Revision 1 held
only the `(D1, HNP)` row; revision 2 adds `authorization` (D2-HNP) and `dispatch` (D3-HNP). The
provenance vault refuses new bytes under a known revision, so the sections came with a new one.
"""

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from commerce_agent.delegation.models import EXECUTOR_REF

from .canonical import canonical_bytes, sha256_digest
from .evaluator import GovernanceError, verify_artifact
from .models import (
    APPLICABLE_RULES,
    HNP_POLICY_SET_ID,
    SCHEMA,
    D1PolicyArtifact,
    DispatchRules,
    HnpAuthorizationRules,
    Id,
    Ids,
    PolicyBinding,
)
from .provenance import D1EnforceConfiguration

if TYPE_CHECKING:
    from commerce_agent.config import Settings

    from .bootstrap import ConfiguredD1

HNP_POLICY_REVISION = 2
# K6: the `ObjectRef.id` of the AP2 HNP authorization profile (`ap2_hnp.PROFILE_ID`, which is not
# an `Id`), the one `b2-hnp-v1` admits for the open and closed mandate proofs.
HNP_AUTHORIZATION_PROFILE_ID = "profile:ap2-hnp-v1"
# D1-HNP eligibility (§3.3): rank_candidates over the feed, restricted to the grant's variants.
GRANT_SCOPE_ELIGIBILITY = "rank_candidates+grant_scope/v1"
# The grant authorizes the whole purchase (§7, «Hechos del executor»).
GRANT_ACTIONS = ("prepare_checkout", "complete_purchase", "commit_dispatch")


class HnpGrant(BaseModel):
    """What D1-HNP needs of the grant an autonomous purchase executes: identifiers only.

    The executor reads it from the delegation ledger, never from the model.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    grant_id: Id
    variant_ids: Annotated[Ids, Field(min_length=1), AfterValidator(lambda ids: tuple(sorted(ids)))]


def executor_principal(*, owns_run: bool) -> dict[str, Any]:
    """`PrincipalFacts` of the executor, the service acting under the grant (§7). D2-HNP evaluates
    them (POL-03) and D3-HNP carries them; D1-HNP records them only for attribution (§9.1).
    `owns_run`: the grant's owner is the run's owner."""
    return {
        "schema_version": SCHEMA,
        "principal_ref": EXECUTOR_REF,
        "authentication_basis": "service_and_grant",
        "authentication_verified": True,
        "owns_run": owns_run,
        "permitted_actions": list(GRANT_ACTIONS),
    }


def build_hnp_policy(*, merchant_ref: str | tuple[str, ...]) -> tuple[bytes, PolicyBinding]:
    """The `b2-hnp-v1` artifact for the allowed merchants, as `build_d1_policy` does for HP."""
    artifact = D1PolicyArtifact(
        schema_version=SCHEMA,
        policy_set_id=HNP_POLICY_SET_ID,
        version=HNP_POLICY_REVISION,
        policy_ids=APPLICABLE_RULES[("D1", "HNP")],
        allowed_actions=("prepare_checkout",),
        allowed_merchant_refs=(merchant_ref,) if isinstance(merchant_ref, str) else merchant_ref,
        currency="COP",
        quantity=1,
        authorization=HnpAuthorizationRules(
            schema_version=SCHEMA,
            policy_ids=APPLICABLE_RULES[("D2", "HNP")],
            allowed_actions=("complete_purchase",),
            authentication_bases=("service_and_grant",),
            profiles=(HNP_AUTHORIZATION_PROFILE_ID,),
        ),
        dispatch=DispatchRules(
            schema_version=SCHEMA,
            policy_ids=APPLICABLE_RULES[("D3", "HNP")],
            allowed_actions=("commit_dispatch",),
        ),
    )
    payload = canonical_bytes(artifact)
    binding = PolicyBinding(
        schema_version=SCHEMA,
        policy_set_id=HNP_POLICY_SET_ID,
        version=HNP_POLICY_REVISION,
        artifact_digest=sha256_digest(payload),
    )
    verify_artifact(payload, binding)
    return payload, binding


def operator_merchant_refs(
    settings: "Settings", configured: "ConfiguredD1"
) -> tuple[str, ...] | None:
    """The merchants POL-02 of `b2-hnp-v1` allows (K11): the operator's list that POL-02 of HP
    evaluates, `AGENT_D1_ALLOWED_MERCHANT_ORIGINS` as `configured_d1` already turned it into
    merchant refs. Without that list, `None`: the D1-HNP gate allows the configured merchant."""
    if settings.agent_d1_allowed_merchant_origins is None:
        return None
    if configured.policy_bytes is None or configured.policy_binding is None:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    return verify_artifact(configured.policy_bytes, configured.policy_binding).allowed_merchant_refs


def hnp_d1_configuration(configuration: D1EnforceConfiguration) -> D1EnforceConfiguration:
    """The enforce configuration bound to `b2-hnp-v1`: same merchant, clocks and units."""
    return D1EnforceConfiguration.model_validate(
        configuration.model_copy(
            update={"policy_set_id": HNP_POLICY_SET_ID, "policy_revision": HNP_POLICY_REVISION}
        )
    )


def grant_scope_eligibility(measured: bytes, grant: HnpGrant) -> bytes:
    """The eligibility source of an HNP episode: the measured selection sources plus the grant's
    restriction, so the evidence alone recomputes `eligible_variant_ids`."""
    return canonical_bytes(
        {
            **json.loads(measured),
            "eligibility_profile": GRANT_SCOPE_ELIGIBILITY,
            "grant_id": grant.grant_id,
            "variant_ids": list(grant.variant_ids),
        }
    )


def registered_grant(source: bytes) -> HnpGrant:
    """The grant restriction a registered HNP eligibility source names."""
    try:
        body = json.loads(source)
        if body["eligibility_profile"] != GRANT_SCOPE_ELIGIBILITY:
            raise ValueError("Not a grant scope eligibility")
        return HnpGrant(grant_id=body["grant_id"], variant_ids=tuple(body["variant_ids"]))
    except Exception:
        raise GovernanceError("GOV_FACTS_UNAVAILABLE") from None


def grant_shortlist(ranked: Iterable[str], grant: HnpGrant) -> tuple[str, ...]:
    """`eligible_variant_ids` of D1-HNP: the ranked variants the grant names, sorted."""
    return tuple(sorted(set(ranked) & set(grant.variant_ids)))
