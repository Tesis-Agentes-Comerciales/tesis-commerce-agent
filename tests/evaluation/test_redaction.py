"""What the evaluation runners keep or print never shows a secret (run-traceability.md §4.2).

Every secret here is generated in the test; none is real.
"""

import json
import os
import secrets
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from commerce_evaluation import governance_pilot, hnp_pilot
from commerce_evaluation.governance_pilot import (
    AGENT_SECRETS,
    INVOCATION_SECRETS,
    SERVICE_SECRETS,
    PilotError,
    _ap2_private_key,
)
from commerce_evaluation.hnp_pilot_episode import _measure
from commerce_evaluation.redaction import REDACTED, redact, secret_values, sweep_fields

# Shaped like the real ones: a three-part JWT, an OpenAI project key and an opaque bearer token.
JWT = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
OPENAI_KEY = "sk-proj-" + "A1b2C3d4" * 6
BEARER = secrets.token_urlsafe(32)


def test_every_known_secret_is_removed_also_json_escaped_and_line_by_line() -> None:
    pem = _ap2_private_key()
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
    ids=["bearer", "jwt", "openai-key", "pem", "pem-json-escaped-and-cut", "postgres-url"],
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


def test_the_invocation_secrets_cover_section_4_2() -> None:
    assert set(AGENT_SECRETS) | set(SERVICE_SECRETS) <= set(INVOCATION_SECRETS)
    assert {"OPENAI_API_KEY", "BETTER_AUTH_SECRET"} <= set(INVOCATION_SECRETS)
    environment = {"OPENAI_API_KEY": OPENAI_KEY, "BETTER_AUTH_SECRET": "", "PATH": "/bin"}
    assert secret_values(environment, INVOCATION_SECRETS) == (OPENAI_KEY,)


def test_a_failed_command_s_stderr_reaches_the_pilot_error_redacted(tmp_path: Path) -> None:
    """The merchant CLI's raw stderr goes into PilotError, harness_error and the runner's stderr."""
    service = secrets.token_urlsafe(32)
    script = f"import sys; sys.stderr.write('boom {service} Bearer {BEARER} {JWT}'); sys.exit(2)"
    environment = {"PAYMENT_TOKEN_SECRET": service, "PATH": os.environ["PATH"]}

    with pytest.raises(PilotError) as raised:
        governance_pilot._run(
            [sys.executable, "-c", script], cwd=tmp_path, env=environment, capture=True
        )

    message = str(raised.value)
    assert "boom" in message
    for secret in (service, BEARER, JWT):
        assert secret not in message


@pytest.mark.parametrize("runner", ["governance-pilot", "hnp-pilot"])
def test_the_runner_s_last_error_line_is_redacted(
    runner: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*_: object) -> int:
        raise PilotError(f"Command failed (uv): Authorization: Bearer {BEARER} {JWT}")

    if runner == "governance-pilot":
        monkeypatch.setattr(governance_pilot, "_run_pilot", fail)
        assert governance_pilot.main() == 1
    else:
        monkeypatch.setattr(hnp_pilot, "_run", fail)
        assert hnp_pilot.main(["--dry-run"]) == 1

    error = capsys.readouterr().err
    assert "failed: Command failed (uv)" in error
    assert BEARER not in error and JWT not in error


def test_the_log_tail_also_hides_secret_shapes(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    log.write_text(f"auth {JWT}\nkey {OPENAI_KEY}\n", encoding="utf-8")

    tail = governance_pilot._tail(log, ())

    assert JWT not in tail and OPENAI_KEY not in tail
    assert tail.count(REDACTED) == 2


def test_a_failed_reading_names_where_its_input_failed_never_the_input() -> None:
    """A Pydantic error quotes its input cut short, a fragment no sweep recognizes as a secret."""
    key = secrets.token_hex(32)

    class Reading(BaseModel):
        count: int

    errors: dict[str, str] = {}
    assert (
        _measure(errors, "ledger", lambda: Reading.model_validate({"count": f"x {key} y"})) is None
    )
    assert errors == {"ledger": "ValidationError: count: int_parsing"}
    assert _measure(errors, "order", lambda: int("not a count")) is None
    assert errors["order"] == "ValueError: invalid literal for int() with base 10: 'not a count'"
