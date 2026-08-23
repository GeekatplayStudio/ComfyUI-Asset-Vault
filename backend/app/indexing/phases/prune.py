"""Phase 9 - soft delete for vanished files, hard delete after retention.

A disconnected drive or a ComfyUI mid-write must never wipe the vault, so a
missing file is only flagged (``missing_since``) and hidden from list endpoints.
Rows on an unavailable root are left completely untouched.
"""

from __future__ import annotations

import os
import sqlite3
import time

from ...core import db as dbmod
from ...core.pathsafe import long_path

RETENTION_MS = 30 * 24 * 60 * 60 * 1000
CHECK_TABLES = (
    ("model_files", "model_files"),
    ("workflows", "workflows"),
    ("outputs", "outputs"),
    ("node_packages", "node_packages"),
)


def _exists(path: str) -> bool:
    try:
        return os.path.exists(long_path(path))
    except (OSError, ValueError):
        return True  # a path we cannot test is assumed present


def run(ctx) -> dict:
    t0 = time.perf_counter()
    conn = dbmod.get_ro()

    available_roots = set()
    for r in dbmod.rows(conn, "SELECT id FROM roots WHERE available = 1"):
        available_roots.add(int(r["id"]))

    gone: dict[str, list[int]] = {}
    back: dict[str, list[int]] = {}
    for table, _ in CHECK_TABLES:
        has_root = table != "node_packages"
        sql = f"SELECT id, abs_path, missing_since{', root_id' if has_root else ''} FROM {table}"  # noqa: S608
        for row in dbmod.rows(conn, sql):
            if has_root and row["root_id"] is not None \
                    and int(row["root_id"]) not in available_roots:
                continue
            exists = _exists(str(row["abs_path"]))
            if not exists and row["missing_since"] is None:
                gone.setdefault(table, []).append(int(row["id"]))
            elif exists and row["missing_since"] is not None:
                back.setdefault(table, []).append(int(row["id"]))

    now = dbmod.now_ms()
    cutoff = now - RETENTION_MS

    def _op(conn: sqlite3.Connection) -> dict:
        conn.execute("BEGIN IMMEDIATE")
        stats: dict[str, int] = {"missing": 0, "restored": 0, "hard_deleted": 0}
        for table, ids in gone.items():
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                ph = ",".join("?" * len(chunk))
                conn.execute(
                    f"UPDATE {table} SET missing_since = ? WHERE id IN ({ph})",  # noqa: S608
                    (now, *chunk),
                )
                stats["missing"] += len(chunk)
        for table, ids in back.items():
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                ph = ",".join("?" * len(chunk))
                conn.execute(
                    f"UPDATE {table} SET missing_since = NULL WHERE id IN ({ph})",  # noqa: S608
                    chunk,
                )
                stats["restored"] += len(chunk)
        for table, _ in CHECK_TABLES:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE missing_since IS NOT NULL AND missing_since < ?",  # noqa: S608
                (cutoff,),
            )
            stats["hard_deleted"] += cur.rowcount or 0
        conn.execute(
            "UPDATE models SET missing_since = ? WHERE missing_since IS NULL AND id NOT IN "
            "(SELECT DISTINCT model_id FROM model_files WHERE missing_since IS NULL)",
            (now,),
        )
        conn.execute(
            "UPDATE models SET missing_since = NULL WHERE missing_since IS NOT NULL AND id IN "
            "(SELECT DISTINCT model_id FROM model_files WHERE missing_since IS NULL)"
        )
        conn.execute("DELETE FROM models WHERE id NOT IN (SELECT model_id FROM model_files)")
        conn.commit()
        return stats

    try:
        stats = dbmod.writer().run(_op)
    except BaseException as exc:  # noqa: BLE001
        ctx.record_exception("prune", "prune", None, exc)
        stats = {"failed": True}
    # A prune that moved rows invalidates the derived links and search docs, so
    # the next scan must re-run those phases even if no file changed.
    dirty = bool(stats.get("missing") or stats.get("restored")
                 or stats.get("hard_deleted") or stats.get("failed"))
    try:
        from ...core import config_service

        if dirty != bool(config_service.get_config().raw.get("needs_relink")):
            config_service.set_config({"needs_relink": dirty})
    except Exception as exc:  # noqa: BLE001 - never let a config write fail a scan
        ctx.record_exception("prune", "prune", None, exc)
    stats["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    return stats
