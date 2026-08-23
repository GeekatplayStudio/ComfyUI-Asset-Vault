"""Schema v5 - the workflow "Enable" fetch queue (REQUIREMENTS_R2 C9).

One table, created only if it is absent.  Nothing existing is altered, so the
migration is forward-only, idempotent and cannot touch a single row the owner
cares about.

The table is deliberately shaped like ``hash_jobs`` (DATA_MODEL 10): a real,
durable queue so a multi-gigabyte download survives an app restart and resumes
where it stopped, rather than an in-memory list that a crash silently discards.
``target_abs_path`` is written by the server from the derived destination and is
never accepted from a client (SECURITY_REVIEW R3).
"""

from __future__ import annotations

import sqlite3

VERSION = 5
NAME = "enable_jobs"

DDL = """
CREATE TABLE IF NOT EXISTS enable_jobs (
    id              INTEGER PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    workflow_id     INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
    item_key        TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('model','node_package')),
    ref_name        TEXT NOT NULL,
    category        TEXT,
    provider        TEXT,
    source_url      TEXT NOT NULL,
    source_host     TEXT NOT NULL,
    expected_size   INTEGER NOT NULL DEFAULT 0,
    expected_sha256 TEXT,
    root_id         INTEGER,
    target_abs_path TEXT NOT NULL,
    part_abs_path   TEXT,
    state           TEXT NOT NULL DEFAULT 'queued'
                    CHECK (state IN ('queued','running','done','failed','cancelled',
                                     'quarantined','skipped')),
    bytes_done      INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error_code      TEXT,
    error_message   TEXT,
    result_json     TEXT,
    enqueued_at     INTEGER NOT NULL,
    started_at      INTEGER,
    finished_at     INTEGER,
    UNIQUE (batch_id, item_key)
);
CREATE INDEX IF NOT EXISTS ix_enable_jobs_pick  ON enable_jobs(state, enqueued_at);
CREATE INDEX IF NOT EXISTS ix_enable_jobs_batch ON enable_jobs(batch_id, state);
CREATE INDEX IF NOT EXISTS ix_enable_jobs_wf    ON enable_jobs(workflow_id, state);
"""


def up(conn: sqlite3.Connection) -> None:
    # Statement by statement, never ``executescript``: the runner has already
    # opened a transaction and ``executescript`` would commit it out from under
    # the migration.
    for statement in DDL.split(";"):
        text = statement.strip()
        if text:
            conn.execute(text)
