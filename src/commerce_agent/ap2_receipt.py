"""The AP2 receipt profile both verifying clients share (docs/ap2-profile.md, Recibos, Errores).

A leaf: only the AP2 SDK and the standard library, so `delegated_payment` and `acp.client` import
it in any order without a cycle through `commerce_agent.acp` (tests/test_ap2_receipt.py).
"""

from ap2.sdk.sdjwt import common

# The AP2 error codes a Human Not Present error receipt may carry (docs/ap2-profile.md, Errores).
AP2_ERRORS = frozenset(
    {"invalid_credential", "invalid_mandate", "unresolved_constraint", "mandates_not_supported"}
)


def chain_reference(mandate: str) -> str:
    """The `reference` of a receipt: `sd_hash` of the mandate, or of the last segment of an
    `open~~closed` chain (docs/ap2-profile.md, Recibos)."""
    return common.compute_sd_hash(common.parse_token(mandate.rsplit("~~", 1)[-1]))
