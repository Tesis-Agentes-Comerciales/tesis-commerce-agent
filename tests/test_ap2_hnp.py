"""AP2 Human Not Present (`ap2-hnp/v1`): open mandates, the Agent Key close and both proofs."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ap2.sdk import mandate as ap2_mandate
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_checkout_mandate import LineItems, OpenCheckoutMandate
from ap2.sdk.generated.open_payment_mandate import OpenPaymentMandate
from ap2.sdk.mandate import MandateClient
from ap2.sdk.sdjwt import common, sd_jwt
from ap2.sdk.sdjwt.chain import PublicKeyProvider
from ap2.sdk.sdjwt.common import ParsedToken
from jwcrypto.jwk import JWK

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.ap2 import checkout_hash
from commerce_agent.ap2_hnp import (
    PROFILE_DIGEST,
    AcceptableItem,
    AgentKeyCloser,
    MandatePair,
    MandateProof,
    OpenMandateIssuer,
    SignedMandate,
    present_closed_mandate,
    project_acp_checkout,
    verify_closed_proof,
    verify_open_proof,
)
from tests.support.ap2_hnp_fixtures import (
    CLOSED_AT,
    CONSENTED_AT,
    EXPIRES_AT,
    ITEMS,
    MERCHANT_ISSUER,
    NOW,
    TOTAL_MAX_MINOR,
    FixtureKeys,
    acp_checkout,
    checkout_jwt,
    generate_keys,
    open_checkout_with,
    profile_merchant,
    public_jwk,
    trust,
)


@pytest.fixture(scope="module")
def keys() -> FixtureKeys:
    return generate_keys()


@pytest.fixture(autouse=True)
def sdk_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK appends every operation to a file inside the installed package; keep it here."""
    log = tmp_path / "mandate_operations.log"
    monkeypatch.setattr(ap2_mandate, "LOG_FILE_PATH", str(log))
    return log


def closer(keys: FixtureKeys) -> AgentKeyCloser:
    return AgentKeyCloser(keys.agent_pem, merchant_issuer=MERCHANT_ISSUER)


def issuer(keys: FixtureKeys) -> OpenMandateIssuer:
    return OpenMandateIssuer(
        keys.provider_pem, agent_public_jwk=closer(keys).public_jwk, merchant_issuer=MERCHANT_ISSUER
    )


def issue(keys: FixtureKeys, **changes: Any) -> MandatePair:
    arguments: dict[str, Any] = {
        "total_max_minor": TOTAL_MAX_MINOR,
        "consented_at": CONSENTED_AT,
        "expires_at": EXPIRES_AT,
    }
    return issuer(keys).issue(changes.pop("items", ITEMS), **{**arguments, **changes})


def open_payload(keys: FixtureKeys, token: str) -> dict[str, Any]:
    provider = JWK.from_json(json.dumps(public_jwk(keys.provider_pem)))
    return sd_jwt.verify(token, provider)["delegate_payload"][0]


def proof_of_open(keys: FixtureKeys, opens: MandatePair, **changes: Any) -> MandateProof:
    arguments: dict[str, Any] = {
        "trust": trust(keys),
        "variant_ids": [item.id for item in ITEMS],
        "total_max_minor": TOTAL_MAX_MINOR,
        "expires_at": EXPIRES_AT,
        "now": NOW,
    }
    return verify_open_proof(opens.checkout.token, opens.payment.token, **{**arguments, **changes})


def signed_checkout(keys: FixtureKeys, items_minor: int = 72_000) -> tuple[dict[str, Any], str]:
    checkout = acp_checkout("cs_unit", ITEMS[0], items_minor)
    return checkout, checkout_jwt(keys.merchant_pem, checkout, jti="jti_unit")


def closed_proof(
    keys: FixtureKeys, opens: MandatePair, chains: tuple[str, str], token: str, **changes: Any
) -> MandateProof:
    arguments: dict[str, Any] = {
        "trust": trust(keys),
        "open_checkout": opens.checkout.token,
        "open_payment": opens.payment.token,
        "checkout_jwt": token,
        "now": NOW,
    }
    return verify_closed_proof(*chains, **{**arguments, **changes})


def close(keys: FixtureKeys, opens: MandatePair, token: str, checkout: dict[str, Any]):
    return closer(keys).close(
        opens.checkout.token,
        opens.payment.token,
        checkout_jwt=token,
        checkout=CheckoutSession.model_validate(checkout),
        closed_at=CLOSED_AT,
    )


def test_open_mandates_follow_the_profile(keys: FixtureKeys) -> None:
    opens = issue(keys)
    checkout = open_payload(keys, opens.checkout.token)
    payment = open_payload(keys, opens.payment.token)
    agent = public_jwk(keys.agent_pem)
    consented, expires = int(CONSENTED_AT.timestamp()), int(EXPIRES_AT.timestamp())

    assert checkout["vct"] == "mandate.checkout.open.1"
    assert payment["vct"] == "mandate.payment.open.1"
    assert checkout["cnf"] == payment["cnf"] == {"jwk": agent}
    assert (checkout["iat"], checkout["exp"]) == (payment["iat"], payment["exp"])
    assert (checkout["iat"], checkout["exp"]) == (consented, expires)
    merchant = {
        "id": "tesis_merchant",
        "name": "Tesis Commerce Merchant",
        "website": MERCHANT_ISSUER,
    }
    acceptable = [{"id": item.id, "title": item.title} for item in ITEMS]
    assert checkout["constraints"] == [
        {"type": "checkout.allowed_merchants", "allowed": [merchant]},
        {
            "type": "checkout.line_items",
            "items": [{"id": "line_1", "acceptable_items": acceptable, "quantity": 1}],
        },
    ]
    reference = common.compute_issuer_jwt_hash(common.parse_token(opens.checkout.token))
    assert payment["constraints"] == [
        {"type": "payment.amount_range", "currency": "COP", "max": TOTAL_MAX_MINOR},
        {"type": "payment.allowed_payees", "allowed": [merchant]},
        {
            "type": "payment.allowed_payment_instruments",
            "allowed": [{"id": "tesis_sandbox", "type": "sandbox_token"}],
        },
        {"type": "payment.reference", "conditional_transaction_id": reference},
    ]
    assert set(payment) == {"vct", "constraints", "cnf", "iat", "exp"}
    root = common.parse_token(opens.checkout.token).header
    assert root == {"alg": "ES256", "typ": "example+sd-jwt", "kid": issuer(keys).kid}
    assert opens.checkout.link_hash == reference
    assert opens.payment.link_hash == common.compute_issuer_jwt_hash(
        common.parse_token(opens.payment.token)
    )
    assert opens.checkout.public_jwk == opens.payment.public_jwk == public_jwk(keys.provider_pem)
    assert opens.checkout.token_digest.startswith("sha256:")
    assert issuer(keys).kid == JWK.from_pem(keys.provider_pem.encode()).thumbprint()
    assert closer(keys).kid == JWK.from_pem(keys.agent_pem.encode()).thumbprint()


def test_the_issuer_refuses_the_provider_key_as_agent_key(keys: FixtureKeys) -> None:
    with pytest.raises(ValueError, match="Agent Key"):
        OpenMandateIssuer(
            keys.provider_pem,
            agent_public_jwk=public_jwk(keys.provider_pem),
            merchant_issuer=MERCHANT_ISSUER,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"items": ()},
        {"items": tuple(AcceptableItem(id=f"V-{n}", title=f"V {n}") for n in range(6))},
        {"items": (ITEMS[0], ITEMS[0])},
        {"items": (AcceptableItem(id="V-1", title=""),)},
        {"total_max_minor": 0},
        {"expires_at": CONSENTED_AT},
        {"consented_at": CONSENTED_AT.replace(tzinfo=None)},
    ],
)
def test_the_issuer_refuses_a_scope_outside_the_profile(
    keys: FixtureKeys, changes: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        issue(keys, **changes)


def test_signers_read_escaped_pem_lines_and_never_expose_private_keys(keys: FixtureKeys) -> None:
    escaped = keys.agent_pem.replace("\n", "\\n")
    agent = AgentKeyCloser(escaped, merchant_issuer=MERCHANT_ISSUER)

    assert agent.public_jwk == public_jwk(keys.agent_pem)
    assert "d" not in agent.public_jwk and "d" not in issuer(keys).public_jwk
    private = json.loads(JWK.from_pem(keys.agent_pem.encode()).export_private())["d"]
    assert private not in repr(agent) and private not in str(vars(agent))
    with pytest.raises(ValueError, match="ES256"):
        AgentKeyCloser("not a key", merchant_issuer=MERCHANT_ISSUER)


def test_valid_open_mandates_prove_the_grant(keys: FixtureKeys) -> None:
    assert proof_of_open(keys, issue(keys)) == MandateProof(status="valid")


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"total_max_minor": TOTAL_MAX_MINOR + 1}, "invalid_mandate"),
        ({"variant_ids": ["SON-01"]}, "invalid_mandate"),
        ({"expires_at": EXPIRES_AT + timedelta(hours=1)}, "invalid_mandate"),
        ({"now": EXPIRES_AT}, "invalid_credential"),
        ({"now": CONSENTED_AT - timedelta(seconds=61)}, "invalid_credential"),
    ],
)
def test_open_mandates_that_do_not_match_the_grant_are_invalid(
    keys: FixtureKeys, changes: dict[str, Any], error: str
) -> None:
    proof = proof_of_open(keys, issue(keys), **changes)

    assert (proof.status, proof.error) == ("invalid", error)


def test_open_mandates_from_another_issuer_or_for_another_key_are_invalid(
    keys: FixtureKeys,
) -> None:
    opens = issue(keys)
    stranger = generate_keys()

    for proof in (
        proof_of_open(keys, opens, trust=trust(stranger)),
        proof_of_open(stranger, issue(stranger), trust=trust(keys)),
        proof_of_open(keys, MandatePair(checkout=opens.payment, payment=opens.checkout)),
    ):
        assert (proof.status, proof.error) == ("invalid", "invalid_credential")


def replaced(opens: MandatePair, *, checkout: str | None = None, payment: str | None = None):
    def signed(mandate: SignedMandate, token: str | None) -> SignedMandate:
        return mandate if token is None else replace(mandate, token=token)

    return MandatePair(signed(opens.checkout, checkout), signed(opens.payment, payment))


def test_open_mandates_beyond_the_profile_are_unresolved(keys: FixtureKeys) -> None:
    opens = issue(keys)
    provider = JWK.from_pem(keys.provider_pem.encode())
    payment = open_payload(keys, opens.payment.token)
    preset = OpenPaymentMandate.model_validate(
        payment | {"payee": {"id": "tesis_merchant", "name": "Tesis Commerce Merchant"}}
    )
    wildcard = OpenCheckoutMandate.model_validate(open_payload(keys, opens.checkout.token))
    line_items = wildcard.constraints[1]
    assert isinstance(line_items, LineItems)
    line_items.items[0].acceptable_items = []

    for altered in (
        replaced(opens, checkout=open_checkout_with(keys, profile_merchant(), unknown=True)),
        replaced(opens, payment=sd_jwt.create(preset, provider).sd_jwt_issuance),
        replaced(opens, checkout=sd_jwt.create(wildcard, provider).sd_jwt_issuance),
    ):
        proof = proof_of_open(keys, altered)
        assert (proof.status, proof.error) == ("unresolved", "unresolved_constraint")


def test_project_acp_checkout_maps_the_profile_fields() -> None:
    checkout = acp_checkout("cs_projection", ITEMS[0], 72_000)

    projected = project_acp_checkout(checkout).model_dump(mode="json", exclude_none=True)

    assert projected == {
        "id": "cs_projection",
        "merchant": {"id": "tesis_merchant", "name": "Tesis Commerce Merchant"},
        "line_items": [
            {
                "id": "li_projection",
                "item": {"id": "SON-01", "title": "Audifonos Sonora SON-01", "price": 72_000},
                "quantity": 1,
                "totals": [],
            }
        ],
        "status": "ready_for_complete",
        "currency": "COP",
        "totals": [
            {"type": "subtotal", "amount": 72_000},
            {"type": "fulfillment", "amount": 2_000},
            {"type": "total", "amount": 74_000},
        ],
        "links": [],
    }
    completed = project_acp_checkout(
        CheckoutSession.model_validate(checkout | {"status": "completed"})
    )
    assert completed.status.value == "completed"
    for status in ("not_ready_for_payment", "canceled", "expired"):
        with pytest.raises(ValueError, match="projected"):
            project_acp_checkout(checkout | {"status": status})
    with pytest.raises(ValueError, match="projected"):
        project_acp_checkout(checkout | {"line_items": [{"id": "li_1"}]})


def test_the_agent_key_closes_both_mandates(keys: FixtureKeys) -> None:
    opens = issue(keys)
    checkout, token = signed_checkout(keys)

    closed = close(keys, opens, token, checkout)

    agent = public_jwk(keys.agent_pem)
    assert closed.checkout.public_jwk == closed.payment.public_jwk == agent
    for chain, open_token, audience in (
        (closed.checkout.token, opens.checkout.token, MERCHANT_ISSUER),
        (closed.payment.token, opens.payment.token, "tesis_payment_provider"),
    ):
        first, last = chain.split("~~")
        assert first + "~" == open_token
        segment = common.parse_token(last)
        assert segment.header == {"alg": "ES256", "typ": "kb+sd-jwt", "kid": closer(keys).kid}
        assert segment.payload["aud"] == audience
        assert segment.payload["nonce"] == checkout_hash(token)
        assert segment.payload["iat"] == int(CLOSED_AT.timestamp())
        assert segment.payload["sd_hash"] == common.compute_sd_hash(common.parse_token(open_token))
        assert "issuer_jwt_hash" not in segment.payload
    assert closed.checkout.link_hash == common.compute_sd_hash(
        common.parse_token(closed.checkout.token.split("~~")[1])
    )
    assert closed_proof(keys, opens, (closed.checkout.token, closed.payment.token), token) == (
        MandateProof(status="valid")
    )


def issued_around_the_real_clock(keys: FixtureKeys) -> tuple[MandatePair, datetime]:
    """`MandateClient.present` stamps the hop with `time.time()`: compare at the real clock."""
    now = datetime.now(UTC).replace(microsecond=0)
    window = {"consented_at": now - timedelta(minutes=1), "expires_at": now + timedelta(hours=1)}
    return issue(keys, **window), now


def test_the_close_has_the_shape_mandate_client_present_produces(keys: FixtureKeys) -> None:
    opens, moment = issued_around_the_real_clock(keys)
    _, token = signed_checkout(keys)
    now = int(moment.timestamp())
    mandate = CheckoutMandate(
        checkout_jwt=token, checkout_hash=checkout_hash(token), iat=now, exp=now + 300
    )
    agent = JWK.from_pem(keys.agent_pem.encode())
    nonce = checkout_hash(token)

    ours = present_closed_mandate(
        opens.checkout.token,
        mandate,
        holder_key=agent,
        aud=MERCHANT_ISSUER,
        nonce=nonce,
        issued_at=now,
    )
    sdk = MandateClient().present(
        agent, opens.checkout.token, [mandate], aud=MERCHANT_ISSUER, nonce=nonce
    )

    def shape(chain: str) -> tuple[Any, ...]:
        first, last = chain.split("~~")
        segment = common.parse_token(last)
        return first, segment.header, sorted(segment.payload), len(segment.disclosures)

    assert shape(ours) == shape(sdk)
    for chain in (ours, sdk):
        payloads = MandateClient().verify(
            chain, provider_of(keys), expected_aud=MERCHANT_ISSUER, expected_nonce=nonce
        )
        assert isinstance(payloads, list) and payloads[1]["checkout_jwt"] == token


def provider_of(keys: FixtureKeys) -> PublicKeyProvider:
    key = JWK.from_json(json.dumps(public_jwk(keys.provider_pem)))

    def provider(token: ParsedToken) -> JWK:
        return key

    return provider


def test_closing_and_proving_leave_no_token_in_the_sdk_log(
    keys: FixtureKeys, sdk_log: Path
) -> None:
    opens = issue(keys)
    checkout, token = signed_checkout(keys)
    closed = close(keys, opens, token, checkout)
    chains = (closed.checkout.token, closed.payment.token)

    assert closed_proof(keys, opens, chains, token) == MandateProof(status="valid")
    written = sdk_log.read_text(encoding="utf-8") if sdk_log.exists() else ""
    jwts = {part.split("~")[0] for chain in chains for part in chain.split("~~")}
    assert all(jwt not in written for jwt in jwts | {token}), "a token reached the SDK log"

    # What the closer avoids: the SDK's own presentation logs the closed mandate in full.
    presented = MandateClient().present(
        JWK.from_pem(keys.agent_pem.encode()),
        opens.checkout.token,
        [CheckoutMandate(checkout_jwt=token, checkout_hash=checkout_hash(token))],
        aud=MERCHANT_ISSUER,
        nonce=checkout_hash(token),
    )
    assert presented.split("~~")[1].split("~")[0] in sdk_log.read_text(encoding="utf-8")


def test_the_closer_refuses_a_checkout_that_is_not_payable(keys: FixtureKeys) -> None:
    opens = issue(keys)
    checkout, token = signed_checkout(keys)

    for changed in ({"status": "not_ready_for_payment"}, {"currency": "usd"}):
        with pytest.raises(ValueError, match="closable"):
            close(keys, opens, token, checkout | changed)
    with pytest.raises(ValueError, match="timezone"):
        closer(keys).close(
            opens.checkout.token,
            opens.payment.token,
            checkout_jwt=token,
            checkout=CheckoutSession.model_validate(checkout),
            closed_at=CLOSED_AT.replace(tzinfo=None),
        )


def test_a_closed_proof_rejects_chains_outside_the_profile(keys: FixtureKeys) -> None:
    opens = issue(keys)
    checkout, token = signed_checkout(keys)
    closed = close(keys, opens, token, checkout)
    chains = (closed.checkout.token, closed.payment.token)
    agent = JWK.from_pem(keys.agent_pem.encode())
    iat = int(CLOSED_AT.timestamp())
    nonce = checkout_hash(token)

    def checkout_chain(mandate: CheckoutMandate, **hop: Any) -> tuple[str, str]:
        arguments = {"holder_key": agent, "aud": MERCHANT_ISSUER, "nonce": nonce, "issued_at": iat}
        chain = present_closed_mandate(opens.checkout.token, mandate, **{**arguments, **hop})
        return chain, chains[1]

    long_lived = CheckoutMandate(checkout_jwt=token, checkout_hash=nonce, iat=iat, exp=iat + 301)
    exact = CheckoutMandate(checkout_jwt=token, checkout_hash=nonce, iat=iat, exp=iat + 300)
    other_open = issue(keys)
    cases = [
        (checkout_chain(long_lived), {}, "invalid_mandate"),
        (checkout_chain(exact, aud="https://otro.example"), {}, "invalid_credential"),
        (checkout_chain(exact, nonce="otro"), {}, "invalid_credential"),
        (chains, {"now": CLOSED_AT + timedelta(seconds=300)}, "invalid_credential"),
        (chains, {"open_checkout": other_open.checkout.token}, "invalid_credential"),
        ((chains[0] + "~" + chains[0], chains[1]), {}, "invalid_credential"),
        ((chains[0], chains[1].split("~~")[0] + "~"), {}, "invalid_credential"),
        (chains, {"checkout_jwt": signed_checkout(keys, 72_001)[1]}, "invalid_credential"),
    ]
    for case_chains, changes, error in cases:
        proof = closed_proof(keys, opens, case_chains, token, **changes)
        assert (proof.status, proof.error) == ("invalid", error), (changes, proof)


def test_a_close_bound_by_issuer_jwt_hash_is_invalid(keys: FixtureKeys) -> None:
    opens, moment = issued_around_the_real_clock(keys)
    checkout, token = signed_checkout(keys)
    agent = JWK.from_pem(keys.agent_pem.encode())
    now = int(moment.timestamp())
    nonce = checkout_hash(token)
    mandate = CheckoutMandate(checkout_jwt=token, checkout_hash=nonce, iat=now, exp=now + 300)
    chain = MandateClient().present(
        agent,
        opens.checkout.token,
        [mandate],
        aud=MERCHANT_ISSUER,
        nonce=nonce,
        hash_mode="issuer_jwt_hash",
    )
    sd_hash_chain = present_closed_mandate(
        opens.checkout.token,
        mandate,
        holder_key=agent,
        aud=MERCHANT_ISSUER,
        nonce=nonce,
        issued_at=now,
    )
    payment = closer(keys).close(
        opens.checkout.token,
        opens.payment.token,
        checkout_jwt=token,
        checkout=CheckoutSession.model_validate(checkout),
        closed_at=moment,
    )
    payment_chain = payment.payment.token

    accepted = closed_proof(keys, opens, (sd_hash_chain, payment_chain), token, now=moment)
    proof = closed_proof(keys, opens, (chain, payment_chain), token, now=moment)

    assert accepted == MandateProof(status="valid")
    assert (proof.status, proof.error) == ("invalid", "invalid_credential")


def test_the_profile_digest_is_a_sha256_reference() -> None:
    assert PROFILE_DIGEST.startswith("sha256:") and len(PROFILE_DIGEST) == 71
