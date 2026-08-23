"""One-time import of the legacy ``asset_vault.db`` config table (DATA_MODEL 14)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path

from ...config import LEGACY_DB_PATH

VERSION = 2
NAME = "import_legacy"

_LEGACY_ASSET_TABLES = ("models", "nodes", "workflows", "output_assets")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    )
    found = cur.fetchone() is not None
    cur.close()
    return found


def up(conn: sqlite3.Connection) -> None:
    legacy = Path(LEGACY_DB_PATH)
    if not legacy.exists():
        return
    try:
        old = sqlite3.connect(f"file:{str(legacy).replace(chr(92), '/')}?mode=ro", uri=True,
                              timeout=5.0)
    except sqlite3.Error:
        return
    old.row_factory = sqlite3.Row
    now = int(time.time() * 1000)
    try:
        if _table_exists(old, "config"):
            try:
                cur = old.execute("SELECT * FROM config")
                for row in cur.fetchall():
                    keys = row.keys()
                    key = row["key"] if "key" in keys else None
                    if not key:
                        continue
                    value = row["value"] if "value" in keys else None
                    vtype = row["value_type"] if "value_type" in keys else "str"
                    if vtype not in ("str", "int", "float", "bool", "json"):
                        vtype = "str"
                    conn.execute(
                        "INSERT OR REPLACE INTO config(key,value,value_type,updated_at) "
                        "VALUES (?,?,?,?)",
                        (str(key), None if value is None else str(value), vtype, now),
                    )
                cur.close()
            except sqlite3.DatabaseError:
                pass

        counts = {}
        for t in _LEGACY_ASSET_TABLES:
            if not _table_exists(old, t):
                counts[t] = 0
                continue
            try:
                cur = old.execute(f"SELECT COUNT(*) FROM {t}")  # noqa: S608 - fixed names
                counts[t] = int(cur.fetchone()[0])
                cur.close()
            except sqlite3.DatabaseError:
                counts[t] = 0
        conn.execute(
            "INSERT OR REPLACE INTO config(key,value,value_type,updated_at) VALUES (?,?,?,?)",
            ("legacy_import", json.dumps({"counts": counts, "at": now}), "json", now),
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            old.close()

    # Never delete the legacy file - rename it aside.
    backup = legacy.with_name(legacy.name + ".v1.bak")
    if not backup.exists():
        with contextlib.suppress(OSError):
            legacy.replace(backup)
