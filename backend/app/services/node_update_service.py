"""Node-package update checks: compare the recorded commit to the remote tip.

One ``git ls-remote`` per package through the same hardened runner and host
allowlist as the Enable clone path - no clone, no checkout, nothing executed,
and only the repository URL ever leaves the machine.  ``commits_behind`` is
deliberately left NULL: counting it would need a fetch, and "the remote tip
is not the commit you have" is the honest fact one ls-remote can establish.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid

from ..core import config_service
from ..core import db as dbmod
from ..enable import git_fetch
from ..enable.hosts import HostNotAllowed

log = logging.getLogger(__name__)

LS_REMOTE_TIMEOUT_S = 30
#: One batch thread at a time; a second request while one runs re-queues only
#: what is still 'pending', so nothing is double-checked.
_batch_lock = threading.Lock()
_batch_running = False

_HEX = set("0123456789abcdef")


def _is_sha(text: str | None) -> bool:
    s = str(text or "").lower()
    return len(s) == 40 and all(ch in _HEX for ch in s)


def _persist(package_id: int, *, state: str, has_update: bool | None = None,
             latest_commit: str | None = None, note: str | None = None) -> None:
    now = dbmod.now_ms()

    def _op(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE node_packages SET update_check_state=?, update_checked_at=?, "
            "has_update=COALESCE(?, has_update), "
            "latest_commit=COALESCE(?, latest_commit), "
            "update_notes=?, updated_at=? WHERE id=?",
            (state, now,
             None if has_update is None else (1 if has_update else 0),
             latest_commit, note, now, int(package_id)),
        )
        conn.commit()

    dbmod.writer().run(_op)


def check_package(package_id: int) -> dict:
    """Check one package now.  Persists the result and returns it."""
    conn = dbmod.get_ro()
    row = dbmod.one(conn, "SELECT repo_url, repo_url_suspect, git_branch, git_commit "
                          "FROM node_packages WHERE id = ?", (int(package_id),))
    if row is None:
        return {"state": "none", "reason": "not_found"}
    if row["repo_url_suspect"]:
        _persist(package_id, state="suspect_remote")
        return {"state": "suspect_remote", "reason": "remote does not match folder"}
    if not row["repo_url"]:
        return {"state": "none", "reason": "no git remote recorded"}
    if not _is_sha(row["git_commit"]):
        _persist(package_id, state="error", note="no local commit recorded")
        return {"state": "error", "reason": "no local commit recorded"}

    try:
        tip, error = git_fetch.resolve_revision(
            str(row["repo_url"]), ref=row["git_branch"],
            timeout_s=LS_REMOTE_TIMEOUT_S)
    except HostNotAllowed as exc:
        _persist(package_id, state="error", note=str(exc)[:200])
        return {"state": "error", "reason": str(exc)[:200]}
    if tip is None:
        _persist(package_id, state="error", note=error)
        return {"state": "error", "reason": error}

    behind = tip != str(row["git_commit"]).lower()
    _persist(package_id, state="ok", has_update=behind,
             latest_commit=tip if behind else str(row["git_commit"]).lower(),
             note=None)
    return {"state": "ok",
            "reason": None if behind else "up to date",
            "has_update": behind, "latest_commit": tip}


def _batch_worker() -> None:
    global _batch_running
    try:
        while True:
            conn = dbmod.get_ro()
            row = dbmod.one(conn, "SELECT id FROM node_packages "
                                  "WHERE update_check_state = 'pending' "
                                  "ORDER BY id LIMIT 1")
            if row is None:
                return
            if not config_service.get_config().online_enabled:
                _persist(int(row["id"]), state="offline",
                         note="online checks were disabled mid-run")
                continue
            try:
                check_package(int(row["id"]))
            except Exception:
                log.exception("update check failed for package %s", row["id"])
                _persist(int(row["id"]), state="error", note="internal error")
    finally:
        with _batch_lock:
            _batch_running = False


def enqueue_checks(ids: list[int] | None) -> dict:
    """Mark packages 'pending' and make sure one worker thread is draining."""
    global _batch_running

    def _mark(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        if ids:
            ph = ",".join("?" * len(ids))
            cur = conn.execute(
                f"UPDATE node_packages SET update_check_state='pending' "  # noqa: S608
                f"WHERE id IN ({ph}) AND repo_url IS NOT NULL "
                "AND repo_url_suspect = 0 AND missing_since IS NULL",
                [int(i) for i in ids])
        else:
            cur = conn.execute(
                "UPDATE node_packages SET update_check_state='pending' "
                "WHERE repo_url IS NOT NULL AND repo_url_suspect = 0 "
                "AND missing_since IS NULL")
        count = cur.rowcount or 0
        conn.commit()
        return count

    queued = int(dbmod.writer().run(_mark))
    with _batch_lock:
        if queued and not _batch_running:
            _batch_running = True
            threading.Thread(target=_batch_worker, daemon=True,
                             name="node-update-check").start()
    return {"job_id": f"upd-{uuid.uuid4().hex[:8]}", "queued": queued}
