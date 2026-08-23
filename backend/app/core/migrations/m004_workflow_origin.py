"""Schema v4 - label every workflow's origin (REQUIREMENTS_R2 C8.4).

Forward-only and non-destructive: two nullable-in-practice columns added with
``ALTER TABLE`` (SQLite appends, it never rewrites the table), then a backfill
computed from the path the indexer already stored.  No row is deleted, no
existing column is touched, and a re-run is a no-op.

``origin`` deliberately carries no ``CHECK`` constraint.  SQLite cannot add one
to an existing table without a full table rebuild, and rebuilding a table that
holds the owner's ratings, notes and album membership to gain a constraint the
writer already enforces is a bad trade.  The vocabulary is enforced in
``parsers/workflow_origin.py`` and documented in DATA_MODEL 6.
"""

from __future__ import annotations

import sqlite3

from ...parsers import workflow_origin

VERSION = 4
NAME = "workflow_origin"


def up(conn: sqlite3.Connection) -> None:
    columns = {str(r["name"]) for r in conn.execute("PRAGMA table_info(workflows)")}

    if "origin" not in columns:
        conn.execute(
            "ALTER TABLE workflows ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'"
        )
    if "origin_package" not in columns:
        conn.execute("ALTER TABLE workflows ADD COLUMN origin_package TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_workflows_origin "
        "ON workflows(origin, name COLLATE NOCASE)"
    )

    updates: list[tuple] = []
    for row in conn.execute("SELECT id, rel_path, abs_path FROM workflows"):
        origin, package = workflow_origin.classify(
            str(row["rel_path"] or ""), str(row["abs_path"] or "")
        )
        updates.append((origin, package, int(row["id"])))
    if updates:
        conn.executemany(
            "UPDATE workflows SET origin = ?, origin_package = ? WHERE id = ?", updates
        )
