"""Private experiment worker. Real HTTP goes to Merchant, never through the pipe.

The pipe only provisions synthetic fixture secrets in memory and starts backend
actions. No public Agent API or Settings enforce assembly is introduced.
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.authority_observation import AuthorityObservationClient, AuthoritySession
from commerce_agent.acp.guarded_creation import HttpGuardedCheckout
from commerce_agent.acp.offer_snapshot import DeliveryContext
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.preselection import PreselectionAuthority
from commerce_agent.governance.preselection_d1 import PreselectionD1Request
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    D1Configuration,
    D1EnforceConfiguration,
    ProvenanceKeys,
)
from commerce_agent.governance.provisioning import CatalogSource, EpisodeGrant
from commerce_agent.persistence.guarded_d1_sqlite import SqliteGuardedD1
from commerce_agent.persistence.preselection_d1_sqlite import SqlitePreselectionD1
from commerce_agent.persistence.preselection_sqlite import SqlitePreselectionStore
from commerce_agent.persistence.provisioning_sqlite import SqliteTestProvisioningRegistry


def assemble(data, fresh):
    root = Path(data["directory"]).resolve()
    assert root.is_relative_to(Path(__file__).resolve().parents[2] / ".local")
    root.mkdir(parents=True, exist_ok=True)
    seed = bytes.fromhex(data["ephemeral_secret"])

    def key(domain):
        return SecretBytes(hashlib.sha256(seed + domain.encode()).digest())

    origin = data["origin"]
    allowed = {int(origin.rsplit(":", 1)[1])}

    def audit(event, args):
        if event == "socket.connect" and (args[1][0] != "127.0.0.1" or args[1][1] not in allowed):
            raise RuntimeError("Agent external socket rejected")
        if (
            event == "sqlite3.connect"
            and args[0] != ":memory:"
            and not Path(args[0]).resolve().is_relative_to(root)
        ):
            raise RuntimeError("Agent foreign SQLite rejected")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            p = Path(os.fsdecode(args[0])).resolve()
            flags = args[2] or 0
            if p.name == ".env" or (
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                and not p.is_relative_to(root)
                and p.name.lower() != "nul"
            ):
                raise RuntimeError("Agent foreign file rejected")

    sys.addaudithook(audit)
    source = SqlitePreselectionStore(root / "source.sqlite", key("p"))
    registry = SqliteTestProvisioningRegistry(root / "registry.sqlite", key("registry"))
    profile = ClockProfile(
        authority_source="system_utc",
        commercial_source="system_utc",
        authority_provider=None,
        commercial_provider=None,
        fixed_commercial_at=None,
    )
    common = dict(
        merchant_base_url=origin,
        policy_set_id="b-live-policy",
        policy_revision=1,
        currency="COP",
        quantity=1,
        clock=profile,
    )
    keys = ProvenanceKeys(
        key_id="b-live", pseudonym_key=key("pseudonym"), encryption_key=key("encryption")
    )
    shadow = SqlitePreselectionD1(
        root / "shadow.sqlite",
        source=source,
        registry=registry,
        bearer=SecretStr(data["token"]),
        custody_key=key("q"),
        keys=keys,
        configuration=D1Configuration.model_validate({**common, "governance_mode": "shadow"}),
        clocks=ClockReader(profile),
    )
    if fresh:
        source.initialize()
        registry.initialize()
        session = registry.issue_test_session(
            CatalogSource(
                event_id=str(uuid4()),
                merchant_origin=origin,
                merchant_id=data["merchant_id"],
                remote_run_id=data["run_id"],
                remote_actor_id=data["actor_id"],
            ),
            SecretStr(data["token"]),
        )
        observation = AuthorityObservationClient(session)
        try:
            flow = PreselectionAuthority(observation, source)
            episode = flow.begin(
                owner_id="b-experimental-owner",
                intent=PurchaseIntent(),
                destination=DeliveryContext(country="CO", city="Bogota", postal_code="110111"),
            )
            seal = flow.capture(episode, owner_id="b-experimental-owner")
        finally:
            observation.close()
        store = shadow.initialize()
        registry.grant_test_episode(
            EpisodeGrant(
                event_id=str(uuid4()),
                episode_id=episode,
                owner_id=seal.episode.owner_id,
                d1_run_id=episode,
                d1_store_id=store,
                selection_digest=sha256_digest(canonical_bytes(seal)),
                receipt_ref=session.binding.source_ref,
            )
        )
        request = shadow.request(episode, seal.episode.owner_id)
        shadow.evaluate(request)
        (root / "request.json").write_bytes(canonical_bytes(request))
    else:
        request = parse_model(PreselectionD1Request, (root / "request.json").read_bytes())
        seal = source.recover(request.episode_id, request.owner_id)
        session = AuthoritySession(seal.episode.binding, SecretStr(data["token"]))
    port = HttpGuardedCheckout(session, timeout_seconds=10)
    applied = SqliteGuardedD1(
        root / "applied.sqlite",
        shadow=shadow,
        custody_key=key("b"),
        keys=keys,
        configuration=D1EnforceConfiguration.model_validate(
            {**common, "governance_mode": "enforce"}
        ),
        clocks=ClockReader(profile),
        transport=port,
    )
    applied.initialize()
    return applied, request


def main():
    case = request = None
    for line in sys.stdin:
        value = json.loads(line)
        try:
            action = value["action"]
            if action in {"setup", "restore"}:
                case, request = assemble(value["data"], action == "setup")
                result = {"pid": os.getpid(), "run_id": request.run_id}
            elif action == "close":
                if case:
                    case.transport.close()
                break
            else:
                assert case is not None and request is not None
                if action == "prepare":
                    result = case.prepare(request).model_dump(mode="json")
                elif action == "dispatch":
                    result = case.dispatch(request).model_dump(mode="json")
                elif action == "recover":
                    result = case.recover(request).model_dump(mode="json")
                elif action == "history":
                    result = case.history(request)
                elif action == "timeout":
                    case.transport._http.timeout = httpx.Timeout(value["seconds"])
                    result = True
                else:
                    raise RuntimeError("Agent command rejected")
            print(json.dumps({"ok": True, "result": result}), flush=True)
        except Exception as error:
            # No traceback, exception text, credential, request or header export.
            code = (
                str(error)
                if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", str(error))
                else getattr(error, "code", None)
            )
            if code is not None and not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", code):
                code = None
            diagnostic = {"ok": False, "error_type": type(error).__name__, "error_code": code}
            print(json.dumps(diagnostic), file=sys.stderr, flush=True)
            print(json.dumps(diagnostic), flush=True)


if __name__ == "__main__":
    main()
