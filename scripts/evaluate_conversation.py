import argparse
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


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
    Case("greeting", ({"role": "user", "content": "hola, ¿qué puedes hacer?"},), ("respond",)),
    Case(
        "browse-only",
        ({"role": "user", "content": "Solo quiero ver qué venden; no prepares una compra."},),
        ("respond",),
    ),
    Case("generic", ({"role": "user", "content": "quiero comprar algo"},), ("clarify",)),
    Case(
        "broad",
        ({"role": "user", "content": "quiero comprar unos audífonos"},),
        ("clarify",),
    ),
    Case("unsupported", ({"role": "user", "content": "quiero un celular"},), ("clarify",)),
    Case(
        "absent-color",
        ({"role": "user", "content": "quiero audífonos morados"},),
        ("clarify",),
    ),
    Case(
        "negated",
        (
            {
                "role": "user",
                "content": (
                    "quiero audífonos negros por menos de 800 USD, "
                    "pero no prepares una compra todavía"
                ),
            },
        ),
        ("respond", "clarify"),
    ),
    Case(
        "ambiguous-reference",
        (
            {"role": "user", "content": "sí, ese está bien"},
            {"role": "assistant", "content": "¿Quieres que proceda con ese artículo?"},
            {"role": "user", "content": "sí"},
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
            {"role": "user", "content": "quiero unos audífonos"},
            {
                "role": "assistant",
                "content": "Hay varias marcas, colores y precios. ¿Qué prefieres?",
            },
            {"role": "user", "content": "el más barato, no importa la marca"},
        ),
        ("purchase",),
        "ALT-01",
    ),
)

SELECTION_CLAIMS = (
    r"(?<!no )he seleccionado",
    r"(?<!no )seleccioné",
    r"(?<!no )he escogido",
    r"(?<!no )procederé con la compra",
    r"(?<!no )continúo con",
)


def request_json(base_url: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(
        f"{base_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"{path} returned HTTP {error.code}: {detail}") from error


def evaluate(base_url: str, case: Case) -> dict[str, Any]:
    turn = request_json(
        base_url,
        "/conversation/turn",
        {
            "turn_id": f"eval-{case.name}-{uuid4().hex}",
            "messages": case.messages,
            "details": DETAILS,
        },
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
        canceled = request_json(base_url, f"/purchases/{run_id}/cancel", {})
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
