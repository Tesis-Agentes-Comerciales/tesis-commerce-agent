"""Golden AP2 Human Not Present chains (profile `ap2-hnp/v1`) for the agent, merchant and PSP.

The merchant (HNP-M1) and the PSP (HNP-P1) copy `tests/fixtures/ap2_hnp/` as is: `keys.json` holds
fixture-only keys (generated here, never used outside tests) and `cases.json` one valid chain plus
one chain per alteration, each with the outcome every verifier must reach at the fixture clock.
Regenerate with `uv run python scripts/export_ap2_hnp_fixtures.py`; keys, salts and signatures
change on every run, the cases and their outcomes do not.
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import jwt
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_checkout_mandate import (
    AllowedMerchants,
    Item,
    LineItemRequirements,
    LineItems,
    OpenCheckoutMandate,
)
from ap2.sdk.generated.open_payment_mandate import (
    AllowedPayees,
    AllowedPaymentInstruments,
    AmountRange,
    Budget,
    OpenPaymentMandate,
    PaymentReference,
)
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.sdjwt import common, sd_jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto.jwk import JWK
from pydantic import BaseModel

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.ap2 import checkout_hash
from commerce_agent.ap2_hnp import (
    MERCHANT_ID,
    MERCHANT_NAME,
    PAYMENT_AUDIENCE,
    PROFILE_DIGEST,
    PROFILE_ID,
    PROFILE_VERSION,
    AcceptableItem,
    AgentKeyCloser,
    MandateTrust,
    OpenMandateIssuer,
    present_closed_mandate,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ap2_hnp"
REGENERATE = "uv run python scripts/export_ap2_hnp_fixtures.py"

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
CONSENTED_AT = NOW - timedelta(hours=2)
EXPIRES_AT = CONSENTED_AT + timedelta(hours=24)
CHECKOUT_SIGNED_AT = NOW - timedelta(seconds=60)
CLOSED_AT = NOW - timedelta(seconds=30)
MERCHANT_ISSUER = "http://127.0.0.1:4120"
CHECKOUT_AUDIENCE = "ap2-shopping-agent"
TOTAL_MAX_MINOR = 75_000
SHIPPING_MINOR = 2_000
ITEMS = (
    AcceptableItem(id="SON-01", title="Audifonos Sonora SON-01"),
    AcceptableItem(id="SON-02", title="Audifonos Sonora SON-02"),
)
OTHER_MERCHANT = Merchant(id="otro_comercio", name="Otro Comercio", website="https://otro.example")
INSTRUMENT = PaymentInstrument(id="tesis_sandbox", type="sandbox_token")

Outcome = Literal["accept", "invalid_credential", "invalid_mandate", "unresolved_constraint"]


@dataclass(frozen=True)
class FixtureKeys:
    provider_pem: str
    agent_pem: str
    merchant_pem: str


def private_key_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def generate_keys() -> FixtureKeys:
    return FixtureKeys(private_key_pem(), private_key_pem(), private_key_pem())


def public_jwk(pem: str) -> dict[str, Any]:
    return json.loads(JWK.from_pem(pem.encode()).export_public())


def merchant_public_jwk(pem: str) -> dict[str, Any]:
    """The JWK the merchant publishes, with its kid (SHA-256 of the SPKI), as its signer does."""
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    kid = base64.urlsafe_b64encode(hashlib.sha256(spki).digest()).rstrip(b"=").decode()
    return {**public_jwk(pem), "kid": kid, "alg": "ES256", "use": "sig"}


def trust(keys: FixtureKeys) -> MandateTrust:
    return MandateTrust(
        issuer_jwk=public_jwk(keys.provider_pem),
        agent_key_jwk=public_jwk(keys.agent_pem),
        merchant_issuer=MERCHANT_ISSUER,
    )


def acp_checkout(checkout_id: str, item: AcceptableItem, items_minor: int) -> dict[str, Any]:
    """A `ready_for_payment` checkout shaped like the merchant's ACP response."""
    line_id = "li_" + checkout_id.removeprefix("cs_")
    subtotal = {"type": "subtotal", "display_text": "Producto", "amount": items_minor}
    return {
        "id": checkout_id,
        "protocol": {"version": "2026-04-17"},
        "status": "ready_for_payment",
        "currency": "cop",
        "capabilities": {"payment": {"handlers": []}},
        "line_items": [
            {
                "id": line_id,
                "item": {"id": item.id, "name": item.title, "unit_amount": items_minor},
                "quantity": 1,
                "totals": [subtotal, {**subtotal, "type": "total"}],
            }
        ],
        "totals": [
            subtotal,
            {"type": "fulfillment", "display_text": "Envio", "amount": SHIPPING_MINOR},
            {"type": "total", "display_text": "Total", "amount": items_minor + SHIPPING_MINOR},
        ],
        "messages": [],
        "links": [],
        "created_at": "2026-10-21T14:58:00Z",
        "updated_at": "2026-10-21T14:58:30Z",
        "expires_at": "2026-10-21T15:30:00Z",
    }


def checkout_jwt(merchant_pem: str, checkout: dict[str, Any], *, jti: str) -> str:
    """The envelope the merchant signs at `GET /checkout_sessions/{id}/ap2/checkout-jwt`."""
    iat = int(CHECKOUT_SIGNED_AT.timestamp())
    claims = {
        "iss": MERCHANT_ISSUER,
        "aud": CHECKOUT_AUDIENCE,
        "iat": iat,
        "exp": iat + 300,
        "jti": jti,
        "checkout": checkout,
        "checkout_revision": 0,
    }
    key = serialization.load_pem_private_key(merchant_pem.encode(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    headers = {"kid": merchant_public_jwk(merchant_pem)["kid"], "typ": "JWT"}
    return jwt.encode(claims, key, algorithm="ES256", headers=headers)


class AllowedRegions(BaseModel):
    """A constraint no AP2 version defines: verifiers must treat it as failing."""

    type: Literal["checkout.allowed_regions"] = "checkout.allowed_regions"
    regions: list[str]


class OpenCheckoutBeyondProfile(OpenCheckoutMandate):
    constraints: list[AllowedMerchants | LineItems | AllowedRegions]  # pyright: ignore[reportIncompatibleVariableOverride]


def profile_merchant() -> Merchant:
    return Merchant(id=MERCHANT_ID, name=MERCHANT_NAME, website=MERCHANT_ISSUER)


def open_checkout_constraints(merchant: Merchant) -> list[AllowedMerchants | LineItems]:
    acceptable = [Item(id=item.id, title=item.title) for item in ITEMS]
    return [
        AllowedMerchants(allowed=[merchant]),
        LineItems(
            items=[LineItemRequirements(id="line_1", quantity=1, acceptable_items=acceptable)]
        ),
    ]


def open_payment_mandate(keys: FixtureKeys, open_checkout: str, extra: list[Budget]) -> str:
    reference = common.compute_issuer_jwt_hash(common.parse_token(open_checkout))
    mandate = OpenPaymentMandate(
        constraints=[
            AmountRange(currency="COP", max=TOTAL_MAX_MINOR),
            AllowedPayees(allowed=[profile_merchant()]),
            AllowedPaymentInstruments(allowed=[INSTRUMENT]),
            PaymentReference(conditional_transaction_id=reference),
            *extra,
        ],
        cnf={"jwk": public_jwk(keys.agent_pem)},
        iat=int(CONSENTED_AT.timestamp()),
        exp=int(EXPIRES_AT.timestamp()),
    )
    return sd_jwt.create(mandate, JWK.from_pem(keys.provider_pem.encode())).sd_jwt_issuance


def signed_open_checkout(keys: FixtureKeys, mandate: OpenCheckoutMandate) -> str:
    return sd_jwt.create(mandate, JWK.from_pem(keys.provider_pem.encode())).sd_jwt_issuance


def open_checkout_with(keys: FixtureKeys, merchant: Merchant, *, unknown: bool = False) -> str:
    fields: dict[str, Any] = {
        "cnf": {"jwk": public_jwk(keys.agent_pem)},
        "iat": int(CONSENTED_AT.timestamp()),
        "exp": int(EXPIRES_AT.timestamp()),
    }
    constraints = open_checkout_constraints(merchant)
    if unknown:
        regions = AllowedRegions(regions=["CO"])
        return signed_open_checkout(
            keys, OpenCheckoutBeyondProfile(constraints=[*constraints, regions], **fields)
        )
    return signed_open_checkout(keys, OpenCheckoutMandate(constraints=constraints, **fields))


def closed_checkout(
    open_token: str, signed_checkout: str, *, declared_hash: str, holder: str
) -> str:
    iat = int(CLOSED_AT.timestamp())
    mandate = CheckoutMandate(
        checkout_jwt=signed_checkout, checkout_hash=declared_hash, iat=iat, exp=iat + 300
    )
    return present_closed_mandate(
        open_token,
        mandate,
        holder_key=JWK.from_pem(holder.encode()),
        aud=MERCHANT_ISSUER,
        nonce=checkout_hash(signed_checkout),
        issued_at=iat,
    )


def closed_payment(
    open_token: str, signed_checkout: str, total: int, *, payee: Merchant, holder: str
) -> str:
    iat = int(CLOSED_AT.timestamp())
    mandate = PaymentMandate(
        transaction_id=checkout_hash(signed_checkout),
        payee=payee,
        payment_amount=Amount(amount=total, currency="COP"),
        payment_instrument=INSTRUMENT,
        iat=iat,
        exp=iat + 300,
    )
    return present_closed_mandate(
        open_token,
        mandate,
        holder_key=JWK.from_pem(holder.encode()),
        aud=PAYMENT_AUDIENCE,
        nonce=checkout_hash(signed_checkout),
        issued_at=iat,
    )


def splice(open_token: str, foreign_chain: str) -> str:
    """The grant's open mandate followed by a closed mandate bound to another open mandate."""
    return open_token.removesuffix("~") + "~~" + foreign_chain.split("~~")[1]


@dataclass(frozen=True)
class Case:
    name: str
    description: str
    checkout: dict[str, Any]
    checkout_jwt: str
    open_checkout: str
    open_payment: str
    checkout_chain: str
    payment_chain: str
    agent: Outcome
    merchant: Outcome
    psp: Outcome

    def document(self) -> dict[str, Any]:
        status = {
            "accept": "valid",
            "invalid_credential": "invalid",
            "invalid_mandate": "invalid",
            "unresolved_constraint": "unresolved",
        }[self.agent]
        agent_error = None if self.agent == "accept" else self.agent
        return {
            "name": self.name,
            "description": self.description,
            "expected": {
                "agent": {"status": status, "error": agent_error},
                "merchant": self.merchant,
                "psp": self.psp,
            },
            "checkout": self.checkout,
            "checkout_jwt": self.checkout_jwt,
            "open_checkout": self.open_checkout,
            "open_payment": self.open_payment,
            "checkout_chain": self.checkout_chain,
            "payment_chain": self.payment_chain,
        }


class GoldenChains:
    """Builds every case from one grant: the issuer and the closer are the product's."""

    def __init__(self, keys: FixtureKeys) -> None:
        self.keys = keys
        self.closer = AgentKeyCloser(keys.agent_pem, merchant_issuer=MERCHANT_ISSUER)
        self.issuer = OpenMandateIssuer(
            keys.provider_pem,
            agent_public_jwk=self.closer.public_jwk,
            merchant_issuer=MERCHANT_ISSUER,
        )
        self.opens = self.grant_opens()

    def grant_opens(self) -> tuple[str, str]:
        opens = self.issuer.issue(
            ITEMS, total_max_minor=TOTAL_MAX_MINOR, consented_at=CONSENTED_AT, expires_at=EXPIRES_AT
        )
        return opens.checkout.token, opens.payment.token

    def signed(
        self, name: str, item: AcceptableItem = ITEMS[0], items_minor: int = 72_000
    ) -> tuple[dict[str, Any], str]:
        checkout = acp_checkout(f"cs_hnp_{name}", item, items_minor)
        return checkout, checkout_jwt(self.keys.merchant_pem, checkout, jti=f"jti_hnp_{name}")

    def closed(
        self, checkout: dict[str, Any], token: str, opens: tuple[str, str] | None = None
    ) -> tuple[str, str]:
        closed = self.closer.close(
            *(opens or self.opens),
            checkout_jwt=token,
            checkout=CheckoutSession.model_validate(checkout),
            closed_at=CLOSED_AT,
        )
        return closed.checkout.token, closed.payment.token

    def case(
        self,
        name: str,
        description: str,
        signed: tuple[dict[str, Any], str],
        chains: tuple[str, str],
        outcomes: tuple[Outcome, Outcome, Outcome],
        opens: tuple[str, str] | None = None,
    ) -> Case:
        checkout, token = signed
        open_checkout, open_payment = opens or self.opens
        agent, merchant, psp = outcomes
        return Case(
            name=name,
            description=description,
            checkout=checkout,
            checkout_jwt=token,
            open_checkout=open_checkout,
            open_payment=open_payment,
            checkout_chain=chains[0],
            payment_chain=chains[1],
            agent=agent,
            merchant=merchant,
            psp=psp,
        )


def build_cases(keys: FixtureKeys) -> list[Case]:
    golden = GoldenChains(keys)
    open_checkout, open_payment = golden.opens
    cases: list[Case] = []

    valid = golden.signed("valid")
    cases.append(
        golden.case(
            "valid",
            "Cadena valida: el agente cierra los abiertos del grant sobre un checkout en alcance.",
            valid,
            golden.closed(*valid),
            ("accept", "accept", "accept"),
        )
    )

    over = golden.signed("total", items_minor=78_000)
    cases.append(
        golden.case(
            "total_out_of_range",
            "Total autoritativo 80000 sobre el maximo 75000 de payment.amount_range.",
            over,
            golden.closed(*over),
            ("invalid_mandate", "accept", "invalid_mandate"),
        )
    )

    payee = golden.signed("payee")
    elsewhere_payment = closed_payment(
        open_payment, payee[1], 74_000, payee=OTHER_MERCHANT, holder=keys.agent_pem
    )
    cases.append(
        golden.case(
            "payee_mismatch",
            "El pago cerrado paga a otro comercio, fuera de payment.allowed_payees.",
            payee,
            (golden.closed(*payee)[0], elsewhere_payment),
            ("invalid_mandate", "accept", "invalid_mandate"),
        )
    )

    variant = golden.signed("variant", item=AcceptableItem("SON-09", "Audifonos Sonora SON-09"))
    cases.append(
        golden.case(
            "variant_not_acceptable",
            "El checkout lleva SON-09, que no esta en acceptable_items.",
            variant,
            golden.closed(*variant),
            ("invalid_mandate", "invalid_mandate", "accept"),
        )
    )

    merchant = golden.signed("merchant")
    elsewhere = open_checkout_with(keys, OTHER_MERCHANT)
    elsewhere_opens = (elsewhere, open_payment_mandate(keys, elsewhere, []))
    cases.append(
        golden.case(
            "merchant_not_allowed",
            "El checkout abierto solo permite otro comercio; el checkout es de tesis_merchant.",
            merchant,
            golden.closed(*merchant, elsewhere_opens),
            ("invalid_mandate", "invalid_mandate", "accept"),
            elsewhere_opens,
        )
    )

    other = golden.signed("other_key")
    other_key = private_key_pem()
    other_hash = checkout_hash(other[1])
    cases.append(
        golden.case(
            "closed_with_other_key",
            "Los dos cierres los firma una clave distinta de la del cnf (la Agent Key).",
            other,
            (
                closed_checkout(
                    open_checkout, other[1], declared_hash=other_hash, holder=other_key
                ),
                closed_payment(
                    open_payment, other[1], 74_000, payee=profile_merchant(), holder=other_key
                ),
            ),
            ("invalid_credential", "invalid_credential", "invalid_credential"),
        )
    )

    foreign = golden.signed("sd_hash")
    foreign_chains = golden.closed(*foreign, golden.grant_opens())
    cases.append(
        golden.case(
            "foreign_sd_hash",
            "Cada cierre lleva el sd_hash de otro abierto (otra emision del mismo alcance).",
            foreign,
            (splice(open_checkout, foreign_chains[0]), splice(open_payment, foreign_chains[1])),
            ("invalid_credential", "invalid_credential", "invalid_credential"),
        )
    )

    hashed = golden.signed("checkout_hash")
    another_jwt = checkout_jwt(keys.merchant_pem, hashed[0], jti="jti_hnp_checkout_hash_other")
    declared = closed_checkout(
        open_checkout, hashed[1], declared_hash=checkout_hash(another_jwt), holder=keys.agent_pem
    )
    cases.append(
        golden.case(
            "checkout_hash_mismatch",
            "El checkout cerrado declara el checkout_hash de otro JWT; nonce y transaction_id"
            " son los del JWT presentado.",
            hashed,
            (declared, golden.closed(*hashed)[1]),
            ("invalid_mandate", "invalid_mandate", "invalid_mandate"),
        )
    )

    regions = golden.signed("unknown_checkout")
    beyond = open_checkout_with(keys, profile_merchant(), unknown=True)
    beyond_opens = (beyond, open_payment_mandate(keys, beyond, []))
    cases.append(
        golden.case(
            "unknown_constraint_checkout",
            "El checkout abierto trae checkout.allowed_regions, que ningun perfil define.",
            regions,
            golden.closed(*regions, beyond_opens),
            ("unresolved_constraint", "unresolved_constraint", "unresolved_constraint"),
            beyond_opens,
        )
    )

    budget = golden.signed("unknown_payment")
    budget_opens = (
        open_checkout,
        open_payment_mandate(keys, open_checkout, [Budget(max=750.0, currency="COP")]),
    )
    cases.append(
        golden.case(
            "unknown_constraint_payment",
            "El pago abierto trae payment.budget, que es de AP2 pero esta fuera del perfil.",
            budget,
            golden.closed(*budget, budget_opens),
            ("unresolved_constraint", "accept", "unresolved_constraint"),
            budget_opens,
        )
    )
    return cases


def keys_document(keys: FixtureKeys) -> dict[str, Any]:
    def entry(role: str, pem: str, public: dict[str, Any]) -> dict[str, Any]:
        return {"role": role, "public_jwk": public, "private_key_pem": pem}

    return {
        "fixture_only": (
            "Claves generadas para estas fixtures de prueba. No son secretos de ningún entorno"
            " y nunca deben configurarse fuera de las pruebas."
        ),
        "provider": entry(
            "AGENT_AP2_PRIVATE_KEY_PEM: firma los mandatos abiertos (raíz de confianza)",
            keys.provider_pem,
            public_jwk(keys.provider_pem),
        ),
        "agent_key": entry(
            "AGENT_HNP_AGENT_KEY_PEM: firma los cierres (cnf de los abiertos)",
            keys.agent_pem,
            public_jwk(keys.agent_pem),
        ),
        "merchant": entry(
            "MERCHANT_AP2_PRIVATE_KEY_PEM: firma el checkout JWT",
            keys.merchant_pem,
            merchant_public_jwk(keys.merchant_pem),
        ),
    }


def cases_document(cases: list[Case]) -> dict[str, Any]:
    return {
        "profile": {"id": PROFILE_ID, "version": PROFILE_VERSION, "digest": PROFILE_DIGEST},
        "regenerate": REGENERATE,
        "now": int(NOW.timestamp()),
        "now_iso": NOW.isoformat().replace("+00:00", "Z"),
        "clock_skew_seconds": 60,
        "merchant_issuer": MERCHANT_ISSUER,
        "payment_audience": PAYMENT_AUDIENCE,
        "checkout_audience": CHECKOUT_AUDIENCE,
        "grant": {
            "variant_ids": [item.id for item in ITEMS],
            "total_max_minor": TOTAL_MAX_MINOR,
            "consented_at": int(CONSENTED_AT.timestamp()),
            "expires_at": int(EXPIRES_AT.timestamp()),
        },
        "cases": [case.document() for case in cases],
    }


def fixture_files(keys: FixtureKeys | None = None) -> dict[str, str]:
    keys = keys or generate_keys()
    documents = {"keys.json": keys_document(keys), "cases.json": cases_document(build_cases(keys))}
    return {
        name: json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        for name, document in documents.items()
    }
