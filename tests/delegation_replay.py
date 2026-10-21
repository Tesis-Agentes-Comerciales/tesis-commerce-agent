"""Real delegation API exchanges for the UI's Playwright replay (HNP-10).

Shared by tests/test_delegation_replay.py and scripts/export_delegation_replays.py. Each scenario
drives the app of tests/test_delegation_api.py (real SQLite, the assistant feed the chat streams
show, a settable authority clock) and records every request with its status and body, so the UI
replays what the backend answers and in that order. Ids come from a seeded generator: the files
are byte-stable. The request details are the UI's `checkoutDetails(DEMO_PROFILE)`, with the address
the merchant serves (Bogota, DC, CO, 110111).
"""

import json
import random
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger
from tests.assistant_support import ASSISTANT_FEED
from tests.support.delegation import AuthorityClock, Flow
from tests.test_delegation_api import build, buyer

REPLAY_DIR = (
    Path(__file__).resolve().parents[1] / "ui" / "src" / "storybook" / "fixtures" / "delegation-api"
)
OWNER = buyer()

# ui/src/lib/commerce-types.ts DEMO_PROFILE through checkoutDetails(): the one destination the
# merchant serves, as it echoes it.
DETAILS = {
    "buyer": {"email": "ana@example.test", "first_name": "Ana", "last_name": "Demo"},
    "fulfillment": {
        "name": "Ana Demo",
        "email": "ana@example.test",
        "phone_number": "+573000000000",
        "address": {
            "name": "Ana Demo",
            "line_one": "Calle 100 # 10-20",
            "city": "Bogota",
            "state": "DC",
            "country": "CO",
            "postal_code": "110111",
        },
    },
}
PROPOSAL = {
    "variant_ids": ["BOS-QCU-BLK", "SON-XM6-BLK"],
    "total_max_minor": 200_000_000,
    "currency": "COP",
    "valid_for_seconds": 3_600,
    "details": DETAILS,
    "preference_text": "Si los dos están disponibles, prefiero los Sony.",
}


def with_city(city: str) -> dict[str, Any]:
    """The proposal with the city as the buyer typed it; the UI sends it unchanged."""
    address = DETAILS["fulfillment"]["address"] | {"city": city}
    fulfillment = DETAILS["fulfillment"] | {"address": address}
    return PROPOSAL | {"details": DETAILS | {"fulfillment": fulfillment}}


@dataclass
class Session:
    """One app on its own database; `exchange` records a request and its answer."""

    client: TestClient
    ledger: SqliteDelegationLedger
    clock: AuthorityClock
    exchanges: list[dict[str, Any]] = field(default_factory=list)

    def exchange(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {} if path == "/health" else OWNER
        response = self.client.request(method, path, json=body, headers=headers)
        self.exchanges.append(
            {
                "method": method,
                "path": path,
                "request": body,
                "status": response.status_code,
                "body": response.json(),
            }
        )
        return response.json()

    def delegate(self, **changes: Any) -> dict[str, Any]:
        """A consented delegation, outside the replay: the setup the buyer did before."""
        proposal = self.client.post(
            "/delegations/proposals", json=PROPOSAL | changes, headers=OWNER
        ).json()
        consent = {key: proposal[key] for key in ("proposal_id", "scope_digest")}
        view = self.client.post("/delegations", json=consent, headers=OWNER).json()
        self.clock.advance(1)
        return view

    def flow(self, view: dict[str, Any]) -> Flow:
        return Flow(self.ledger, self.clock, view["task"]["task_id"], view["grant_id"])


@dataclass(frozen=True)
class Replay:
    name: str
    description: str
    record: Callable[[Session], None]
    hnp_enabled: bool = True


@contextmanager
def seeded_ids() -> Iterator[None]:
    """`uuid4` of the service and the ledger from a fixed seed: the same ids on every export."""
    generator = random.Random(20261021)

    def seeded() -> uuid.UUID:
        return uuid.UUID(int=generator.getrandbits(128), version=4)

    with (
        patch("commerce_agent.delegation.service.uuid4", seeded),
        patch("commerce_agent.persistence.delegation_sqlite.uuid4", seeded),
    ):
        yield


def consent_flow(session: Session) -> None:
    session.exchange("GET", "/health")
    session.exchange("GET", "/me/delegations")
    # Typed with the accent: the proposal binds the merchant's spelling (hnp-governed.md §5.1).
    proposal = session.exchange("POST", "/delegations/proposals", with_city("Bogotá"))
    session.clock.advance(30)
    consent = {key: proposal[key] for key in ("proposal_id", "scope_digest")}
    session.exchange("POST", "/delegations", consent)
    session.exchange("GET", "/me/delegations")


def scope_mismatch(session: Session) -> None:
    proposal = session.exchange("POST", "/delegations/proposals", PROPOSAL)
    tampered = {"proposal_id": proposal["proposal_id"], "scope_digest": "sha256:" + "0" * 64}
    session.exchange("POST", "/delegations", tampered)


def proposal_expired(session: Session) -> None:
    proposal = session.exchange(
        "POST", "/delegations/proposals", PROPOSAL | {"valid_for_seconds": 60}
    )
    session.clock.advance(60)
    consent = {key: proposal[key] for key in ("proposal_id", "scope_digest")}
    session.exchange("POST", "/delegations", consent)


def destination_unsupported(session: Session) -> None:
    session.exchange("GET", "/me/delegations")
    session.exchange("POST", "/delegations/proposals", with_city("Medellín"))


def three_active(session: Session) -> tuple[dict[str, Any], ...]:
    """Idle, with a reservation held (`released`) and committed to dispatch (`in_flight`)."""
    idle = session.delegate()
    held = session.delegate()
    in_flight = session.delegate()
    held_flow = session.flow(held)
    held_flow.claim()
    held_flow.reserve(2)
    session.flow(in_flight).committed()
    return idle, held, in_flight


def revoke_each(session: Session) -> None:
    delegations = three_active(session)
    session.exchange("GET", "/me/delegations")
    for view in delegations:
        session.clock.advance(5)
        session.exchange("POST", f"/delegations/{view['grant_id']}/revoke")
        session.exchange("GET", f"/delegations/{view['grant_id']}")


def revoke_all(session: Session) -> None:
    three_active(session)
    closed = session.delegate()
    session.client.post(f"/delegations/{closed['grant_id']}/revoke", headers=OWNER)
    session.exchange("GET", "/me/delegations")
    session.clock.advance(5)
    session.exchange("POST", "/me/delegations/revoke-all")
    session.exchange("GET", "/me/delegations")


def not_revocable(session: Session) -> None:
    view = session.delegate()
    session.exchange("GET", "/me/delegations")
    session.clock.advance(3_600)
    session.exchange("POST", f"/delegations/{view['grant_id']}/revoke")
    session.exchange("GET", f"/delegations/{view['grant_id']}")


def flag_off(session: Session) -> None:
    session.exchange("GET", "/health")
    session.exchange("GET", "/me/delegations")
    session.exchange("POST", "/delegations/proposals", PROPOSAL)


REPLAYS = (
    Replay(
        "consent",
        "Consentimiento exitoso: sin delegaciones, propuesta con la ciudad escrita «Bogotá» (el "
        "alcance dice «Bogota, DC»), «Autorizar» 30 s después (201) y la lista con la delegación "
        "activa.",
        consent_flow,
    ),
    Replay(
        "scope-mismatch",
        "El servidor no reconoce el digest del alcance (409 `delegation_scope_mismatch`).",
        scope_mismatch,
    ),
    Replay(
        "proposal-expired",
        "Propuesta con `valid_for_seconds = 60`: la propuesta vale 10 min (TTL), pero el grant "
        "vence al minuto, y «Autorizar» al minuto responde 409 `delegation_proposal_expired`.",
        proposal_expired,
    ),
    Replay(
        "destination-unsupported",
        "Un destino que el comercio no atiende (Medellín): 422 "
        "`delegation_destination_unsupported`, sin propuesta.",
        destination_unsupported,
    ),
    Replay(
        "revoke",
        "Tres delegaciones activas: sin reserva, con la reserva tomada y con el pago comprometido. "
        "Revocar cada una devuelve `no_effect`, `released` e `in_flight`, y la vista releída.",
        revoke_each,
    ),
    Replay(
        "revoke-all",
        "Las mismas tres y una ya revocada: «Revocar todo» devuelve un recibo por cada activa y la "
        "lista releída.",
        revoke_all,
    ),
    Replay(
        "not-revocable",
        "La lista muestra una delegación activa que vence antes de revocarla: 409 "
        "`delegation_not_revocable` y la vista releída, vencida.",
        not_revocable,
    ),
    Replay(
        "flag-off",
        "`AGENT_HNP_ENABLED=false`: `/health` sin campos `hnp_*` y las rutas de delegación en 404.",
        flag_off,
        hnp_enabled=False,
    ),
)


def record(replay: Replay, workdir: Path) -> list[dict[str, Any]]:
    clock = AuthorityClock()
    app = build(
        workdir, clock, agent_feed_path=ASSISTANT_FEED, agent_hnp_enabled=replay.hnp_enabled
    )
    with seeded_ids(), TestClient(app) as client:
        ledger = SqliteDelegationLedger(workdir / "agent.db", clock=clock)
        session = Session(client, ledger, clock)
        replay.record(session)
    return session.exchanges


def replay_readme() -> str:
    lines = [
        "# Respuestas reales de la API de delegaciones",
        "",
        "Generadas por `uv run python scripts/export_delegation_replays.py`; no se editan a mano.",
        "`tests/test_delegation_replay.py` verifica que coincidan con la salida actual.",
        "",
        "Cada archivo es la secuencia de peticiones de un escenario con la respuesta real del",
        "backend (`tests/delegation_replay.py`): la app de `tests/test_delegation_api.py` con",
        "SQLite real, el feed de `tests/fixtures/assistant_feed` (el mismo de los streams del",
        "chat), el reloj de autoridad en 2026-10-21T15:00:00Z e ids deterministas. Las tareas",
        "avanzan por el ledger, como en esas pruebas. `ui/e2e/delegations.spec.ts` las reproduce",
        "en orden.",
        "",
    ]
    for replay in REPLAYS:
        lines += [f"## `{replay.name}.json`", "", replay.description, ""]
    return "\n".join(lines)


def replay_files(workdir: Path) -> dict[str, str]:
    files = {}
    for replay in REPLAYS:
        directory = workdir / replay.name
        directory.mkdir()
        exchanges = record(replay, directory)
        files[f"{replay.name}.json"] = json.dumps(exchanges, ensure_ascii=False, indent=2) + "\n"
    files["README.md"] = replay_readme()
    return files
