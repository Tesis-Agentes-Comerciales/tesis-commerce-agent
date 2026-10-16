"""Foreign-environment fixture process. Run ONLY with the frozen Merchant's Python.

No Agent process imports Merchant. Private authenticated IPC carries credentials
in memory; stdout is never used for tokens. No production source is patched.
"""

import hashlib
import importlib
import json
import os
import socket
import sys
import traceback
from contextlib import suppress
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any


def bootstrap():
    merchant = Path(os.environ["G06_MERCHANT"]).resolve()
    sys.path.insert(0, str(merchant / "src"))
    settings = importlib.import_module("commerce_lab.settings")
    settings.Settings.model_config["env_file"] = None
    settings.get_settings.cache_clear()
    pg: Any = importlib.import_module("psycopg")
    real_connect = pg.connect
    expected = pg.conninfo.conninfo_to_dict(os.environ["DATABASE_URL"])
    assert expected["host"] == "127.0.0.1"
    assert expected["dbname"] == "tesis_lab"
    assert expected["port"] not in {"55432", "55433", "62281"}
    assert expected["user"] == "gov06dc_app"

    def checked(conninfo="", **kwargs):
        actual = pg.conninfo.conninfo_to_dict(conninfo, **kwargs)
        for key in ("host", "port", "dbname", "user", "password"):
            if actual.get(key) != expected.get(key):
                raise RuntimeError("G06_DSN_REJECTED")
        connection = real_connect(conninfo, **kwargs)
        try:
            row = connection.execute(
                "SELECT current_database(),current_user,host(inet_server_addr()),"
                "inet_server_port(),current_setting('data_directory')"
            ).fetchone()
            assert row[:4] == ("tesis_lab", "gov06dc_app", "127.0.0.1", int(expected["port"]))
            assert Path(row[4]).resolve() == Path(os.environ["G06_PGDATA"]).resolve()
            connection.rollback()
        except BaseException:
            connection.close()
            raise
        return connection

    pg.connect = checked
    # Prohibit writes to either repository except this new disposable run directory.
    allowed = Path(os.environ["G06_ROOT"]).resolve()
    roots = (merchant, Path(os.environ["G06_AGENT"]).resolve())
    listeners = set()
    original_listen = socket.socket.listen

    def listen(self, *args, **kwargs):
        result = original_listen(self, *args, **kwargs)
        address = self.getsockname()
        if isinstance(address, tuple) and address[0] == "127.0.0.1":
            listeners.add(address[1])
        return result

    socket.socket.listen = listen
    original_bind = socket.socket.bind

    def bind(self, address):
        result = original_bind(self, address)
        if isinstance(address, tuple) and address == ("127.0.0.1", 0):
            selected = self.getsockname()[1]
            if selected not in {55432, 55433, 62281}:
                listeners.add(selected)
        return result

    socket.socket.bind = bind

    def guard(event, args):
        candidates = []
        if event == "open":
            name, mode, flags = args
            if (mode and any(x in mode for x in "wax+")) or (
                flags and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            ):
                candidates = [name]
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename"}:
            candidates = args[:2] if event == "os.rename" else args[:1]
        elif event == "socket.connect":
            address = args[1]
            permitted = {int(expected["port"]), int(os.environ["G06_CONTROL"])} | listeners
            if (
                not isinstance(address, tuple)
                or address[0] != "127.0.0.1"
                or address[1] not in permitted
            ):
                raise RuntimeError("G06_EXTERNAL_SOCKET_REJECTED")
        for value in candidates:
            if isinstance(value, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(value)).resolve()
                if any(path == root or root in path.parents for root in roots) and not (
                    path == allowed or allowed in path.parents
                ):
                    raise RuntimeError("G06_REPOSITORY_WRITE_REJECTED")

    sys.addaudithook(guard)
    return pg, importlib.import_module("commerce_lab.db"), expected


class FixtureControl:
    def __init__(self, pg, db):
        self.pg, self.db = pg, db
        self.context = importlib.import_module("commerce_lab.context")
        self.acp = importlib.import_module("commerce_lab.acp")
        self.checkout = importlib.import_module("commerce_lab.checkout")
        self.sql = importlib.import_module("psycopg.sql")
        self.pending: dict[str, Any] = {}

    def connection(self):
        return self.pg.connect(self.db.database_url())

    def command(self, command: dict[str, Any]):
        operation = command["operation"]
        if operation == "prepare":
            with self.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
                    ).fetchone()[0]
                    == 0
                )
            first = self.db.migrate()
            second = self.db.migrate()
            return {"first": first, "second": second}
        if operation == "episode":
            run = str(self.db.seed(actor_id=command["actor"], variant="B2")["run_id"])
            with self.connection() as connection:
                offer = connection.execute(
                    "SELECT snapshot FROM catalog_offers WHERE run_id=%s ORDER BY offer_id LIMIT 1",
                    (run,),
                ).fetchone()[0]
            return {"run_id": run, "actor_id": command["actor"], "offer": offer}
        if operation == "issue":
            run, actor = command["run_id"], command["actor_id"]
            token = self.context.issue_lab_session(run, actor)
            resolved = self.context.authenticate_lab_session(token)
            assert (resolved.run_id, resolved.actor_id) == (run, actor)
            # Credentials travel only through the authenticated private IPC connection.
            return {
                "bearer": token,
                "run_id": run,
                "actor_id": actor,
                "session_sha256": hashlib.sha256(token.encode()).hexdigest(),
            }
        if operation == "session_state":
            column = {"expired": "expires_at", "revoked": "revoked_at"}[command["state"]]
            with self.connection() as connection:
                connection.execute(
                    self.sql.SQL(
                        "UPDATE lab_sessions SET {}=now()-interval '1 second' "
                        "WHERE session_sha256=%s"
                    ).format(self.sql.Identifier(column)),
                    (command["session_sha256"],),
                )
            return {"state": command["state"]}
        if operation == "snapshot":
            result = {}
            with self.connection() as connection, connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                for (table,) in connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
                ).fetchall():
                    value, count = connection.execute(
                        self.sql.SQL(
                            "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                            "'[]'::jsonb),count(*) FROM {} t"
                        ).format(self.sql.Identifier(table))
                    ).fetchone()
                    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
                    result[table] = {
                        "count": count,
                        "sha256": hashlib.sha256(body.encode()).hexdigest(),
                    }
            return result
        if operation == "versions":
            metadata = importlib.import_module("importlib.metadata")
            source = self.acp.__file__
            assert source is not None
            return {
                "python": sys.version.split()[0],
                "merchant_source": str(Path(source).resolve()),
                **{
                    name: metadata.version(name)
                    for name in ("psycopg", "fastapi", "uvicorn", "httpx", "pydantic")
                },
            }
        if operation in {"internal_create", "pending_create"}:
            ctx = self.context.authenticate_lab_session(command["bearer"])
            adapter = self.acp.ACPCheckoutAdapter(ctx)
            connection = self.connection()
            try:
                offer = adapter._offer(command["variant"], connection=connection)
                result = self.checkout.CheckoutService(ctx).prepare_in_transaction(
                    {
                        "offer_id": offer.id,
                        "expected_offer_revision": offer.revision,
                        "quantity": 1,
                        "delivery_context": offer.delivery_context.model_dump(mode="json"),
                        "idempotency_key": command["key"],
                    },
                    connection=connection,
                )
                assert result.ok is True
                if operation == "pending_create":
                    original = adapter._response(result.data, offer)
                    fingerprint = self.acp.creation_fingerprint(command["body"])
                    key_hash = hashlib.sha256(command["key"].encode()).hexdigest()
                    adapter._save_creation(
                        connection, key_hash, fingerprint, result.data.id, original
                    )
                    self.pending[command["handle"]] = connection
                else:
                    connection.commit()
                    connection.close()
                return {"checkout_id": result.data.id}
            except BaseException:
                connection.rollback()
                connection.close()
                raise
        if operation == "commit_pending":
            connection = self.pending.pop(command["handle"])
            connection.commit()
            connection.close()
            return {"committed": True}
        if operation == "cancel_fixture":
            ctx = self.context.authenticate_lab_session(command["bearer"])
            result = self.checkout.CheckoutService(ctx).cancel(
                {
                    "checkout_id": command["checkout_id"],
                    "idempotency_key": command["key"],
                }
            )
            assert result.ok is True
            return {"status": result.data.status}
        if operation == "alter_offer_fixture":
            with self.connection() as connection:
                value = connection.execute(
                    "SELECT snapshot FROM catalog_offers WHERE run_id=%s AND offer_id=%s",
                    (command["run_id"], command["variant"]),
                ).fetchone()[0]
                value["revision"] += 1
                value["name"] = "Changed after original creation"
                value["availability"] = "out_of_stock"
                value["available_quantity"] = 0
                value["pricing"]["items_total_minor"] += 100
                value["pricing"]["total_minor"] += 100
                importlib.import_module("commerce_lab.contracts").Offer.model_validate(value)
                jsonb = importlib.import_module("psycopg.types.json").Jsonb
                count = connection.execute(
                    "UPDATE catalog_offers SET snapshot=%s,revision=%s "
                    "WHERE run_id=%s AND offer_id=%s",
                    (jsonb(value), value["revision"], command["run_id"], command["variant"]),
                ).rowcount
            assert count == 1
            return {"changed": count}
        raise ValueError("G06_UNKNOWN_CONTROL_COMMAND")


def main():
    pg, db, _ = bootstrap()
    if sys.argv[1] == "regressions":
        import io

        pytest = importlib.import_module("pytest")
        process_module: Any = importlib.import_module("subprocess")
        real_popen = process_module.Popen
        children = []

        class OwnedProcess(real_popen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                children.append(self)

        process_module.Popen = OwnedProcess
        root = Path(os.environ["G06_ROOT"])
        os.environ["RUN_DB_INTEGRATION"] = "1"
        os.environ["RECOVERY_TEST_CLUSTER"] = os.environ["G06_PGDATA"]
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        summary: dict[str, Any] = {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "warnings": 0,
            "warning_origins": [],
            "failures": [],
        }

        class Counts:
            def pytest_runtest_logreport(self, report):
                if report.failed:
                    summary["failed"] += 1
                    crash = getattr(report.longrepr, "reprcrash", None)
                    kind = str(getattr(crash, "message", "unknown")).split(":", 1)[0]
                    summary["failures"].append(
                        {
                            "nodeid": report.nodeid,
                            "when": report.when,
                            "error_type": kind if kind.isidentifier() else "withheld",
                        }
                    )
                elif report.skipped:
                    summary["skipped"] += 1
                elif report.when == "call" and report.passed:
                    summary["passed"] += 1

            def pytest_warning_recorded(self, warning_message, when, nodeid, location):
                summary["warnings"] += 1
                # Record category/location only; warning values may contain credentials.
                summary["warning_origins"].append(
                    {
                        "category": warning_message.category.__name__,
                        "file": Path(warning_message.filename).name,
                        "line": warning_message.lineno,
                        "when": when,
                        "nodeid": nodeid,
                    }
                )

        # Keep rewritten assertion values and foreign diagnostics in memory. The
        # disk evidence contains only counts and node IDs, never longrepr/credentials.
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        import time

        started = time.perf_counter()
        code = pytest.main(
            [
                "tests/test_acp_creation.py",
                "tests/integration/test_acp_creation_recovery.py",
                "tests/test_creation_lookup.py",
                "tests/integration/test_acp_creation_lookup.py",
                "-q",
                "--tb=short",
                "--assert=plain",
                "-p",
                "no:cacheprovider",
                "--basetemp=" + str(root / "merchant-regressions"),
            ],
            plugins=[Counts()],
        )
        summary["duration_s"] = time.perf_counter() - started
        summary["exit_code"] = code
        summary["owned_children"] = [child.pid for child in children]
        summary["children_requiring_cleanup"] = []
        for child in children:
            if child.poll() is None:
                summary["children_requiring_cleanup"].append(child.pid)
                child.terminate()
                child.wait(timeout=10)
        summary["all_owned_children_stopped"] = all(child.poll() is not None for child in children)
        (root / "merchant-validation.json").write_text(json.dumps(summary), encoding="utf-8")
        raise SystemExit(code)
    if sys.argv[1] == "server":
        uvicorn = importlib.import_module("uvicorn")
        api = importlib.import_module("commerce_lab.api")

        class DiagnosticApp:
            async def __call__(self, scope, receive, send):
                try:
                    await api.app(scope, receive, send)
                except BaseException as error:
                    rows = []
                    current = error
                    while current is not None:
                        rows.append(
                            {
                                "type": type(current).__name__,
                                "frames": [
                                    {
                                        "file": Path(f.filename).name,
                                        "line": f.lineno,
                                        "function": f.name,
                                    }
                                    for f in traceback.extract_tb(current.__traceback__)
                                ],
                            }
                        )
                        current = current.__cause__ or current.__context__
                    path = Path(os.environ["G06_ROOT"]) / "server-diagnostic.json"
                    path.write_text(json.dumps(rows), encoding="utf-8")
                    raise

        ticket = os.environ["G06_LAUNCH_TICKET"]
        (Path(os.environ["G06_ROOT"]) / f"server-boot-{ticket}.json").write_text(
            json.dumps({"pid": os.getpid(), "parent_pid": os.getppid()}), encoding="utf-8"
        )
        transfer = json.loads(sys.stdin.readline())
        shared = socket.fromshare(bytes.fromhex(transfer["socket_share"]))
        assert shared.getsockname() == ("127.0.0.1", int(os.environ["G06_HTTP"]))
        marker = {
            "pid": os.getpid(),
            "address": shared.getsockname(),
            "source": "targeted_windows_socket_share",
        }
        (Path(os.environ["G06_ROOT"]) / f"server-socket-{os.getpid()}.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )
        config = uvicorn.Config(
            DiagnosticApp(),
            host="127.0.0.1",
            port=int(os.environ["G06_HTTP"]),
            access_log=False,
            log_level="critical",
        )
        uvicorn.Server(config).run(sockets=[shared])
        return
    control = FixtureControl(pg, db)
    with Listener(
        ("127.0.0.1", int(os.environ["G06_CONTROL"])),
        authkey=bytes.fromhex(os.environ["G06_IPC_KEY"]),
    ) as listener:
        while True:
            with listener.accept() as channel:
                command = channel.recv()
                if command["operation"] == "shutdown":
                    for connection in control.pending.values():
                        connection.rollback()
                        connection.close()
                    channel.send({"ok": True, "data": {"stopped": True}})
                    return
                try:
                    channel.send({"ok": True, "data": control.command(command)})
                except Exception as error:
                    # Never reflect tokens, requests, DSNs, rows or exception text.
                    with suppress(Exception):
                        channel.send({"ok": False, "error_type": type(error).__name__})


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("G06_MERCHANT_WORKER_FAILED:" + type(error).__name__, file=sys.stderr)
        raise SystemExit(2) from None
