"""Evaluate conversation turns of a running local agent with its configured LLM."""

import argparse
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from uuid import uuid4

from .redaction import redact


@dataclass(frozen=True)
class Case:
    name: str
    messages: tuple[dict[str, str], ...]
    actions: tuple[str, ...]
    variant_id: str | None = None


DETAILS = {
    "buyer": {
        "email": "buyer.eval@example.test",
        "first_name": "Buyer",
        "last_name": "Eval",
    },
    "fulfillment": {
        "name": "Buyer Eval",
        "email": "buyer.eval@example.test",
        "phone_number": "+573000000000",
        "address": {
            "name": "Buyer Eval",
            "line_one": "Calle 100 # 10-20",
            "city": "Bogota",
            "state": "DC",
            "country": "CO",
            "postal_code": "110111",
        },
    },
}

CASES = (
    Case("greeting", ({"role": "user", "content": "hola, Â¿quÃ© puedes hacer?"},), ("respond",)),
    Case(
        "browse-only",
        ({"role": "user", "content": "Solo quiero ver quÃ© venden; no prepares una compra."},),
        ("respond",),
    ),
    Case("generic", ({"role": "user", "content": "quiero comprar algo"},), ("clarify",)),
    Case(
        "broad",
        ({"role": "user", "content": "quiero comprar unos audÃ­fonos"},),
        ("clarify",),
    ),
    Case("unsupported", ({"role": "user", "content": "quiero un celular"},), ("clarify",)),
    Case(
        "absent-color",
        ({"role": "user", "content": "quiero audÃ­fonos morados"},),
        ("clarify",),
    ),
    Case(
        "negated",
        (
            {
                "role": "user",
                "content": (
                    "quiero audÃ­fonos negros por menos de 800 USD, "
                    "pero no prepares una compra todavÃ­a"
                ),
            },
        ),
        ("respond", "clarify"),
    ),
    Case(
        "ambiguous-reference",
        (
            {"role": "user", "content": "sÃ­, ese estÃ¡ bien"},
            {"role": "assistant", "content": "Â¿Quieres que proceda con ese artÃ­culo?"},
            {"role": "user", "content": "sÃ­"},
        ),
        ("clarify",),
    ),
    Case(
        "exact-model",
        ({"role": "user", "content": "quiero comprar exactamente el modelo ALT-01"},),
        ("purchase",),
        "ALT-01",
    ),
    Case(
        "cheapest-followup",
        (
            {"role": "user", "content": "quiero unos audÃ­fonos"},
            {
                "role": "assistant",
                "content": "Hay varias marcas, colores y precios. Â¿QuÃ© prefieres?",
            },
            {"role": "user", "content": "el mÃ¡s barato, no importa la marca"},
        ),
        ("purchase",),
        "ALT-01",
    ),
)

SELECTION_CLAIMS = (
    r"(?<!no )he seleccionado",
    r"(?<!no )seleccionÃ©",
    r"(?<!no )he escogido",
    r"(?<!no )procederÃ© con la compra",
    r"(?<!no )continÃºo con",
)


class TokenSource(Protocol):
    """`auth_client.AuthClient`; credentials remain in the HTTP boundary."""

    def token(self) -> str: ...


class ConversationHTTPError(RuntimeError):
    def __init__(self, status_code: int, body: dict[str, Any]):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Conversation endpoint returned HTTP {status_code}: {json.dumps(body)}")


class ConversationTransportError(RuntimeError):
    """An uncertain technical result, never a failed commercial condition."""


class _NoAuthenticatedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    auth: TokenSource | None = None,
) -> dict[str, Any]:
    token = auth.token() if auth is not None else None
    if auth is not None and (not isinstance(token, str) or not token.strip()):
        raise ConversationTransportError("Authenticated execution requires a nonempty buyer token")
    values = (token,) if token else ()
    body = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(
        base_url.rstrip("/") + path,
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    # Authenticated redirects are refused instead of forwarding the buyer's bearer elsewhere.
    open_request = build_opener(_NoAuthenticatedRedirect()).open if auth is not None else urlopen
    try:
        with open_request(request, timeout=120) as response:
            raw = response.read().decode()
    except HTTPError as error:
        safe = redact(error.read().decode(errors="replace"), values)
        try:
            detail = json.loads(safe)
        except ValueError:
            detail = {"unparseable_body": safe}
        raise ConversationHTTPError(
            error.code, detail if isinstance(detail, dict) else {"value": detail}
        ) from None
    except (URLError, TimeoutError, OSError):
        raise ConversationTransportError("conversation_transport_unavailable") from None
    try:
        value = json.loads(redact(raw, values))
    except ValueError:
        raise ConversationTransportError("conversation_response_invalid") from None
    if not isinstance(value, dict):
        raise ConversationTransportError("conversation_response_invalid")
    return value


def evaluate(base_url: str, case: Case, *, auth: TokenSource | None = None) -> dict[str, Any]:
    turn = request_json(
        base_url,
        "/conversation/turn",
        {
            "turn_id": f"eval-{case.name}-{uuid4().hex}",
            "messages": case.messages,
            "details": DETAILS,
        },
        auth=auth,
    )
    action = turn.get("action")
    message = str(turn.get("assistant_message", ""))
    selected = (turn.get("purchase") or {}).get("selected_candidate", {}).get("variant_id")
    errors: list[str] = []
    if action not in case.actions:
        errors.append(f"expected action {case.actions}, got {action!r}")
    if case.variant_id is not None and selected != case.variant_id:
        errors.append(f"expected variant {case.variant_id}, got {selected!r}")
    if action != "purchase" and turn.get("purchase") is not None:
        errors.append("non-purchase action returned purchase data")
    if action == "clarify" and any(
        re.search(claim, message.casefold()) for claim in SELECTION_CLAIMS
    ):
        errors.append("clarification claims that a product was already selected")
    if turn.get("purchase") is not None:
        run_id = turn["purchase"]["run_id"]
        canceled = request_json(base_url, f"/purchases/{run_id}/cancel", {}, auth=auth)
        if canceled.get("stage") != "canceled":
            errors.append("sandbox checkout was not canceled")
    return {
        "case": case.name,
        "passed": not errors,
        "action": action,
        "selected_variant_id": selected,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the local agent with its configured LLM")
    parser.add_argument("--base-url", default="http://127.0.0.1:43120")
    parser.add_argument("--broad-repetitions", type=int, default=5)
    arguments = parser.parse_args()
    base_url = arguments.base_url.rstrip("/")
    cases = tuple(case for case in CASES if case.name != "broad") + tuple(
        Case(f"broad-{index + 1}", CASES[3].messages, ("clarify",))
        for index in range(arguments.broad_repetitions)
    )
    results = [evaluate(base_url, case) for case in cases]
    for result in results:
        print(json.dumps(result, ensure_ascii=False))
    failures = sum(not result["passed"] for result in results)
    print(json.dumps({"passed": len(results) - failures, "failed": failures}))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
