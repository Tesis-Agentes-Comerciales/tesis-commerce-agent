"""Opt-in: own Agent and Merchant processes, real loopback HTTP, disposable PG."""

import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from tests.integration.gov06dc_harness import live_requirements, merchant_python, pg_tool, port
from tests.integration.test_preselection_merchant import MerchantFixture

ROOT = Path(__file__).resolve().parents[2]
pytestmark = live_requirements(
    "RUN_GOV07BB_REAL", "requires new isolated 07B-B real HTTP/PG experiment"
)


class LiveMerchant(MerchantFixture):
    def close(self):
        try:
            if self.worker is not None and self.worker.poll() is None:
                receipt = {
                    "effects": self.rpc("effects"),
                    "calls": self.rpc("calls"),
                    "merchant_pid": self.setup.get("worker_pid"),
                    "launcher_pid": self.worker.pid,
                }
                (self.root / "merchant-final.json").write_text(json.dumps(receipt, indent=2))
        finally:
            super().close()
        status = subprocess.run(
            [str(pg_tool("pg_ctl")), "-D", str(self.data), "status"],
            capture_output=True,
            env=self.env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert status.returncode == 3 and not (self.data / "postmaster.pid").exists()
        (self.root / "pg-stopped.json").write_text(
            json.dumps(
                {
                    "pg_ctl_status_exit": status.returncode,
                    "postmaster_pid_absent": True,
                    "postgres_port": self.port,
                }
            )
        )

    def start(self):
        self.http_port = port()
        assert self.http_port != self.port
        self.origin = f"http://127.0.0.1:{self.http_port}"
        self.command(
            pg_tool("initdb"),
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
        with (self.data / "postgresql.conf").open("a") as output:
            output.write("\ntimezone='UTC'\nlog_timezone='UTC'\n")
        self.started = True
        self.command(
            pg_tool("pg_ctl"),
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
                pg_tool("psql"),
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
        self.env.update(
            DATABASE_URL=f"postgresql://p07_app:{secrets.token_hex(20)}@127.0.0.1:{self.port}/tesis_lab",
            ACP_API_BASE_URL=self.origin,
            P07_MERCHANT=str(self.merchant),
            P07_PORT=str(self.port),
            P07_DATA=str(self.data),
            P07_OUTPUT=str(self.root),
            B_HTTP=str(self.http_port),
        )
        self.error_log = (self.root / "merchant-stderr.txt").open("w")
        self.worker = subprocess.Popen(
            [
                str(merchant_python()),
                "-B",
                str(Path(__file__).with_name("gov07bb_merchant_worker.py")),
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
        assert self.worker.pid != os.getpid()


class AgentProcess:
    def __init__(self, live, data, fresh=True):
        self.live, self.data = live, data
        env = dict(live.env)
        for key in tuple(env):
            if key.startswith(("P07_", "B_HTTP")) or key in {"DATABASE_URL", "ACP_API_BASE_URL"}:
                env.pop(key)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT)
        self.log = (live.root / ("agent-" + secrets.token_hex(4) + ".log")).open("w")
        self.worker = subprocess.Popen(
            [
                # This Agent checkout's interpreter, as the 06D-C restart probe uses.
                sys.executable,
                "-B",
                str(Path(__file__).with_name("gov07bb_agent_worker.py")),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=live.root,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            setup = self.rpc("setup" if fresh else "restore", data=data)
            self.pid = setup["pid"]
            assert self.pid not in {
                os.getpid(),
                live.setup["worker_pid"],
            }
        except BaseException:
            self.close()
            raise

    def rpc(self, action, *, required=True, **data):
        assert self.worker.stdin and self.worker.stdout
        self.worker.stdin.write(
            json.dumps(
                {"action": action, **data},
                default=lambda value: (
                    value.get_secret_value() if isinstance(value, SecretStr) else None
                ),
            )
            + "\n"
        )
        self.worker.stdin.flush()
        line = self.worker.stdout.readline()
        assert line, "Agent experiment process stopped"
        value = json.loads(line)
        if required:
            assert value["ok"], "Agent experiment rejected action: " + value.get(
                "error_type", "unknown"
            )
        return value["result"] if value["ok"] else value

    def close(self):
        if self.worker.poll() is None:
            assert self.worker.stdin
            self.worker.stdin.write('{"action":"close"}\n')
            self.worker.stdin.flush()
            self.worker.wait(timeout=20)
        if self.worker.stdin:
            self.worker.stdin.close()
        if self.worker.stdout:
            self.worker.stdout.close()
        self.log.close()


@pytest.fixture(scope="module")
def live():
    value = LiveMerchant()
    try:
        value.start()
        yield value
    finally:
        value.close()
        with socket.socket() as probe:
            probe.settimeout(0.2)
            assert probe.connect_ex(("127.0.0.1", value.http_port)) != 0


@pytest.mark.parametrize(
    "scenario", ["received", "lost", "revision", "digest", "shipping", "expiry"]
)
def test_real_new_selection_applied_d1_one_guarded_post_and_readonly_recovery(live, scenario):
    before = live.rpc("effects")
    calls_before = len(live.rpc("calls"))
    directory = live.root / ("agent-" + scenario)
    data = {
        "directory": str(directory),
        "ephemeral_secret": SecretStr(secrets.token_hex(32)),
        "origin": live.origin,
        "run_id": live.setup["run_id"],
        "actor_id": live.setup["actor_id"],
        "merchant_id": live.setup["merchant_id"],
        "token": SecretStr(live.setup["token"]),
    }
    agent = AgentProcess(live, data)
    guard = None
    try:
        assert live.rpc("effects") == before
        guard = agent.rpc("prepare")
        assert guard["shadow_binding"]["governance_mode"] == "shadow"
        assert guard["shadow_binding"]["operation_id"] is None
        assert not [
            c
            for c in live.rpc("calls")[calls_before:]
            if c["path"] == "/lab/governance/checkout-creation/guarded"
        ]
        if scenario in {"revision", "digest", "shipping", "expiry"}:
            live.rpc("edit", offer_id=guard["request"]["offer_id"], change=scenario)
        if scenario == "lost":
            live.rpc("delay", enabled=True)
            agent.rpc("timeout", seconds=0.2)
        initial = agent.rpc("dispatch")
        assert initial["result"] == ("found" if scenario == "received" else "error")
        if scenario == "received":
            live.rpc("edit", offer_id=guard["request"]["offer_id"], change="revision")
        old_pid = agent.pid
        agent.close()
        agent = AgentProcess(live, data, fresh=False)
        assert agent.pid != old_pid
        expected = "found" if scenario in {"received", "lost"} else "rejected"
        first = agent.rpc("recover")
        assert (
            first["result"] == expected
            and not first["replay_allowed"]
            and not first["continuation_allowed"]
        )
        assert agent.rpc("recover") == first
        assert not agent.rpc("dispatch", required=False)["ok"]
        history = agent.rpc("history")
        assert len(history) == 3 and history[-1]["kind"] == "lookup"
        calls = live.rpc("calls")[calls_before:]
        commercial = [c for c in calls if c["path"] == "/lab/governance/checkout-creation/guarded"]
        lookup = [c for c in calls if c["path"] == "/lab/recovery/guarded-checkout-creation/lookup"]
        assert len(commercial) == 1 and len(lookup) == 2
        after = live.rpc("effects")
        created = 1 if expected == "found" else 0
        assert after["checkout_sessions"] - before["checkout_sessions"] == created
        assert after["guarded_checkout_creations"] - before["guarded_checkout_creations"] == created
        assert (
            after["rejected_guarded_checkout_attempts"]
            - before["rejected_guarded_checkout_attempts"]
            == 1 - created
        )
        for name in ("orders", "checkout_mutations", "checkout_completion_attempts"):
            assert after[name] == before[name]
        evidence = dict(
            scenario=scenario,
            agent_pid=agent.pid,
            merchant_pid=live.setup["worker_pid"],
            guard=guard,
            initial=initial,
            recovered=first,
            calls=calls,
            before=before,
            after=after,
            post_commercial=1,
            readonly_lookups=2,
        )
        (directory / "public-result.json").write_text(json.dumps(evidence, indent=2))
        print("07BB_REAL", scenario, "POST=1 LOOKUP=2", directory)
    finally:
        agent.close()
        live.rpc("delay", enabled=False)
        if guard and scenario in {"received", "revision", "digest", "shipping", "expiry"}:
            live.rpc(
                "edit",
                offer_id=guard["request"]["offer_id"],
                change="restore",
                snapshot=guard["selected_offer"],
            )
