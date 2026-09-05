"""Schema v8 - support custom extra output scan roots.

Allows users to add arbitrary external folders containing ComfyUI output images/videos
on any drive. These folders are cataloged, watched, parsed, and linked to models.
"""

from __future__ import annotations

import sqlite3

VERSION = 8
NAME = "extra_outputs"


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE roots_new (
            id           INTEGER PRIMARY KEY,
            kind         TEXT NOT NULL CHECK (kind IN
                           ('comfyui','extra_models','extra_workflows','extra_outputs','data')),
            path         TEXT NOT NULL,
            path_key     TEXT NOT NULL UNIQUE,
            label        TEXT NOT NULL,
            category     TEXT,
            is_default   INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
            source       TEXT NOT NULL CHECK (source IN ('config','yaml','manual')),
            available    INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0,1)),
            last_seen_at INTEGER,
            created_at   INTEGER NOT NULL
        )
    """)
    conn.execute("INSERT INTO roots_new SELECT * FROM roots")
    conn.execute("DROP TABLE roots")
    conn.execute("ALTER TABLE roots_new RENAME TO roots")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_roots_kind ON roots(kind, available)")
