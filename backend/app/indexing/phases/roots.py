"""Phase 0 - resolve and persist the roots table."""

from __future__ import annotations

import sqlite3

from ...core import config_service
from ...core import db as dbmod
from ...core.pathsafe import normalize, path_key


def run(ctx) -> dict:
    cfg = ctx.cfg
    roots = list(cfg.roots)
    now = dbmod.now_ms()

    rows = []
    for r in roots:
        p = normalize(r.path)
        available = 1
        try:
            available = 1 if p.is_dir() else 0
        except OSError:
            available = 0
        rows.append((
            r.kind, str(p), path_key(p), r.label, r.category,
            1 if r.is_default else 0, r.source, available, now if available else None, now,
        ))

    configured_keys = [row[2] for row in rows]

    def _op(conn: sqlite3.Connection) -> dict[str, int]:
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            conn.execute(
                "INSERT INTO roots(kind,path,path_key,label,category,is_default,source,"
                "available,last_seen_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path_key) DO UPDATE SET kind=excluded.kind, "
                "path=excluded.path, label=excluded.label, category=excluded.category, "
                "is_default=excluded.is_default, source=excluded.source, "
                "available=excluded.available, last_seen_at=excluded.last_seen_at",
                row,
            )
        # C7.3 - rows for a root the owner has pointed away from are RETAINED,
        # never pruned: they carry ratings, tags, notes and album membership that
        # no re-scan could rebuild.  Retiring the root row (available=0) is what
        # makes that a guarantee rather than an accident - phase 9 skips every
        # table row whose root is unavailable, so the old library survives even
        # if that drive is later disconnected.  The root row itself is kept so
        # the UI can name what it is holding and offer an explicit prune.
        if configured_keys:
            ph = ",".join("?" * len(configured_keys))
            conn.execute(
                f"UPDATE roots SET available = 0 WHERE path_key NOT IN ({ph})",  # noqa: S608
                configured_keys,
            )
        conn.commit()
        out: dict[str, int] = {}
        for r in conn.execute("SELECT id, path_key FROM roots"):
            out[r["path_key"]] = int(r["id"])
        return out

    ctx.root_ids = dbmod.writer().run(_op)
    unavailable = [r.label for r, row in zip(roots, rows, strict=False) if not row[7]]
    for r, row in zip(roots, rows, strict=False):
        if not row[7]:
            ctx.record_error("roots", "root", row[1], "PERMISSION_DENIED",
                             f"Root '{r.label}' is not reachable; existing rows are kept.")
    config_service.invalidate()
    return {"roots": len(rows), "unavailable": unavailable}
