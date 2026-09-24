from dataclasses import dataclass
from typing import Protocol

import jwt
from jwt import PyJWKClient


@dataclass(frozen=True, slots=True)
class BuyerPrincipal:
    id: str


class BuyerAuthenticator(Protocol):
    def authenticate(self, authorization: str | None) -> BuyerPrincipal | None: ...


class LocalBuyerAuthenticator:
    def __init__(self, principal_id: str) -> None:
        self._principal = BuyerPrincipal(principal_id)

    def authenticate(self, authorization: str | None) -> BuyerPrincipal | None:
        del authorization
        return self._principal


class JwtBuyerAuthenticator:
    def __init__(self, issuer: str) -> None:
        self._issuer = issuer.rstrip("/")
        self._keys = PyJWKClient(f"{self._issuer}/api/auth/jwks", cache_jwk_set=True)

    def authenticate(self, authorization: str | None) -> BuyerPrincipal | None:
        scheme, separator, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not separator or not token or len(token) > 8_192:
            return None
        try:
            key = self._keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["ES256"],
                issuer=self._issuer,
                audience=self._issuer,
                options={"require": ["sub", "exp", "iat", "iss", "aud"]},
            )
        except (jwt.PyJWTError, ValueError, OSError):
            return None
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 256:
            return None
        return BuyerPrincipal(subject)
