"""Opt-in real Merchant/PG with controlled pipe→ASGI HTTP, not HTTP sockets."""

import json
import os
import secrets
import socket
import subprocess
from pathlib import Path

import httpx
import pytest
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.authority_observation import (
    AuthorityObservationClient,
    AuthoritySession,
    CatalogBinding,
    ObservationBlocked,
)
from commerce_agent.acp.offer_snapshot import DeliveryContext, MerchantOffer, offer_snapshot_sha256
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.models import ObjectRef
from commerce_agent.governance.preselection import PreselectionAuthority, projected_candidate
from commerce_agent.governance.remote_creation import SessionScope
from commerce_agent.persistence.preselection_sqlite import SqlitePreselectionStore
from tests.integration.gov06dc_harness import clean_environment

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_GOV07BP_REAL") != "1", reason="requires new isolated Merchant/PostgreSQL cluster"
)
ROOT = Path(__file__).resolve().parents[2]
MERCHANT = Path("C:/ANDES/Tesis/tesis-commerce-merchant")
PG = Path("C:/Program Files/PostgreSQL/17/bin")


class PipeTransport(httpx.BaseTransport):
    def __init__(self, live):
        self.live = live

    def handle_request(self, request):
        result = self.live.rpc(
            "http",
            method=request.method,
            path=request.url.path,
            headers=dict(request.headers),
            content=request.content.decode(),
        )
        return httpx.Response(
            result["status"],
            headers=result["headers"],
            content=result["content"].encode(),
            request=request,
        )


class MerchantFixture:
    def __init__(self):
        assert (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=MERCHANT, text=True).strip()
            == "9743c9f179c7ad5413e8818766ef3dceb326a917"
        )
        assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=MERCHANT)
        self.root = ROOT / ".local" / ("07bp-real-" + secrets.token_hex(6))
        self.root.mkdir(parents=True, exist_ok=False)
        self.data = self.root / "pgdata"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        assert self.port not in {4111, 4120, 4130, 55432, 55433, 62281}
        self.env = clean_environment()
        self.env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PGCONNECT_TIMEOUT="5")
        self.started = False
        self.worker = None
        self.error_log = None

    def command(self, executable, *args):
        path = self.root / ("command-" + secrets.token_hex(4))
        with path.with_suffix(".out").open("wb") as out, path.with_suffix(".err").open("wb") as err:
            result = subprocess.run(
                [str(executable), *map(str, args)],
                env=self.env,
                stdout=out,
                stderr=err,
                timeout=40,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        assert result.returncode == 0, "Owned PostgreSQL command failed"
        return path.with_suffix(".out").read_text(errors="replace").strip()

    def start(self):
        self.command(
            PG / "initdb.exe",
            "-D",
            self.data,
            "-U",
            "p07_admin",
            "-A",
            "trust",
            "--encoding=UTF8",
            "--no-locale",
        )
        (self.data / "pg_hba.conf").write_text(
            "host all p07_admin 127.0.0.1/32 trust\nhost tesis_lab p07_app 127.0.0.1/32 trust\n"
        )
        with (self.data / "postgresql.conf").open("a") as out:
            out.write("\ntimezone='UTC'\nlog_timezone='UTC'\n")
        self.started = True
        self.command(
            PG / "pg_ctl.exe",
            "-D",
            self.data,
            "-l",
            self.root / "postgres.log",
            "-o",
            f"-h 127.0.0.1 -p {self.port}",
            "-w",
            "start",
        )

        def psql(sql):
            return self.command(
                PG / "psql.exe",
                "-X",
                "-qAt",
                "-h",
                "127.0.0.1",
                "-p",
                self.port,
                "-U",
                "p07_admin",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            )

        identity = json.loads(
            psql(
                "SELECT json_build_array(current_database(),current_user,"
                "inet_server_port(),current_setting('data_directory'))"
            )
        )
        assert identity[:3] == ["postgres", "p07_admin", self.port]
        assert Path(identity[3]).resolve() == self.data.resolve()
        psql("CREATE ROLE p07_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        psql("CREATE DATABASE tesis_lab OWNER p07_app")
        psql("GRANT pg_read_all_settings TO p07_app")
        self.origin = (
            "http://127.0.0.1:49999"  # Origin identity only: transport uses ASGI, no listener.
        )
        self.env.update(
            DATABASE_URL=f"postgresql://p07_app:{secrets.token_hex(20)}@127.0.0.1:{self.port}/tesis_lab",
            ACP_API_BASE_URL=self.origin,
            P07_MERCHANT=str(MERCHANT),
            P07_PORT=str(self.port),
            P07_DATA=str(self.data),
            P07_OUTPUT=str(self.root),
        )
        self.error_log = (self.root / "worker-stderr.txt").open("w")
        self.worker = subprocess.Popen(
            [
                str(MERCHANT / ".venv/Scripts/python.exe"),
                "-B",
                str(Path(__file__).with_name("gov07bp_merchant_worker.py")),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.error_log,
            text=True,
            encoding="utf-8",
            env=self.env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.setup = self.rpc("setup")
        receipt = {
            "origin": self.origin,
            "run_id": self.setup["run_id"],
            "actor_id": self.setup["actor_id"],
            "catalog_kind": "merchant",
            "source": "managed_catalog_context + issue_lab_session; isolated Merchant worker",
        }
        (self.root / "catalog-provisioning-receipt.json").write_bytes(canonical_bytes(receipt))
        self.ref = ObjectRef(
            schema_version="commerce-policy/1.0",
            id="p07:provisioning",
            version=1,
            digest=sha256_digest(canonical_bytes(receipt)),
        )
        self.binding = CatalogBinding(
            assurance="backend_managed_catalog_registry/1",
            merchant_origin=self.origin,
            merchant_id=self.setup["merchant_id"],
            catalog_kind="merchant",
            feed_id="feed_merchant_catalog",
            source_ref=self.ref,
            scope=SessionScope(
                assurance="backend_session_registry/1",
                source_ref=self.ref,
                remote_run_id=self.setup["run_id"],
                remote_actor_id=self.setup["actor_id"],
            ),
        )

    def rpc(self, action, **data):
        assert self.worker and self.worker.stdin and self.worker.stdout
        self.worker.stdin.write(json.dumps({"action": action, **data}) + "\n")
        self.worker.stdin.flush()
        line = self.worker.stdout.readline()
        assert line, "Merchant worker stopped"
        result = json.loads(line)
        assert result["ok"], "Merchant worker rejected operation"
        return result["result"]

    def client(self, token_name="token"):
        return AuthorityObservationClient(
            AuthoritySession(self.binding, SecretStr(self.setup[token_name])),
            transport=PipeTransport(self),
        )

    def close(self):
        if self.worker:
            if self.worker.poll() is None:
                assert self.worker.stdin
                self.worker.stdin.write('{"action":"close"}\n')
                self.worker.stdin.flush()
                self.worker.wait(timeout=20)
            if self.worker.stdin:
                self.worker.stdin.close()
            if self.worker.stdout:
                self.worker.stdout.close()
        if self.error_log:
            self.error_log.close()
        if self.started:
            self.command(PG / "pg_ctl.exe", "-D", self.data, "-m", "fast", "-w", "stop")
            with socket.socket() as probe:
                probe.settimeout(0.2)
                assert probe.connect_ex(("127.0.0.1", self.port)) != 0
        (self.root / "closed.json").write_text(
            json.dumps(
                {
                    "postgres_port": self.port,
                    "stopped": True,
                    "worker_exit": self.worker.returncode if self.worker else None,
                }
            )
        )


@pytest.fixture(scope="module")
def live():
    fixture = MerchantFixture()
    try:
        fixture.start()
        yield fixture
    finally:
        fixture.close()


def test_real_merchant_catalog_session_and_snapshot(live):
    client = live.client()
    store = SqlitePreselectionStore(live.root / "aux.sqlite", SecretBytes(secrets.token_bytes(32)))
    store.initialize()
    try:
        flow = PreselectionAuthority(client, store)
        episode = flow.begin(
            owner_id="synthetic-buyer",
            intent=PurchaseIntent(),
            destination=DeliveryContext(country="CO", city="Bogota", postal_code="110111"),
        )
        result = flow.capture(episode, owner_id="synthetic-buyer")
        assert len(result.observations) == len(result.episode.feed.available_candidates)
        assert len(result.observations) > 1
        assert not result.commercial_continuation
        assert live.rpc("effects") == live.setup["effects"]
        # Public evidence only; neither token nor custody key is serialized.
        (live.root / "selection.json").write_bytes(canonical_bytes(result))
        print("P07_REAL_EVIDENCE", live.root, "observations", len(result.observations))
    finally:
        client.close()


def test_real_same_id_different_catalog_is_not_authority(live):
    client = live.client("other_token")
    try:
        with pytest.raises(ObservationBlocked, match="SCOPE_CONFLICT"):
            client.verify_scope()
        # Authenticated, valid Offer with the same ID really exists in that other run.
        offer, _ = client.observe(
            live.setup["offer"]["id"],
            DeliveryContext(country="CO", city="Bogota", postal_code="110111"),
        )
        assert offer.id == live.setup["offer"]["id"]
        assert offer.expires_at != live.setup["offer"]["expires_at"]
        assert live.rpc("effects") == live.setup["effects"]
    finally:
        client.close()


def test_real_merchant_digest_equivalence_and_noninjective_projection(live):
    pair = live.rpc("projection", offer=live.setup["offer"])
    offer = MerchantOffer.model_validate(live.setup["offer"])
    assert offer_snapshot_sha256(offer) == pair["first_digest"]
    assert pair["same_feed"] and pair["first_digest"] != pair["second_digest"]
    changed = offer.model_dump(mode="json")
    changed["revision"] += 1
    changed["pricing"]["shipping_total_minor"] += 1
    changed["pricing"]["total_minor"] += 1
    newer = MerchantOffer.model_validate(changed)
    assert projected_candidate(offer, live.origin) == projected_candidate(newer, live.origin)
    assert offer_snapshot_sha256(newer) == pair["second_digest"]


def test_real_revoked_session_conservative(live):
    live.rpc("revoke")
    client = live.client()
    try:
        with pytest.raises(ObservationBlocked, match="SESSION_REJECTED"):
            client.verify_scope()
        assert live.rpc("effects") == live.setup["effects"]
    finally:
        client.close()
