"""AP2 Human Not Present, profile `ap2-hnp/v1` (docs/ap2-profile.md, «Human Not Present»).

At consent the provider key signs an open checkout mandate and an open payment mandate whose `cnf`
is the Agent Key. Later, with no human present, the Agent Key closes both over the merchant's
signed checkout. `verify_open_proof` and `verify_closed_proof` repeat what the merchant and the PSP
check, so the policy decisions (D2-HNP, D3-HNP) only see a status. Only deterministic services
build these signers; no model-facing tool reaches them (tests/test_ap2_hnp_boundary.py).
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import jwt
from ap2.sdk.checkout_mandate_chain import CheckoutMandateChain
from ap2.sdk.constraints import check_checkout_constraints
from ap2.sdk.disclosure_metadata import DisclosureMetadata, sd_claims_to_disclose
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_checkout_mandate import (
    AllowedMerchants,
    LineItemRequirements,
    LineItems,
    OpenCheckoutMandate,
)
from ap2.sdk.generated.open_checkout_mandate import Item as OpenItem
from ap2.sdk.generated.open_payment_mandate import (
    AllowedPayees,
    AllowedPaymentInstruments,
    AmountRange,
    OpenPaymentMandate,
    PaymentReference,
)
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.checkout import Checkout
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.mandate import MandateClient
from ap2.sdk.payment_mandate_chain import PaymentMandateChain
from ap2.sdk.sdjwt import chain, common, sd_jwt
from jwcrypto.jwk import JWK
from pydantic import BaseModel, ValidationError
from sd_jwt.holder import SDJWTHolder

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.ap2 import checkout_hash
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest

PROFILE_ID = "ap2-hnp/v1"
PROFILE_VERSION = 1
MERCHANT_ID = "tesis_merchant"
MERCHANT_NAME = "Tesis Commerce Merchant"
PAYMENT_AUDIENCE = "tesis_payment_provider"
CURRENCY = "COP"
LINE_ID = "line_1"
MAX_VARIANTS = 5
CLOSED_TTL_SECONDS = 300
CLOCK_SKEW_SECONDS = 60

_INSTRUMENT = PaymentInstrument(id="tesis_sandbox", type="sandbox_token")
_OPEN_VCT = {"checkout": "mandate.checkout.open.1", "payment": "mandate.payment.open.1"}
_CLOSED_VCT = {"checkout": "mandate.checkout.1", "payment": "mandate.payment.1"}
_OPEN_CLAIMS = {"vct", "constraints", "cnf", "iat", "exp"}
# The constraints the profile emits, keyed by `type`; their SDK fields are the only properties.
_CONSTRAINTS: dict[str, dict[str, type[BaseModel]]] = {
    kind: {model.model_fields["type"].default: model for model in models}
    for kind, models in (
        ("checkout", (AllowedMerchants, LineItems)),
        ("payment", (AmountRange, AllowedPayees, AllowedPaymentInstruments, PaymentReference)),
    )
}
_NESTED: dict[type[BaseModel], tuple[str, type[BaseModel]]] = {
    AllowedMerchants: ("allowed", Merchant),
    LineItems: ("items", LineItemRequirements),
    LineItemRequirements: ("acceptable_items", OpenItem),
    AllowedPayees: ("allowed", Merchant),
    AllowedPaymentInstruments: ("allowed", PaymentInstrument),
}
_PROFILE = {
    "schema_version": "commerce-ap2-hnp-profile/1",
    "profile": PROFILE_ID,
    "version": PROFILE_VERSION,
    "vct": {"open": _OPEN_VCT, "closed": _CLOSED_VCT},
    "constraints": {kind: sorted(types) for kind, types in _CONSTRAINTS.items()},
    "merchant": {"id": MERCHANT_ID, "name": MERCHANT_NAME},
    "instrument": _INSTRUMENT.model_dump(exclude_none=True),
    "currency": CURRENCY,
    "line": {"id": LINE_ID, "quantity": 1, "max_variants": MAX_VARIANTS},
    "binding": "sd_hash",
    "hash": "sha-256",
    "audiences": {"checkout": "merchant_issuer", "payment": PAYMENT_AUDIENCE},
    "nonce": "checkout_hash",
    "payment_reference": "issuer_jwt_hash",
    "closed_ttl_seconds": CLOSED_TTL_SECONDS,
    "clock_skew_seconds": CLOCK_SKEW_SECONDS,
}
PROFILE_DIGEST = sha256_digest(b"commerce-ap2-hnp-profile/v1\0" + canonical_bytes(_PROFILE))

Ap2Error = Literal["invalid_credential", "invalid_mandate", "unresolved_constraint"]


@dataclass(frozen=True)
class AcceptableItem:
    """A variant of the grant, with the feed title the open checkout mandate shows."""

    id: str
    title: str


@dataclass(frozen=True)
class SignedMandate:
    """A token as `delegation_mandates` keeps it (spec §4.3)."""

    token: str
    link_hash: str
    public_jwk: dict[str, Any]

    @property
    def token_digest(self) -> str:
        return sha256_digest(self.token.encode("ascii"))


@dataclass(frozen=True)
class MandatePair:
    """Two open SD-JWTs, or two `open~~closed` chains whose `link_hash` is the `sd_hash` that the
    receipts reference."""

    checkout: SignedMandate
    payment: SignedMandate


@dataclass(frozen=True)
class MandateTrust:
    """Public keys the proofs trust: the provider (root) and the Agent Key (`cnf`)."""

    issuer_jwk: Mapping[str, Any]
    agent_key_jwk: Mapping[str, Any]
    merchant_issuer: str


@dataclass(frozen=True)
class MandateProof:
    """`status` feeds `open_proof`/`closed_proof`; `error` is the AP2 code a verifier would use."""

    status: Literal["valid", "invalid", "unresolved"]
    error: Ap2Error | None = None
    detail: str | None = field(default=None, compare=False)


class _Rejected(Exception):
    error: Ap2Error

    def __init__(self, error: Ap2Error, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail


def project_acp_checkout(checkout: Mapping[str, Any] | CheckoutSession) -> Checkout:
    """The deterministic ACP→SDK projection of the profile; it claims no UCP conformance."""
    acp = checkout.model_dump(mode="json") if isinstance(checkout, CheckoutSession) else checkout
    status = {"ready_for_payment": "ready_for_complete", "completed": "completed"}
    try:
        return Checkout.model_validate(
            {
                "id": acp["id"],
                "merchant": {"id": MERCHANT_ID, "name": MERCHANT_NAME},
                "line_items": [
                    {
                        "id": line["id"],
                        "item": {
                            "id": line["item"]["id"],
                            "title": line["item"]["name"],
                            "price": line["item"]["unit_amount"],
                        },
                        "quantity": line["quantity"],
                        "totals": [],
                    }
                    for line in acp["line_items"]
                ],
                "status": status[acp["status"]],
                "currency": acp["currency"].upper(),
                "totals": [{"type": row["type"], "amount": row["amount"]} for row in acp["totals"]],
                "links": [],
            }
        )
    except (KeyError, TypeError, AttributeError, ValidationError) as error:
        raise ValueError("Checkout cannot be projected to an AP2 SDK checkout") from error


def present_closed_mandate(
    open_token: str,
    closed: BaseModel,
    *,
    holder_key: JWK,
    aud: str,
    nonce: str,
    issued_at: int,
) -> str:
    """`MandateClient.present` (full disclosure, `sd_hash`) with the caller's clock.

    The SDK stamps the hop's `iat` from `time.time()` and appends the presentation to a log
    file inside the installed package, so the hop is built from the same SDK primitives here.
    """
    previous = common.parse_token(open_token)
    claims = common.selectively_disclosable_claims(
        common.delegate_claims_from_model(closed),
        DisclosureMetadata.from_model(closed),
        {"iat": issued_at, "aud": aud, "nonce": nonce, "sd_hash": common.compute_sd_hash(previous)},
    )
    issued = common.issue_sd_jwt(
        claims=claims,
        issuer_key=holder_key,
        header_params=common.header_parameters(holder_key, "kb+sd-jwt"),
        add_decoy_claims=False,
        serialization_format="compact",
    )
    holder = SDJWTHolder(issued.sd_jwt_issuance)
    holder.create_presentation(
        claims_to_disclose={"delegate_payload": [sd_claims_to_disclose(closed)]}
    )
    return f"{open_token.removesuffix('~')}~~{holder.sd_jwt_presentation}"


class OpenMandateIssuer:
    """Signs the open mandates at consent with the provider key (`AGENT_AP2_PRIVATE_KEY_PEM`)."""

    def __init__(
        self, private_key_pem: str, *, agent_public_jwk: Mapping[str, Any], merchant_issuer: str
    ) -> None:
        self._key = _private_key(private_key_pem)
        self._agent_jwk = json.loads(_public_key(agent_public_jwk).export_public())
        if _public_key(agent_public_jwk).thumbprint() == self._key.thumbprint():
            raise ValueError("The Agent Key must differ from the provider key")
        self._merchant = Merchant(id=MERCHANT_ID, name=MERCHANT_NAME, website=merchant_issuer)

    @property
    def public_jwk(self) -> dict[str, Any]:
        return json.loads(self._key.export_public())

    @property
    def kid(self) -> str:
        return self._key.thumbprint()

    def issue(
        self,
        items: Sequence[AcceptableItem],
        *,
        total_max_minor: int,
        consented_at: datetime,
        expires_at: datetime,
    ) -> MandatePair:
        ids = [item.id for item in items]
        if not 1 <= len(ids) <= MAX_VARIANTS or len(set(ids)) != len(ids):
            raise ValueError(f"A grant names 1 to {MAX_VARIANTS} distinct variants")
        if not all(item.id and item.title for item in items):
            raise ValueError("Every variant needs its id and feed title")
        if type(total_max_minor) is not int or total_max_minor <= 0:
            raise ValueError("The maximum total must be a positive integer")
        iat, exp = _epoch(consented_at), _epoch(expires_at)
        if exp <= iat:
            raise ValueError("Open mandates must expire after consent")
        timing: dict[str, Any] = {"cnf": {"jwk": self._agent_jwk}, "iat": iat, "exp": exp}
        acceptable = [OpenItem(id=item.id, title=item.title) for item in items]
        requirement = LineItemRequirements(id=LINE_ID, quantity=1, acceptable_items=acceptable)
        open_checkout = OpenCheckoutMandate(
            constraints=[
                AllowedMerchants(allowed=[self._merchant]),
                LineItems(items=[requirement]),
            ],
            **timing,
        )
        checkout = self._signed(open_checkout)
        open_payment = OpenPaymentMandate(
            constraints=[
                AmountRange(currency=CURRENCY, max=total_max_minor),
                AllowedPayees(allowed=[self._merchant]),
                AllowedPaymentInstruments(allowed=[_INSTRUMENT]),
                PaymentReference(conditional_transaction_id=checkout.link_hash),
            ],
            **timing,
        )
        return MandatePair(checkout=checkout, payment=self._signed(open_payment))

    def _signed(self, mandate: BaseModel) -> SignedMandate:
        token = sd_jwt.create(mandate, self._key).sd_jwt_issuance
        return SignedMandate(token, _issuer_hash(token), self.public_jwk)


class AgentKeyCloser:
    """Closes a grant's open mandates with the Agent Key (`AGENT_HNP_AGENT_KEY_PEM`)."""

    def __init__(self, private_key_pem: str, *, merchant_issuer: str) -> None:
        self._key = _private_key(private_key_pem)
        self._merchant_issuer = merchant_issuer

    @property
    def public_jwk(self) -> dict[str, Any]:
        return json.loads(self._key.export_public())

    @property
    def kid(self) -> str:
        return self._key.thumbprint()

    def close(
        self,
        open_checkout: str,
        open_payment: str,
        *,
        checkout_jwt: str,
        checkout: CheckoutSession,
        closed_at: datetime,
    ) -> MandatePair:
        if checkout.status != "ready_for_payment" or checkout.currency != "cop":
            raise ValueError("Checkout is not closable")
        iat = _epoch(closed_at)
        exp = iat + CLOSED_TTL_SECONDS
        digest = checkout_hash(checkout_jwt)
        closed_checkout = CheckoutMandate(
            checkout_jwt=checkout_jwt, checkout_hash=digest, iat=iat, exp=exp
        )
        closed_payment = PaymentMandate(
            transaction_id=digest,
            payee=Merchant(id=MERCHANT_ID, name=MERCHANT_NAME),
            payment_amount=Amount(amount=checkout.total_minor, currency=CURRENCY),
            payment_instrument=_INSTRUMENT,
            iat=iat,
            exp=exp,
        )
        return MandatePair(
            checkout=self._presented(open_checkout, closed_checkout, self._merchant_issuer, digest),
            payment=self._presented(open_payment, closed_payment, PAYMENT_AUDIENCE, digest),
        )

    def _presented(
        self, open_token: str, closed: CheckoutMandate | PaymentMandate, aud: str, nonce: str
    ) -> SignedMandate:
        token = present_closed_mandate(
            open_token,
            closed,
            holder_key=self._key,
            aud=aud,
            nonce=nonce,
            issued_at=closed.iat or 0,
        )
        last = common.parse_token(token.split("~~")[1])
        return SignedMandate(token, common.compute_sd_hash(last), self.public_jwk)


def verify_open_proof(
    open_checkout: str,
    open_payment: str,
    *,
    trust: MandateTrust,
    variant_ids: Sequence[str],
    total_max_minor: int,
    expires_at: datetime,
    now: datetime,
) -> MandateProof:
    """The open mandates are the provider's, bound to the Agent Key and exactly the grant."""

    def check() -> None:
        at = _epoch(now)
        checkout = _validated(
            OpenCheckoutMandate, _open_payload(open_checkout, "checkout", trust, at)
        )
        payment = _validated(OpenPaymentMandate, _open_payload(open_payment, "payment", trust, at))
        merchant = Merchant(id=MERCHANT_ID, name=MERCHANT_NAME, website=trust.merchant_issuer)
        line = _constraint(checkout.constraints, LineItems).items
        amount = _constraint(payment.constraints, AmountRange)
        reference = _constraint(payment.constraints, PaymentReference)
        if not (
            checkout.exp == payment.exp == _epoch(expires_at)
            and _constraint(checkout.constraints, AllowedMerchants).allowed == [merchant]
            and [(item.id, item.quantity) for item in line] == [(LINE_ID, 1)]
            and sorted(item.id for item in line[0].acceptable_items) == sorted(variant_ids)
            and (amount.currency, amount.max, amount.min) == (CURRENCY, total_max_minor, None)
            and _constraint(payment.constraints, AllowedPayees).allowed == [merchant]
            and _constraint(payment.constraints, AllowedPaymentInstruments).allowed == [_INSTRUMENT]
            and reference.conditional_transaction_id == _issuer_hash(open_checkout)
        ):
            raise _Rejected("invalid_mandate", "Open mandates do not match the grant")

    return _proof(check)


def verify_closed_proof(
    checkout_chain: str,
    payment_chain: str,
    *,
    trust: MandateTrust,
    open_checkout: str,
    open_payment: str,
    checkout_jwt: str,
    now: datetime,
) -> MandateProof:
    """Both chains close the grant's open mandates over `checkout_jwt`, as M1 and P1 verify them.

    `checkout_jwt` is the token the agent already verified against the merchant's JWKS.
    """

    def check() -> None:
        at = _epoch(now)
        nonce = checkout_hash(checkout_jwt)
        checkout_payloads = _chain_payloads(
            checkout_chain, "checkout", open_checkout, trust.merchant_issuer, nonce, trust, at
        )
        payment_payloads = _chain_payloads(
            payment_chain, "payment", open_payment, PAYMENT_AUDIENCE, nonce, trust, at
        )
        acp = _signed_checkout(checkout_jwt)
        _check_checkout_chain(_parsed(CheckoutMandateChain.parse, checkout_payloads), acp, nonce)
        payments = _parsed(PaymentMandateChain.parse, payment_payloads)
        _check_payment_chain(payments, acp, nonce, _issuer_hash(open_checkout))

    return _proof(check)


def _proof(check: Callable[[], None]) -> MandateProof:
    try:
        check()
    except _Rejected as rejected:
        status = "unresolved" if rejected.error == "unresolved_constraint" else "invalid"
        return MandateProof(status, rejected.error, rejected.detail)
    return MandateProof(status="valid")


def _open_payload(token: str, kind: str, trust: MandateTrust, at: int) -> dict[str, Any]:
    if "~~" in token or not token.endswith("~"):
        raise _Rejected("invalid_credential", "An open mandate is one SD-JWT without key binding")
    try:
        payload = chain.verify_chain(
            [common.parse_token(token)],
            _root_key(trust),
            clock_skew_seconds=CLOCK_SKEW_SECONDS,
            current_time=at,
        )[0]
    except Exception as error:
        raise _Rejected("invalid_credential", "Open mandate signature is invalid") from error
    _check_open_segment(payload, kind, trust, at)
    return payload


def _chain_payloads(
    token: str,
    kind: str,
    expected_open: str,
    aud: str,
    nonce: str,
    trust: MandateTrust,
    at: int,
) -> list[dict[str, Any]]:
    segments = token.split("~~")
    if len(segments) != 2 or segments[0] != expected_open.removesuffix("~"):
        raise _Rejected(
            "invalid_credential", f"The {kind} chain does not close the grant's mandate"
        )
    try:
        closed = common.parse_token(segments[1])
        if closed.typ != "kb+sd-jwt" or "sd_hash" not in closed.payload:
            raise ValueError("The close must be a terminal hop bound by sd_hash")
        payloads = MandateClient().verify(
            token,
            _root_key(trust),
            expected_aud=aud,
            expected_nonce=nonce,
            clock_skew_seconds=CLOCK_SKEW_SECONDS,
            current_time=at,
        )
    except Exception as error:
        raise _Rejected("invalid_credential", f"The {kind} chain does not verify") from error
    if not isinstance(payloads, list) or len(payloads) != 2:
        raise _Rejected("invalid_credential", f"The {kind} chain needs exactly two payloads")
    _check_open_segment(payloads[0], kind, trust, at)
    iat, exp = _check_times(payloads[1], _CLOSED_VCT[kind], at)
    if exp <= iat or exp - iat > CLOSED_TTL_SECONDS:
        raise _Rejected("invalid_mandate", f"The closed {kind} mandate lives too long")
    return payloads


def _check_open_segment(payload: dict[str, Any], kind: str, trust: MandateTrust, at: int) -> None:
    _check_times(payload, _OPEN_VCT[kind], at)
    cnf = payload.get("cnf")
    try:
        bound = _public_key(cnf["jwk"] if isinstance(cnf, dict) else {}).thumbprint()
    except (KeyError, TypeError, ValueError) as error:
        raise _Rejected("invalid_credential", "Open mandate has no cnf key") from error
    if bound != _public_key(trust.agent_key_jwk).thumbprint():
        raise _Rejected("invalid_credential", "Open mandate is bound to another key")
    _check_profile_constraints(payload, kind)


def _check_times(payload: dict[str, Any], vct: str, at: int) -> tuple[int, int]:
    if payload.get("vct") != vct:
        raise _Rejected("invalid_credential", f"Expected {vct} at this position")
    iat, exp = payload.get("iat"), payload.get("exp")
    if type(iat) is not int or type(exp) is not int:
        raise _Rejected("invalid_credential", f"{vct} needs integer iat and exp")
    if iat > at + CLOCK_SKEW_SECONDS or at >= exp:
        raise _Rejected("invalid_credential", f"{vct} is not current")
    return iat, exp


def _check_profile_constraints(payload: dict[str, Any], kind: str) -> None:
    """The spec fails unknown constraints; the SDK raises on them or ignores extra properties."""
    models = _CONSTRAINTS[kind]
    constraints = payload.get("constraints")
    if set(payload) - _OPEN_CLAIMS or not isinstance(constraints, list):
        raise _Rejected(
            "unresolved_constraint", f"Open {kind} mandate has claims beyond the profile"
        )
    types = [item.get("type") if isinstance(item, dict) else None for item in constraints]
    if sorted(map(str, types)) != sorted(models):
        raise _Rejected("unresolved_constraint", f"Open {kind} constraints are not the profile's")
    for item in constraints:
        if _has_unknown_properties(item, models[item["type"]]):
            raise _Rejected("unresolved_constraint", f"{item['type']} has unknown properties")
    # `_has_unknown_properties` already required every line requirement to be an object.
    lines = next((item["items"] for item in constraints if "items" in item), [])
    if any(not requirement.get("acceptable_items") for requirement in lines):
        raise _Rejected("unresolved_constraint", "Empty acceptable_items is a wildcard")


def _has_unknown_properties(value: Any, model: type[BaseModel]) -> bool:
    if not isinstance(value, dict) or set(value) - set(model.model_fields):
        return True
    field_name, nested = _NESTED.get(model, ("", model))
    entries = value.get(field_name) if field_name else None
    return isinstance(entries, list) and any(_has_unknown_properties(e, nested) for e in entries)


def _check_checkout_chain(chain: CheckoutMandateChain, acp: dict[str, Any], nonce: str) -> None:
    closed = chain.closed_mandate
    if closed.checkout_hash != nonce or checkout_hash(closed.checkout_jwt) != nonce:
        raise _Rejected("invalid_mandate", "checkout_hash does not match the presented checkout")
    try:
        projected = project_acp_checkout(acp)
    except ValueError as error:
        raise _Rejected("unresolved_constraint", "Checkout cannot be projected") from error
    if [line.quantity for line in projected.line_items] != [1]:
        raise _Rejected("invalid_mandate", "The profile closes one line with quantity 1")
    violations = check_checkout_constraints(chain.open_mandate, projected)
    if violations:
        raise _Rejected("invalid_mandate", violations[0])


def _check_payment_chain(
    chain: PaymentMandateChain, acp: dict[str, Any], nonce: str, open_checkout_hash: str
) -> None:
    violations = chain.verify(
        expected_transaction_id=nonce, expected_open_checkout_hash=open_checkout_hash
    )
    closed = chain.closed_mandate
    totals = [row.get("amount") for row in acp.get("totals", []) if row.get("type") == "total"]
    if not violations and not (
        closed.payee.id == MERCHANT_ID
        and closed.payment_instrument.id == _INSTRUMENT.id
        and closed.payment_amount.currency == CURRENCY
        and totals == [closed.payment_amount.amount]
        and acp.get("status") == "ready_for_payment"
    ):
        violations = ["The closed payment does not match the checkout"]
    if violations:
        raise _Rejected("invalid_mandate", violations[0])


def _signed_checkout(checkout_jwt: str) -> dict[str, Any]:
    # The agent verified this token against the merchant's JWKS before closing (ACPClient).
    try:
        acp = jwt.decode(checkout_jwt, options={"verify_signature": False})["checkout"]
    except (jwt.PyJWTError, KeyError, TypeError) as error:
        raise _Rejected("unresolved_constraint", "Checkout JWT carries no checkout") from error
    if not isinstance(acp, dict):
        raise _Rejected("unresolved_constraint", "Checkout JWT carries no checkout")
    return acp


def _parsed[Chain: (CheckoutMandateChain, PaymentMandateChain)](
    parse: Callable[[list[dict[str, Any]]], Chain], payloads: list[dict[str, Any]]
) -> Chain:
    try:
        return parse(payloads)
    except (ValueError, ValidationError) as error:
        raise _Rejected(
            "unresolved_constraint", "Mandates do not parse as the SDK chain"
        ) from error


def _validated[Model: BaseModel](model: type[Model], payload: dict[str, Any]) -> Model:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise _Rejected("unresolved_constraint", "Open mandate does not parse") from error


def _constraint[Model: BaseModel](constraints: Sequence[object], kind: type[Model]) -> Model:
    return next(item for item in constraints if isinstance(item, kind))


def _root_key(trust: MandateTrust) -> chain.PublicKeyProvider:
    """Only the pinned provider key, named by its RFC 7638 thumbprint; never an x5c chain."""
    issuer = _public_key(trust.issuer_jwk)

    def provider(token: common.ParsedToken) -> JWK:
        header = token.header
        if (
            "x5c" in header
            or header.get("alg") != "ES256"
            or header.get("kid") != issuer.thumbprint()
        ):
            raise ValueError("Root mandate is not signed by the pinned provider key")
        return issuer

    return provider


def _issuer_hash(token: str) -> str:
    return common.compute_issuer_jwt_hash(common.parse_token(token))


def _private_key(pem: str) -> JWK:
    try:
        key = JWK.from_pem(pem.replace("\\n", "\n").encode())
    except (ValueError, TypeError) as error:
        raise ValueError("AP2 HNP signing requires an ES256 private key") from error
    if key.get("kty") != "EC" or key.get("crv") != "P-256" or not key.has_private:
        raise ValueError("AP2 HNP signing requires an ES256 private key")
    return key


def _public_key(jwk: Mapping[str, Any]) -> JWK:
    key = JWK.from_json(json.dumps(dict(jwk)))
    if key.get("kty") != "EC" or key.get("crv") != "P-256" or key.has_private:
        raise ValueError("Trusted AP2 keys are public P-256 JWKs")
    return key


def _epoch(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError("AP2 HNP times need an explicit timezone")
    return int(moment.timestamp())
