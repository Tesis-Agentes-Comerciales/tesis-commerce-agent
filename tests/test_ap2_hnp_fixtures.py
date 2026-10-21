"""The golden `ap2-hnp/v1` chains that the merchant (HNP-M1) and the PSP (HNP-P1) copy as is."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ap2.sdk import mandate as ap2_mandate
from ap2.sdk.checkout_mandate_chain import CheckoutMandateChain
from ap2.sdk.constraints import check_checkout_constraints
from ap2.sdk.mandate import MandateClient
from ap2.sdk.payment_mandate_chain import PaymentMandateChain
from ap2.sdk.sdjwt import common
from ap2.sdk.sdjwt.common import ParsedToken
from jwcrypto.common import JWException
from jwcrypto.jwk import JWK
from pydantic import ValidationError

from commerce_agent.ap2 import checkout_hash
from commerce_agent.ap2_hnp import (
    PROFILE_DIGEST,
    MandateProof,
    MandateTrust,
    project_acp_checkout,
    verify_closed_proof,
    verify_open_proof,
)
from tests.support.ap2_hnp_fixtures import FIXTURE_DIR, build_cases, generate_keys, trust

CASES = json.loads((FIXTURE_DIR / "cases.json").read_text(encoding="utf-8"))
KEYS = json.loads((FIXTURE_DIR / "keys.json").read_text(encoding="utf-8"))
NOW = datetime.fromtimestamp(CASES["now"], UTC)
TRUST = MandateTrust(
    issuer_jwk=KEYS["provider"]["public_jwk"],
    agent_key_jwk=KEYS["agent_key"]["public_jwk"],
    merchant_issuer=CASES["merchant_issuer"],
)
BY_NAME = {case["name"]: case for case in CASES["cases"]}


def closed_proof(case: dict[str, Any], trust: MandateTrust = TRUST, now: datetime = NOW):
    return verify_closed_proof(
        case["checkout_chain"],
        case["payment_chain"],
        trust=trust,
        open_checkout=case["open_checkout"],
        open_payment=case["open_payment"],
        checkout_jwt=case["checkout_jwt"],
        now=now,
    )


@pytest.fixture(autouse=True)
def sdk_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ap2_mandate, "LOG_FILE_PATH", str(tmp_path / "mandate_operations.log"))


def trusted_provider(token: ParsedToken) -> JWK:
    """The merchant's and the PSP's root provider: the pinned key, by its RFC 7638 kid."""
    key = JWK.from_json(json.dumps(KEYS["provider"]["public_jwk"]))
    assert token.header["kid"] == key.thumbprint() and "x5c" not in token.header
    return key


def sdk_payloads(chain: str, audience: str, nonce: str) -> list[dict[str, Any]]:
    payloads = MandateClient().verify(
        chain,
        trusted_provider,
        expected_aud=audience,
        expected_nonce=nonce,
        clock_skew_seconds=CASES["clock_skew_seconds"],
        current_time=CASES["now"],
    )
    assert isinstance(payloads, list)
    return payloads


def test_the_fixtures_carry_the_current_profile_and_only_public_trust() -> None:
    assert CASES["profile"] == {"id": "ap2-hnp/v1", "version": 1, "digest": PROFILE_DIGEST}
    assert [case["name"] for case in CASES["cases"]] == [
        case.name for case in build_cases(generate_keys())
    ]
    assert "fixture" in KEYS["fixture_only"]
    for role in ("provider", "agent_key", "merchant"):
        assert "d" not in KEYS[role]["public_jwk"]


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["name"])
def test_each_golden_chain_gets_its_expected_agent_proof(case: dict[str, Any]) -> None:
    expected = case["expected"]["agent"]

    assert closed_proof(case) == MandateProof(
        status=expected["status"], error=expected["error"], detail=closed_proof(case).detail
    )


def test_the_golden_open_mandates_prove_the_grant() -> None:
    grant = CASES["grant"]
    valid = BY_NAME["valid"]

    proof = verify_open_proof(
        valid["open_checkout"],
        valid["open_payment"],
        trust=TRUST,
        variant_ids=grant["variant_ids"],
        total_max_minor=grant["total_max_minor"],
        expires_at=datetime.fromtimestamp(grant["expires_at"], UTC),
        now=NOW,
    )

    assert proof == MandateProof(status="valid")


def test_the_valid_chain_verifies_with_the_sdk_mandate_client_and_chains() -> None:
    valid = BY_NAME["valid"]
    nonce = checkout_hash(valid["checkout_jwt"])
    checkout_payloads = sdk_payloads(valid["checkout_chain"], CASES["merchant_issuer"], nonce)
    payment_payloads = sdk_payloads(valid["payment_chain"], CASES["payment_audience"], nonce)

    checkout_chain = CheckoutMandateChain.parse(checkout_payloads)
    payment_chain = PaymentMandateChain.parse(payment_payloads)

    # The profile projects the merchant's ACP envelope instead of CheckoutMandateChain.verify,
    # which would read the JWT payload as an SDK Checkout (docs/ap2-profile.md).
    projected = project_acp_checkout(valid["checkout"])
    assert check_checkout_constraints(checkout_chain.open_mandate, projected) == []
    assert checkout_chain.closed_mandate.checkout_hash == nonce
    open_checkout_hash = common.compute_issuer_jwt_hash(common.parse_token(valid["open_checkout"]))
    assert (
        payment_chain.verify(
            expected_transaction_id=nonce, expected_open_checkout_hash=open_checkout_hash
        )
        == []
    )
    assert payment_chain.closed_mandate.payment_amount.amount == 74_000


@pytest.mark.parametrize(
    ("name", "chain"),
    [("closed_with_other_key", "checkout_chain"), ("foreign_sd_hash", "payment_chain")],
)
def test_the_sdk_rejects_the_altered_bindings(name: str, chain: str) -> None:
    case = BY_NAME[name]
    audience = CASES["merchant_issuer" if chain == "checkout_chain" else "payment_audience"]

    with pytest.raises((ValueError, JWException)):
        sdk_payloads(case[chain], audience, checkout_hash(case["checkout_jwt"]))


def test_the_sdk_chains_report_the_altered_constraints() -> None:
    def payment_violations(name: str) -> list[str]:
        case = BY_NAME[name]
        nonce = checkout_hash(case["checkout_jwt"])
        chain = PaymentMandateChain.parse(
            sdk_payloads(case["payment_chain"], CASES["payment_audience"], nonce)
        )
        open_hash = common.compute_issuer_jwt_hash(common.parse_token(case["open_checkout"]))
        return chain.verify(expected_transaction_id=nonce, expected_open_checkout_hash=open_hash)

    def checkout_violations(name: str) -> list[str]:
        case = BY_NAME[name]
        nonce = checkout_hash(case["checkout_jwt"])
        chain = CheckoutMandateChain.parse(
            sdk_payloads(case["checkout_chain"], CASES["merchant_issuer"], nonce)
        )
        return check_checkout_constraints(
            chain.open_mandate, project_acp_checkout(case["checkout"])
        )

    assert payment_violations("total_out_of_range") == ["Amount 80000 exceeds maximum 75000"]
    assert payment_violations("payee_mismatch") == ["Payee Otro Comercio not in allowed list"]
    assert checkout_violations("variant_not_acceptable") == [
        "Item SON-09 not in any requirement's acceptable items"
    ]
    assert checkout_violations("merchant_not_allowed") == [
        "Merchant Tesis Commerce Merchant not in allowed list"
    ]
    mismatch = BY_NAME["checkout_hash_mismatch"]
    closed = CheckoutMandateChain.parse(
        sdk_payloads(
            mismatch["checkout_chain"],
            CASES["merchant_issuer"],
            checkout_hash(mismatch["checkout_jwt"]),
        )
    ).closed_mandate
    assert closed.checkout_hash != checkout_hash(mismatch["checkout_jwt"])
    unknown = BY_NAME["unknown_constraint_checkout"]
    with pytest.raises(ValidationError):
        CheckoutMandateChain.parse(
            sdk_payloads(
                unknown["checkout_chain"],
                CASES["merchant_issuer"],
                checkout_hash(unknown["checkout_jwt"]),
            )
        )


def test_the_golden_chains_expire_with_their_close() -> None:
    expired = datetime.fromtimestamp(CASES["now"] + 270, UTC)

    proof = closed_proof(BY_NAME["valid"], now=expired)

    assert (proof.status, proof.error) == ("invalid", "invalid_credential")


def test_regenerated_fixtures_keep_every_expected_outcome() -> None:
    keys = generate_keys()

    for case in build_cases(keys):
        document = case.document()
        proof = closed_proof(document, trust=trust(keys))
        expected = document["expected"]["agent"]
        assert (proof.status, proof.error) == (expected["status"], expected["error"]), case.name
