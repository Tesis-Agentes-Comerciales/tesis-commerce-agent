"""What the evaluation runners keep or print never shows a secret (run-traceability.md §4.2).

Every secret here is generated in the test; none is real.
"""

import json
import secrets

import pytest

from commerce_evaluation.agent_condition import ap2_private_key
from commerce_evaluation.redaction import REDACTED, redact, sweep_fields

# Shaped like the real ones: a three-part JWT, model API keys and an opaque bearer token.
JWT = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
ANTHROPIC_KEY = "sk-ant-api03-" + "A1b2C3d4_-" * 9
OPENAI_KEY = "sk-proj-" + "A1b2C3d4" * 6
BEARER = secrets.token_urlsafe(32)


def test_every_known_secret_is_removed_also_json_escaped_and_line_by_line() -> None:
    pem = ap2_private_key()
    hmac = secrets.token_hex(32)
    text = "\n".join(
        [
            f"hmac={hmac}",
            f"settings: {json.dumps({'key': pem})}",
            # A traceback that printed only the second line of the key.
            f"line {pem.splitlines()[1]}",
        ]
    )

    redacted = redact(text, [hmac, pem])

    assert hmac not in redacted
    for line in pem.splitlines()[1:-1]:
        assert line not in redacted
    assert redacted.count(REDACTED) >= 3


@pytest.mark.parametrize(
    ("leak", "redacted"),
    [
        (f"Authorization: Bearer {BEARER}", f"Authorization: Bearer {REDACTED}"),
        (f"token={JWT} rest", f"token={REDACTED} rest"),
        (f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}", f"ANTHROPIC_API_KEY={REDACTED}"),
        (f"OPENAI_API_KEY={OPENAI_KEY}", f"OPENAI_API_KEY={REDACTED}"),
        (
            "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMG\n-----END PRIVATE KEY-----\nnext",
            f"{REDACTED}\nnext",
        ),
        ('"-----BEGIN PRIVATE KEY-----\\nMIGHAgEAMBMG\\n', f'"{REDACTED}'),
        (
            "connection to postgresql://user:pw@db:5432/lab failed",
            f"connection to {REDACTED} failed",
        ),
    ],
    ids=[
        "bearer",
        "jwt",
        "anthropic-key",
        "openai-key",
        "pem",
        "pem-json-escaped-and-cut",
        "postgres-url",
    ],
)
def test_secret_shapes_are_removed_without_knowing_their_value(leak: str, redacted: str) -> None:
    assert redact(leak) == redacted


def test_prose_about_tokens_is_not_redacted() -> None:
    text = "The merchant session command did not return a token; missing Bearer token"

    assert redact(text) == text


def test_the_marker_can_be_the_demo_s_spanish_one() -> None:
    assert redact("key=abcdefabcdefabcdef", ["abcdefabcdefabcdef"], marker="[redactado]") == (
        "key=[redactado]"
    )


def test_a_row_field_keeps_its_diagnosis_without_the_secret() -> None:
    """Only the secret goes: the rest of the message still says what failed."""
    token = secrets.token_urlsafe(32)
    row = {
        "harness_error": f"PilotError: Command failed (uv): token {token}",
        "error_response": {"code": "d1_denied", "detail": [f"Bearer {BEARER}", "kept"]},
        "measurement_errors": {"psp_payments": "OperationalError: locked", "x": f"jwt {JWT}"},
        "assistant_message": f"not swept {token}",
    }

    swept = sweep_fields(row, ("harness_error", "error_response", "measurement_errors"), [token])

    assert swept["harness_error"] == f"PilotError: Command failed (uv): token {REDACTED}"
    assert swept["error_response"] == {
        "code": "d1_denied",
        "detail": [f"Bearer {REDACTED}", "kept"],
    }
    assert swept["measurement_errors"] == {
        "psp_payments": "OperationalError: locked",
        "x": f"jwt {REDACTED}",
    }
    assert swept["assistant_message"] == row["assistant_message"]
    assert "harness_error" not in sweep_fields({}, ("harness_error",), [token])
