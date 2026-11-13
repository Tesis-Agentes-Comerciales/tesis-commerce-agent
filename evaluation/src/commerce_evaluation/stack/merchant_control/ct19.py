"""CT19: catalog identity evidence and its reversible retire and restore fixture."""

import hashlib
import json
from typing import Any
from uuid import uuid4

from commerce_evaluation.stack.merchant_control.core import BaseControl


class Ct19Control(BaseControl):
    def ct19_state(self, command: dict[str, Any]) -> dict[str, Any]:
        """Public catalog rows and constraint evidence, scoped to an owned disposable run."""
        with self.connection() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            assert (
                connection.execute(
                    "SELECT count(*) FROM experiment_runs WHERE run_id=%s AND actor_id=%s",
                    (command["run_id"], command["actor_id"]),
                ).fetchone()[0]
                == 1
            )
            rows = connection.execute(
                "SELECT to_jsonb(t) FROM catalog_offers t WHERE run_id=%s ORDER BY offer_id",
                (command["run_id"],),
            ).fetchall()
            target = next((r[0] for r in rows if r[0]["offer_id"] == command["offer_id"]), None)
            constraints = connection.execute(
                "SELECT conname,convalidated,pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid IN ('catalog_offers'::regclass,'checkout_sessions'::regclass) "
                "ORDER BY conname"
            ).fetchall()
            triggers = connection.execute(
                "SELECT tgrelid::regclass::text,tgname,tgenabled FROM pg_trigger "
                "WHERE tgrelid IN ('catalog_offers'::regclass,'checkout_sessions'::regclass) "
                "ORDER BY 1,2"
            ).fetchall()
            references = connection.execute(
                "SELECT count(*) FROM checkout_sessions WHERE run_id=%s AND offer_id=%s",
                (command["run_id"], command["offer_id"]),
            ).fetchone()[0]
            events = connection.execute(
                "SELECT event_type,payload FROM run_events WHERE run_id=%s "
                "AND event_type='offer.read' ORDER BY event_id",
                (command["run_id"],),
            ).fetchall()

        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

        return {
            "row": target,
            "catalog": [r[0] for r in rows],
            "catalog_sha256": digest(rows),
            "others_sha256": digest([r for r in rows if r[0]["offer_id"] != command["offer_id"]]),
            "references": references,
            "constraints": constraints,
            "triggers": triggers,
            "offer_read_events": events,
        }

    def ct19_capture(self, command: dict[str, Any]) -> dict[str, Any]:
        state = self.ct19_state(command)
        key = (command["run_id"], command["offer_id"])
        assert key not in self.ct19_originals and state["row"] is not None
        assert state["references"] == 0
        self.ct19_originals[key] = state
        return state

    def ct19_audit(self, _: dict[str, Any]) -> dict[str, Any]:
        """Hash full audit rows but disclose only fields needed to correlate synthetic requests."""
        with self.connection() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            requests = connection.execute(
                "SELECT request_id, to_jsonb(t), jsonb_build_object("
                "'request_id',request_id,'run_id',run_id,'actor_id',actor_id,"
                "'operation',operation,'result',result,'http_status',http_status,"
                "'checkout_id',checkout_id,'order_id',order_id,"
                "'idempotency_sha256',idempotency_sha256,'error_code',error_code) "
                "FROM request_observations t ORDER BY request_id"
            ).fetchall()
            events = connection.execute(
                "SELECT event_id::text, to_jsonb(t), jsonb_build_object("
                "'run_id',run_id,'event_type',event_type,'actor_id',payload->>'actor_id',"
                "'offer_id',payload->>'offer_id','request_id',payload->>'request_id',"
                "'result',payload->>'result') FROM run_events t ORDER BY event_id"
            ).fetchall()
        return {
            table: {
                key: {
                    "sha256": hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
                    "fields": fields,
                }
                for key, row, fields in rows
            }
            for table, rows in (("request_observations", requests), ("run_events", events))
        }

    def ct19_constraints(self, command: dict[str, Any]) -> dict[str, str]:
        """Rollback-only invalid-row probes; these are not commercial observations."""
        state = self.ct19_state(command)
        assert state["row"] is None
        key = (command["run_id"], command["offer_id"])
        result = {}
        for kind in ("identity", "revision", "positive_revision", "run_fk"):
            row = json.loads(json.dumps(self.ct19_originals[key]["row"]))
            if kind == "identity":
                row["snapshot"]["id"] += "-invalid-probe"
            elif kind == "revision":
                row["revision"] += 1
            elif kind == "positive_revision":
                row["revision"] = row["snapshot"]["revision"] = 0
            else:
                row["run_id"] = str(uuid4())
            with self.connection() as connection:
                try:
                    with connection.transaction():
                        connection.execute(
                            "INSERT INTO catalog_offers SELECT "
                            "(jsonb_populate_record(NULL::catalog_offers,%s)).*",
                            (self.jsonb(row),),
                        )
                        raise RuntimeError("CT19_CONSTRAINT_ACCEPTED_INVALID_ROW")
                except self.pg.IntegrityError as error:
                    result[kind] = error.sqlstate
        assert self.ct19_state(command) == state
        return result

    def ct19_mutate(self, command: dict[str, Any]) -> dict[str, Any]:
        """Reversible fixture only; never disable FK, triggers or CHECK constraints."""
        key = (command["run_id"], command["offer_id"])
        original = self.ct19_originals[key]
        assert self.ct19_state(command)["references"] == 0
        with self.connection() as connection:
            connection.execute(
                "LOCK TABLE catalog_offers,checkout_sessions IN SHARE ROW EXCLUSIVE MODE"
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM checkout_sessions WHERE run_id=%s AND offer_id=%s", key
                ).fetchone()[0]
                == 0
            )
            if command["change"] == "retire":
                assert self.ct19_state(command)["row"] == original["row"]
                assert (
                    connection.execute(
                        "DELETE FROM catalog_offers WHERE run_id=%s AND offer_id=%s", key
                    ).rowcount
                    == 1
                )
            elif command["change"] == "restore":
                connection.execute(
                    "DELETE FROM catalog_offers WHERE run_id=%s AND offer_id=%s", key
                )
                connection.execute(
                    "INSERT INTO catalog_offers SELECT "
                    "(jsonb_populate_record(NULL::catalog_offers,%s)).*",
                    (self.jsonb(original["row"]),),
                )
            else:
                raise ValueError("Unknown CT19 mutation")
        return self.ct19_state(command)
