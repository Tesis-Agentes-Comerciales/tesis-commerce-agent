"""Ports of the delegation ledger (spec §4.5).

A `TransactionHook` runs on the ledger's own connection, inside its `BEGIN IMMEDIATE`, after the
ledger's writes and before `COMMIT`; if it raises, the whole unit of work rolls back. HNP-06 writes
the D2/D3 evidence through it, and HNP-05/09b the purchase transitions.
"""

import sqlite3
from collections.abc import Callable

TransactionHook = Callable[[sqlite3.Connection], None]
