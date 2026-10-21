"""The keyed destination binding that D2 and the delegation scope share (hnp-governed.md §4.2, K8).

D2-HP binds the fulfillment details of the authoritative checkout; the delegation API binds the
ones the buyer consents to. Both use this function, so POL-08 compares `(scheme, key_id, value)`
of one space: same vault key, same `destination` domain, same canonical bytes.
"""

from typing import Any

from .canonical import canonical_bytes
from .models import SCHEMA
from .provenance import ProvenanceKeys, pseudonym


def destination_binding(
    keys: ProvenanceKeys, binding_id: str, fulfillment_details: Any
) -> dict[str, Any]:
    """`PrivateBinding` (hmac-sha256/v1) of a destination, never the destination itself."""
    return {
        "schema_version": SCHEMA,
        "binding_id": binding_id,
        "scheme": "hmac-sha256/v1",
        "key_id": keys.key_id,
        "value": pseudonym(keys, "destination", canonical_bytes(fulfillment_details)),
    }
