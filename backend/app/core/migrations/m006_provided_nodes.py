"""Schema v6 - subgraph counts, and who registers a node class.

Two forward-only ``ADD COLUMN`` statements, both with a default, so nothing
existing is rewritten and no row the owner cares about is touched.

``workflows.subgraph_count`` records how many subgraph *definitions* a workflow
declares.  Nodes that instantiate one carry the definition's UUID as their
``type``; those UUIDs used to be indexed as node classes and then reported as
missing packages, which is the defect this migration's parser change fixes.

``node_classes.registration`` says where a class comes from at runtime:
``python`` (a class in the package's own source), ``javascript`` (the package's
shipped ``web/**/*.js``) or ``frontend`` (the ComfyUI web client itself).  The
last two have no Python definition anywhere by construction, so the UI can show
honestly that they are provided rather than pretending they were parsed out of
Python - and the dependency ladder can stop calling them missing.
"""

from __future__ import annotations

import sqlite3

VERSION = 6
NAME = "provided_nodes"

STATEMENTS = (
    "ALTER TABLE workflows ADD COLUMN subgraph_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE node_classes ADD COLUMN registration TEXT NOT NULL DEFAULT 'python'",
    "CREATE INDEX IF NOT EXISTS ix_node_classes_registration "
    "ON node_classes(registration) WHERE registration <> 'python'",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def up(conn: sqlite3.Connection) -> None:
    # Idempotent: ``ADD COLUMN`` is not guarded by ``IF NOT EXISTS`` in SQLite,
    # so the column list is checked first.  The runner has already opened a
    # transaction, so every statement goes through ``execute``.
    existing = {
        "workflows": _columns(conn, "workflows"),
        "node_classes": _columns(conn, "node_classes"),
    }
    for statement in STATEMENTS:
        if statement.startswith("ALTER TABLE workflows") \
                and "subgraph_count" in existing["workflows"]:
            continue
        if statement.startswith("ALTER TABLE node_classes") \
                and "registration" in existing["node_classes"]:
            continue
        conn.execute(statement)
