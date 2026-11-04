import json
from zipfile import ZipFile

from tests.support.browser_trace import REDACTED, sanitize_trace


def test_trace_redacts_credentials_and_preserves_business_evidence(tmp_path):
    raw, target = tmp_path / "raw.zip", tmp_path / "trace.zip"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJidXllciJ9.signature"
    events = [
        {"params": {"selector": 'getByLabel("Contraseña")', "value": "synthetic-password"}},
        {
            "headers": [{"name": "Authorization", "value": "Bearer opaque-secret"}],
            "cookies": [{"name": "session", "value": "opaque-cookie"}],
        },
        {"password": "synthetic-password", "receipt": jwt, "total_minor": 12300},
        {"kind": "policy_decision", "outcome": "allow", "aria-expanded": "true"},
    ]
    with ZipFile(raw, "w") as archive:
        archive.writestr("trace.network", "\n".join(json.dumps(item) for item in events))
        archive.writestr("resources/body.json", json.dumps({"access_token": "opaque-secret"}))
        archive.writestr("resources/snapshot.html", "synthetic-password opaque-secret")
        archive.writestr("resources/frame.png", b"\x89PNG\xff")
    manifest = sanitize_trace(raw, target)
    with ZipFile(target) as archive:
        text = archive.read("trace.network").decode()
        assert "synthetic-password" not in text and "opaque-secret" not in text and jwt not in text
        observed = [json.loads(line) for line in text.splitlines()]
        assert observed[0]["params"]["value"] == REDACTED
        assert observed[1]["headers"][0]["value"] == REDACTED
        assert isinstance(observed[1]["cookies"], list)
        assert observed[1]["cookies"][0]["value"] == REDACTED
        assert observed[2]["total_minor"] == 12300
        assert observed[3] == events[3]
        assert json.loads(archive.read("resources/body.json"))["access_token"] == REDACTED
        assert archive.read("resources/snapshot.html") == f"{REDACTED} {REDACTED}".encode()
        assert archive.read("resources/frame.png") == b"\x89PNG\xff"
    assert len(str(manifest["sha256"])) == 64 and manifest["redacted_resources"] == 3
