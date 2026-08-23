"""Hand-rolled, transactional migration runner keyed on ``PRAGMA user_version``."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time

from ...config import SCHEMA_VERSION
from .. import db as dbmod
from . import (
    m001_initial,
    m002_import_legacy,
    m003_album_identity,
    m004_workflow_origin,
    m005_enable_jobs,
    m006_provided_nodes,
)

log = logging.getLogger(__name__)

MIGRATIONS = (m001_initial, m002_import_legacy, m003_album_identity,
              m004_workflow_origin, m005_enable_jobs, m006_provided_nodes)


def current_version(conn: sqlite3.Connection) -> int:
    cur = conn.execute("PRAGMA user_version")
    v = int(cur.fetchone()[0])
    cur.close()
    return v


def _apply(conn: sqlite3.Connection) -> list[str]:
    applied: list[str] = []
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is newer than this build supports "
            f"({SCHEMA_VERSION}). Refusing to open it."
        )
    for mod in MIGRATIONS:
        if version >= mod.VERSION:
            continue
        t0 = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        try:
            mod.up(conn)
            elapsed = int((time.perf_counter() - t0) * 1000)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations(version,name,applied_at,duration_ms) "
                "VALUES (?,?,?,?)",
                (mod.VERSION, mod.NAME, int(time.time() * 1000), elapsed),
            )
            conn.execute(f"PRAGMA user_version = {int(mod.VERSION)}")
            conn.commit()
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            raise
        applied.append(mod.NAME)
        log.info("applied migration %03d %s", mod.VERSION, mod.NAME)
    return applied


def migrate() -> list[str]:
    """Run every pending migration on the writer thread.  Returns applied names."""
    return dbmod.writer().run(_apply)
