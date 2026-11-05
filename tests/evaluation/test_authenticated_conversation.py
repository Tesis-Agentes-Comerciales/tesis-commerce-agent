"""Transport contracts only: scripted HTTP bytes never count as semantic UX evidence."""

import io
import json
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from commerce_evaluation import conversation as client


class SyntheticAuth:
    def __init__(self, value="synthetic-unit-token-not-a-real-credential"):
        self.calls = 0
        self.value = value

    def token(self):
        self.calls += 1
        return self.value


def test_auth_is_header_only_refreshed_and_used_for_cancellation(monkeypatch):
    calls = []
    auth = SyntheticAuth()
    bodies = [
        {
            "action": "purchase",
            "assistant_message": "x",
            "purchase": {
                "run_id": "synthetic-run",
                "selected_candidate": {"variant_id": "fixture-only"},
            },
        },
        {"stage": "canceled"},
    ]

    def open_request(request, timeout):
        calls.append(request)
        assert timeout == 120
        return io.BytesIO(json.dumps(bodies.pop(0)).encode())

    monkeypatch.setattr(client, "build_opener", lambda *_: SimpleNamespace(open=open_request))
    case = client.Case("transport-unit", ({"role": "user", "content": "fixture"},), ("purchase",))
    result = client.evaluate("http://127.0.0.1:9", case, auth=auth)
    assert result["passed"] and auth.calls == 2 and len(calls) == 2
    assert calls[0].get_header("Authorization") == "Bearer " + auth.token()
    assert all(b"synthetic-unit-token" not in c.data for c in calls)
    assert calls[1].full_url.endswith("/purchases/synthetic-run/cancel")


@pytest.mark.parametrize("status", [401, 503, 302])
def test_http_errors_preserve_status_and_remove_reflected_token(monkeypatch, status):
    auth = SyntheticAuth()
    token = auth.token()

    def open_request(request, timeout):
        raise HTTPError(
            request.full_url,
            status,
            "failure",
            Message(),
            io.BytesIO(json.dumps({"echo": token}).encode()),
        )

    monkeypatch.setattr(client, "build_opener", lambda *_: SimpleNamespace(open=open_request))
    with pytest.raises(client.ConversationHTTPError) as error:
        client.request_json("http://127.0.0.1:9", "/conversation/turn", {}, auth=auth)
    assert error.value.status_code == status
    assert token not in str(error.value) and token not in json.dumps(error.value.body)


def test_missing_token_network_and_invalid_json_are_errors_not_commercial_false(monkeypatch):
    with pytest.raises(client.ConversationTransportError):
        client.request_json("http://127.0.0.1:9", "/", auth=SyntheticAuth(""))

    def failure(request, timeout):
        raise URLError("network unavailable")

    monkeypatch.setattr(client, "build_opener", lambda *_: SimpleNamespace(open=failure))
    with pytest.raises(client.ConversationTransportError, match="transport_unavailable"):
        client.request_json("http://127.0.0.1:9", "/", auth=SyntheticAuth())
    monkeypatch.setattr(
        client, "build_opener", lambda *_: SimpleNamespace(open=lambda *a, **k: io.BytesIO(b"[]"))
    )
    with pytest.raises(client.ConversationTransportError, match="response_invalid"):
        client.request_json("http://127.0.0.1:9", "/", auth=SyntheticAuth())


def test_auth_failure_does_not_retry_without_credentials(monkeypatch):
    class FailingAuth:
        def token(self):
            raise RuntimeError("synthetic auth failed")

    def must_not_open(*a, **k):
        pytest.fail("must not fall back to unauthenticated HTTP")

    monkeypatch.setattr(client, "build_opener", must_not_open)
    with pytest.raises(RuntimeError, match="synthetic auth failed"):
        client.request_json("http://127.0.0.1:9", "/", auth=FailingAuth())
