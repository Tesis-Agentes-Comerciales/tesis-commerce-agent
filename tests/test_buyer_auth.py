from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from commerce_agent.buyer_auth import JwtBuyerAuthenticator


def test_better_auth_jwt_checks_signature_issuer_audience_and_expiry(monkeypatch) -> None:
    issuer = "https://buyer.example.test"
    signer = ec.generate_private_key(ec.SECP256R1())
    other_signer = ec.generate_private_key(ec.SECP256R1())
    authenticator = JwtBuyerAuthenticator(issuer)
    monkeypatch.setattr(
        authenticator._keys,
        "get_signing_key_from_jwt",
        lambda token: SimpleNamespace(key=signer.public_key()),
    )
    now = datetime.now(UTC)

    def bearer(**overrides: object) -> str:
        claims: dict[str, object] = {
            "sub": "buyer-a",
            "iss": issuer,
            "aud": issuer,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            **overrides,
        }
        return "Bearer " + jwt.encode(claims, signer, algorithm="ES256")

    assert authenticator.authenticate(bearer()).id == "buyer-a"  # type: ignore[union-attr]
    assert authenticator.authenticate(bearer(iss="https://other.test")) is None
    assert authenticator.authenticate(bearer(aud="https://other.test")) is None
    assert authenticator.authenticate(bearer(exp=now - timedelta(minutes=5))) is None
    assert authenticator.authenticate(None) is None
    assert authenticator.authenticate("Bearer garbage") is None
    wrong_signature = jwt.encode(
        {
            "sub": "buyer-a",
            "iss": issuer,
            "aud": issuer,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        other_signer,
        algorithm="ES256",
    )
    assert authenticator.authenticate("Bearer " + wrong_signature) is None
