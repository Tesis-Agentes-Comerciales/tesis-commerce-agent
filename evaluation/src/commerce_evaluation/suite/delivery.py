"""Whether a case's injected text reached the agent's model (docs/design/eval-suite.md §4).

A `catalog_text` event tests its attack only when its text reaches a model request: the router,
the selection, the selector or the reviewer. The trial's `model_requests` ledger keeps each
request's input (`input_json`) and the catalog vocabulary of its instructions
(`catalog_vocabulary`), the one part of the instructions that comes from the feed; the rest is the
product's own text. An event's `Injection` counts the requests that carried its whole text, case
aside (`exposure.carries`), and names their stages and channels.

The executor writes the record's `injections`. For a record written before the field, the report
reads them from the trial's folder (`with_evidence`): when the trial was repeated, the folder
whose database names the record's run. The database is read from a copy with its WAL, so a report
never changes a run, not even one whose agent is still writing.

A trial's injection is delivered when the text of at least one of its events reached the model
under test: with the same text on two variants, the model needs to see only one. Without a model
under test (the reference agent of regression) there is nothing to deliver to.
"""

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple

from commerce_evaluation.suite.cases import Case, Event
from commerce_evaluation.suite.exposure import carries
from commerce_evaluation.suite.rundir import AGENT_DATABASE, trial_folders
from commerce_evaluation.suite.trial import REFERENCE, Carrier, Channel, Injection, TrialRecord

# A request whose scope named no stage.
UNNAMED = "sin etapa"
COLUMNS = ("stage", "input_json", "catalog_vocabulary", "run_id")


class Request(NamedTuple):
    stage: str | None
    model_input: str | None
    vocabulary: str | None


class Ledger(NamedTuple):
    """A trial's model requests, in order, and the runs its database names."""

    requests: tuple[Request, ...]
    runs: frozenset[str]


def injected(case: Case) -> list[Event]:
    return [event for event in case.environment.events if event.kind == "catalog_text"]


def observed(case: Case, database: Path) -> tuple[Injection, ...]:
    """Each `catalog_text` event of the case, with the trial's requests that carried its text."""
    events = injected(case)
    if not events:
        return ()
    ledger = read_ledger(database)
    requests = ledger.requests if ledger is not None else ()
    return tuple(_injection(event, requests) for event in events)


def delivered(case: Case, record: TrialRecord) -> bool | None:
    """Whether the trial's injected text reached the model under test; None when the case injects
    none or no model is under test."""
    if not injected(case) or record.agent.model == REFERENCE:
        return None
    return any(injection.requests for injection in record.injections or ())


def with_evidence(run_dir: Path, case: Case, record: TrialRecord) -> TrialRecord:
    """The record with its `injections`, read from its trial's folder when it was written
    without them; as it is when it has them or the folder is not found."""
    if record.injections is not None or not injected(case):
        return record
    folders = trial_folders(run_dir, record)
    if len(folders) > 1:
        folders = [folder for folder in folders if record.run_id in _runs(folder)]
    if len(folders) != 1:
        return record
    return record.model_copy(update={"injections": observed(case, folders[0] / AGENT_DATABASE)})


def read_ledger(database: Path) -> Ledger | None:
    """The ledger of a trial's database, read from a copy; None without the database."""
    if not database.is_file():
        return None
    with TemporaryDirectory() as directory:
        copy = Path(directory) / database.name
        # Without its -shm: SQLite rebuilds the index of the copied WAL.
        for suffix in ("", "-wal"):
            source = database.with_name(database.name + suffix)
            if source.is_file():
                shutil.copyfile(source, copy.with_name(copy.name + suffix))
        with closing(sqlite3.connect(copy)) as db:
            tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master")}
            rows = _requests(db) if "model_requests" in tables else []
            purchases = db.execute("SELECT run_id FROM purchases") if "purchases" in tables else ()
            runs = {run for *_, run in rows} | {run for (run,) in purchases}
    return Ledger(tuple(Request(*row[:3]) for row in rows), frozenset(run for run in runs if run))


def _requests(db: sqlite3.Connection) -> list[tuple[str | None, ...]]:
    # A ledger from before a column holds NULL in its place.
    present = {row[1] for row in db.execute("PRAGMA table_info(model_requests)")}
    picked = ", ".join(name if name in present else "NULL" for name in COLUMNS)
    return db.execute(f"SELECT {picked} FROM model_requests ORDER BY sequence").fetchall()


def _runs(folder: Path) -> frozenset[str]:
    ledger = read_ledger(folder / AGENT_DATABASE)
    return ledger.runs if ledger is not None else frozenset()


def _injection(event: Event, requests: tuple[Request, ...]) -> Injection:
    assert event.variant and event.field and event.text
    count, carriers = 0, dict[Carrier, None]()
    for request in requests:
        found: list[Channel] = []
        if carries(event.text, request.model_input):
            found.append("input")
        if carries(event.text, request.vocabulary):
            found.append("catalog_vocabulary")
        count += bool(found)
        stage = request.stage or UNNAMED
        carriers.update((Carrier(stage=stage, channel=channel), None) for channel in found)
    return Injection(
        variant=event.variant, field=event.field, requests=count, carriers=tuple(carriers)
    )
