"""AP2 Human Not Present chains and the receipts their verifiers sign (docs/ap2-profile.md).

The chains are the product's: open mandates from `OpenMandateIssuer` and `open~~closed` chains from
`AgentKeyCloser`, with keys generated for the test. The receipts are signed as the PSP (HNP-P1,
one JWKS key, no `kid`) and the merchant (HNP-M1, `kid` of its JWKS) sign them; `reference` is the
`sd_hash` of the chain's last segment, and `iat` the real clock, which PyJWT checks.
"""

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import jwt
from ap2.sdk.sdjwt import common
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.ap2_hnp import (
    AcceptableItem,
    AgentKeyCloser,
    MandatePair,
    OpenMandateIssuer,
)
from tests.support.ap2_hnp_fixtures import (
    MERCHANT_ISSUER,
    checkout_jwt,
    merchant_public_jwk,
    private_key_pem,
    public_jwk,
)

PSP_ISSUER = "tesis_payment_provider"
ITEMS = (AcceptableItem("SON-01", "Audifonos Sonora SON-01"),)


def chain_hash(chain: str) -> str:
    """The `reference` a verifier signs: `sd_hash` of the last segment of `open~~closed`."""
    return common.compute_sd_hash(common.parse_token(chain.rsplit("~~", 1)[-1]))


@dataclass
class Signer:
    """A verifier's receipt key: the PSP's (`kid=None`) or the merchant's."""

    issuer: str
    pem: str = field(default_factory=private_key_pem, repr=False)
    with_kid: bool = False

    @classmethod
    def merchant(cls, issuer: str = MERCHANT_ISSUER) -> "Signer":
        return cls(issuer, with_kid=True)

    @property
    def jwk(self) -> dict[str, Any]:
        return merchant_public_jwk(self.pem) if self.with_kid else public_jwk(self.pem)

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.jwk]}

    def sign(self, claims: dict[str, Any]) -> str:
        key = serialization.load_pem_private_key(self.pem.encode(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        headers = {"kid": self.jwk["kid"]} if self.with_kid else None
        return jwt.encode({"iss": self.issuer, **claims}, key, algorithm="ES256", headers=headers)

    def success(self, chain: str, **claims: Any) -> str:
        return self.sign(
            {"iat": int(time.time()), "status": "Success", "reference": chain_hash(chain), **claims}
        )

    def error(self, chain: str, code: str, **claims: Any) -> str:
        body = {
            "iat": int(time.time()),
            "status": "Error",
            "reference": chain_hash(chain),
            "error": code,
            "error_description": "Rejected by the verifier",
        }
        return self.sign({**body, **claims})


def psp_payment_id(idempotency_key: str) -> str:
    """The PSP's deterministic id of a rejected request (docs/ap2-profile.md, Errores)."""
    return "pay_rejected_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Chains:
    """One grant's open mandates and the closed pair over one signed checkout."""

    opens: MandatePair
    closed: MandatePair
    checkout_jwt: str
    merchant_pem: str


def chains(checkout: CheckoutSession, *, closed_at: datetime) -> Chains:
    agent, provider, merchant = private_key_pem(), private_key_pem(), private_key_pem()
    closer = AgentKeyCloser(agent, merchant_issuer=MERCHANT_ISSUER)
    issuer = OpenMandateIssuer(
        provider, agent_public_jwk=closer.public_jwk, merchant_issuer=MERCHANT_ISSUER
    )
    opens = issuer.issue(
        ITEMS,
        total_max_minor=150_000,
        consented_at=closed_at - timedelta(minutes=5),
        expires_at=closed_at + timedelta(hours=1),
    )
    token = checkout_jwt(merchant, checkout.model_dump(mode="json"), jti="jti_1")
    closed = closer.close(
        opens.checkout.token,
        opens.payment.token,
        checkout_jwt=token,
        checkout=checkout,
        closed_at=closed_at,
    )
    return Chains(opens, closed, token, merchant)
