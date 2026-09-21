import hashlib
import hmac
import re

SIGNATURE_TOLERANCE_SECONDS = 300
_SIGNATURE_PATTERN = re.compile(r"t=(\d+),v1=([0-9a-fA-F]{64})")


class WebhookSignatureError(ValueError):
    pass


def verify_signature(
    header: str | None,
    *,
    secret: str,
    raw_body: bytes,
    now: float,
    tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
) -> None:
    if header is None:
        raise WebhookSignatureError("Missing Merchant-Signature header")
    match = _SIGNATURE_PATTERN.fullmatch(header)
    if match is None:
        raise WebhookSignatureError("Malformed Merchant-Signature header")

    timestamp = int(match.group(1))
    if abs(now - timestamp) > tolerance_seconds:
        raise WebhookSignatureError("Webhook timestamp is outside the allowed window")

    signed_payload = str(timestamp).encode("ascii") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, match.group(2).lower()):
        raise WebhookSignatureError("Webhook signature verification failed")
