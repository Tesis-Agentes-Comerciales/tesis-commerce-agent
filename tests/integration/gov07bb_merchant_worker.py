"""Foreign Merchant process: real loopback HTTP, isolated PG and private setup RPC."""

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def main():
    root = Path(os.environ["P07_MERCHANT"])
    sys.path.insert(0, str(root / "src"))
    settings = importlib.import_module("commerce_lab.settings")
    settings.Settings.model_config["env_file"] = None
    settings.get_settings.cache_clear()
    pg: Any = importlib.import_module("psycopg")
    real_connect = pg.connect
    dsn = os.environ["DATABASE_URL"]

    def connect(conninfo="", **kwargs):
        if conninfo != dsn:
            raise RuntimeError("Foreign DSN rejected")
        db = real_connect(conninfo, **kwargs)
        row = db.execute(
            "SELECT current_database(),current_user,inet_server_port(),"
            "current_setting('data_directory')"
        ).fetchone()
        if (
            row[:3] != ("tesis_lab", "p07_app", int(os.environ["P07_PORT"]))
            or Path(row[3]).resolve() != Path(os.environ["P07_DATA"]).resolve()
        ):
            db.close()
            raise RuntimeError("Foreign cluster rejected")
        db.rollback()
        return db

    pg.connect = connect
    allowed = Path(os.environ["P07_OUTPUT"]).resolve()

    def audit(event, args):
        if event == "socket.connect":
            address = args[1]
            import socket

            frame = sys._getframe(1)
            if (
                frame.f_code.co_filename == socket.__file__
                and frame.f_code.co_name == "_fallback_socketpair"
            ):
                listener = frame.f_locals.get("lsock")
                if (
                    listener
                    and frame.f_locals.get("csock") is args[0]
                    and address == listener.getsockname()
                ):
                    return
            if address != ("127.0.0.1", int(os.environ["P07_PORT"])):
                raise RuntimeError("Foreign socket rejected")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.name == ".env":
                raise RuntimeError("Environment file rejected")
            flags = args[2] or 0
            if (
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                and not path.is_relative_to(allowed)
                and path.name.lower() != "nul"
            ):
                raise RuntimeError("Foreign write rejected")

    sys.addaudithook(audit)
    db_module = importlib.import_module("commerce_lab.db")
    db_module.migrate()
    catalog = importlib.import_module("commerce_lab.managed_catalog")
    context = importlib.import_module("commerce_lab.context")
    contracts = importlib.import_module("commerce_lab.contracts")
    guarded = importlib.import_module("commerce_lab.contracts.guarded_creation")
    feed_module = importlib.import_module("commerce_lab.feed")
    api = importlib.import_module("commerce_lab.api")
    catalog.import_current_catalog()
    managed_run, actor = catalog.managed_catalog_context()
    token = context.issue_lab_session(str(managed_run), actor)
    # Fixture episode intentionally shares Offer IDs with the published catalog.
    other = str(db_module.seed(actor_id="p07-other", variant="B2")["run_id"])
    other_token = context.issue_lab_session(other, "p07-other")
    import asyncio
    import threading
    import time

    uvicorn = importlib.import_module("uvicorn")
    calls = []
    modes = {"delay": False}

    @api.app.middleware("http")
    async def trace(request, call_next):
        permitted = {
            ("GET", "/feeds/current"),
            ("POST", "/lab/offers/get"),
            ("POST", "/lab/recovery/checkout-creation/lookup"),
            ("POST", "/lab/governance/checkout-creation/guarded"),
            ("POST", "/lab/recovery/guarded-checkout-creation/lookup"),
        }
        if (request.method, request.url.path) not in permitted:
            from fastapi.responses import JSONResponse

            return JSONResponse({"blocked": True}, status_code=405)
        response = await call_next(request)
        calls.append(
            {"method": request.method, "path": request.url.path, "status": response.status_code}
        )
        if request.url.path == "/lab/governance/checkout-creation/guarded" and modes["delay"]:
            await asyncio.sleep(1.5)
        return response

    server = uvicorn.Server(
        uvicorn.Config(
            api.app,
            host="127.0.0.1",
            port=int(os.environ["B_HTTP"]),
            log_level="critical",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 25
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("Merchant HTTP did not start")
        time.sleep(0.05)

    def effects():
        with pg.connect(dsn) as db:
            return {
                name: db.execute("SELECT count(*) FROM " + name).fetchone()[0]
                for name in (
                    "checkout_sessions",
                    "checkout_mutations",
                    "orders",
                    "checkout_completion_attempts",
                    "guarded_checkout_creations",
                    "rejected_guarded_checkout_attempts",
                )
            }

    for line in sys.stdin:
        request = json.loads(line)
        try:
            action = request["action"]
            if action == "setup":
                _, products = feed_module.current_feed(base_url=os.environ["ACP_API_BASE_URL"])
                with pg.connect(dsn) as db:
                    raw = db.execute(
                        "SELECT snapshot FROM catalog_offers WHERE run_id=%s "
                        "ORDER BY offer_id LIMIT 1",
                        (managed_run,),
                    ).fetchone()[0]
                result = {
                    "worker_pid": os.getpid(),
                    "run_id": str(managed_run),
                    "actor_id": actor,
                    "token": token,
                    "other_token": other_token,
                    "merchant_id": raw["merchant_id"],
                    "offer": raw,
                    "products": len(products),
                    "effects": effects(),
                }
            elif action == "calls":
                result = calls.copy()
            elif action == "delay":
                modes["delay"] = request["enabled"]
                result = True
            elif action == "edit":
                Jsonb = importlib.import_module("psycopg.types.json").Jsonb

                with pg.connect(dsn) as db:
                    row = db.execute(
                        "SELECT snapshot FROM catalog_offers "
                        "WHERE run_id=%s AND offer_id=%s FOR UPDATE",
                        (managed_run, request["offer_id"]),
                    ).fetchone()
                    value = request.get("snapshot", row[0])
                    change = request["change"]
                    if change == "revision":
                        value["revision"] += 1
                    if change == "digest":
                        value["name"] += " changed"
                    if change == "shipping":
                        value["pricing"]["shipping_total_minor"] += 1
                        value["pricing"]["total_minor"] += 1
                    if change == "expiry":
                        from datetime import datetime, timedelta

                        value["expires_at"] = (
                            datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
                            + timedelta(seconds=1)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    db.execute(
                        "UPDATE catalog_offers SET snapshot=%s,revision=%s "
                        "WHERE run_id=%s AND offer_id=%s",
                        (Jsonb(value), value["revision"], managed_run, request["offer_id"]),
                    )
                result = True
            elif action == "projection":
                first = contracts.Offer.model_validate(request["offer"])
                changed = first.model_dump(mode="json")
                changed["revision"] += 1
                changed["pricing"]["shipping_total_minor"] += 1
                changed["pricing"]["total_minor"] += 1
                second = contracts.Offer.model_validate(changed)
                result = {
                    "same_feed": feed_module.build_feed([first])
                    == feed_module.build_feed([second]),
                    "first_digest": guarded.offer_snapshot_sha256(first),
                    "second_digest": guarded.offer_snapshot_sha256(second),
                }
            elif action == "revoke":
                with pg.connect(dsn) as db:
                    db.execute(
                        "UPDATE lab_sessions SET revoked_at=now() WHERE run_id=%s", (managed_run,)
                    )
                result = True
            elif action == "effects":
                result = effects()
            elif action == "close":
                break
            else:
                raise RuntimeError("RPC rejected")
            print(json.dumps({"ok": True, "result": result}), flush=True)
        except Exception as error:
            # Deliberately no error text, traceback, request or secret values.
            print(json.dumps({"ok": False, "error_type": type(error).__name__}), flush=True)
    server.should_exit = True
    thread.join(timeout=20)
    assert not thread.is_alive()


if __name__ == "__main__":
    main()
