"""Full-file SHA-256 / AutoV2 as a cancellable, resumable background job (B2).

AutoV2 = the first 10 hex characters of the **full-file** SHA-256, uppercased.
The previous implementation hashed 64 KB after the safetensors header and
returned ``E3B0C44298`` - the SHA-256 of the empty string.

Hashing NEVER runs inside a scan (DECISIONS C1).  A model card is fully usable
with ``hash_state='unhashed'``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncIterator

from ..core import config_service, errors, progress
from ..core import db as dbmod
from ..core.pathsafe import long_path

log = logging.getLogger(__name__)

CHUNK = 8 * 1024 * 1024
MAX_ATTEMPTS = 3
BACKOFF_S = (1.0, 4.0, 15.0)

PRIORITY_SINGLE = 0
PRIORITY_CATEGORY = 5
PRIORITY_BULK = 10


def compute_sha256(path: str, *, cancel: threading.Event | None = None,
                   throttle_mbps: int = 0,
                   on_chunk=None) -> tuple[str | None, str | None, int]:
    """Stream the whole file.  Returns (sha256_hex, error_code, bytes_read)."""
    h = hashlib.sha256()
    done = 0
    budget = float(throttle_mbps) * 1024 * 1024 if throttle_mbps > 0 else 0.0
    window_start = time.monotonic()
    window_bytes = 0
    try:
        with open(long_path(path), "rb") as fh:
            while True:
                if cancel is not None and cancel.is_set():
                    return None, "CANCELLED", done
                block = fh.read(CHUNK)
                if not block:
                    break
                h.update(block)
                done += len(block)
                if on_chunk is not None:
                    on_chunk(done)
                if budget:
                    window_bytes += len(block)
                    elapsed = time.monotonic() - window_start
                    allowed = budget * max(elapsed, 1e-6)
                    if window_bytes > allowed:
                        time.sleep(min(2.0, (window_bytes - allowed) / budget))
                    if elapsed > 5.0:
                        window_start = time.monotonic()
                        window_bytes = 0
    except OSError as exc:
        return None, errors.classify_os_error(exc), done
    return h.hexdigest(), None, done


def autov2(sha256_hex: str | None) -> str | None:
    return sha256_hex[:10].upper() if sha256_hex else None


def compute_autov2(path: str) -> str | None:
    """Convenience entry point used by tests and one-off checks."""
    digest, _code, _n = compute_sha256(path)
    return autov2(digest)


class HashService:
    """A real table-backed queue, so it survives an app restart."""

    def __init__(self) -> None:
        self.bus = progress.bus("hash")
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._workers: list[threading.Thread] = []
        self._active: dict[int, dict] = {}
        self._batch_cancel: set[str] = set()
        self._started_at: int | None = None
        self._stopping = False

    # -- frozen interface -------------------------------------------------
    def enqueue(self, scope: str = "unhashed_only", **kw) -> dict:
        """scope: all | unhashed_only | category:<name> | folder:<path> | ids"""
        uids = kw.get("uids") or kw.get("ids") or []
        priority = int(kw.get("priority") or _default_priority(scope, uids))
        batch_id = str(kw.get("batch_id") or uuid.uuid4().hex[:12])
        where, params = _scope_where(scope, uids, kw)
        conn = dbmod.get_ro()
        # `where` is built by _scope_where from fixed fragments; values are bound.
        sql = (
            "SELECT f.id, f.size FROM model_files f JOIN models m ON m.id = f.model_id "  # noqa: S608
            f"WHERE f.missing_since IS NULL AND {where}"
        )
        rows = dbmod.rows(conn, sql, tuple(params))
        if not rows:
            return {"batch_id": batch_id, "queued": 0, "bytes_total": 0, "eta_ms": 0}
        now = dbmod.now_ms()
        items = [(int(r["id"]), batch_id, priority, int(r["size"]), now) for r in rows]
        bytes_total = sum(int(r["size"]) for r in rows)

        def _op(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO hash_jobs(model_file_id,batch_id,priority,size,enqueued_at,"
                "state) VALUES (?,?,?,?,?,'queued') "
                "ON CONFLICT(model_file_id) DO UPDATE SET batch_id=excluded.batch_id, "
                "priority=MIN(hash_jobs.priority, excluded.priority), "
                "state=CASE WHEN hash_jobs.state IN ('running','done') THEN hash_jobs.state "
                "ELSE 'queued' END, attempts=0, error_code=NULL, error_message=NULL, "
                "enqueued_at=excluded.enqueued_at",
                items,
            )
            ids = [i[0] for i in items]
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                ph = ",".join("?" * len(chunk))
                conn.execute(
                    f"UPDATE model_files SET hash_state='queued' WHERE id IN ({ph}) "  # noqa: S608
                    "AND hash_state NOT IN ('done','hashing')", chunk,
                )
            conn.commit()
            return len(items)

        queued = int(dbmod.writer().run(_op))
        self._batch_cancel.discard(batch_id)
        self._ensure_workers()
        return {
            "batch_id": batch_id, "queued": queued, "bytes_total": bytes_total,
            "eta_ms": int(bytes_total / (150 * 1024 * 1024) * 1000) if bytes_total else 0,
        }

    def cancel(self, batch_id: str | None = None,
               uids: list[str] | None = None) -> dict:
        if batch_id:
            self._batch_cancel.add(batch_id)
        ids = _ids_from_uids(uids) if uids else None

        def _op(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            if batch_id:
                cur = conn.execute(
                    "UPDATE hash_jobs SET state='cancelled', finished_at=? "
                    "WHERE batch_id=? AND state IN ('queued','running')",
                    (dbmod.now_ms(), batch_id),
                )
            elif ids:
                ph = ",".join("?" * len(ids))
                cur = conn.execute(
                    f"UPDATE hash_jobs SET state='cancelled', finished_at=? "  # noqa: S608
                    f"WHERE model_file_id IN ({ph}) AND state IN ('queued','running')",
                    (dbmod.now_ms(), *ids),
                )
            else:
                cur = conn.execute(
                    "UPDATE hash_jobs SET state='cancelled', finished_at=? "
                    "WHERE state IN ('queued','running')", (dbmod.now_ms(),),
                )
            n = cur.rowcount or 0
            conn.execute(
                "UPDATE model_files SET hash_state='unhashed' WHERE hash_state IN "
                "('queued','hashing') AND id IN (SELECT model_file_id FROM hash_jobs "
                "WHERE state='cancelled')"
            )
            conn.commit()
            return n

        cancelled = int(dbmod.writer().run(_op))
        if not batch_id and not uids:
            self._cancel.set()
        return {"cancelled": cancelled, "batch_id": batch_id}

    def status(self) -> dict:
        conn = dbmod.get_ro()
        counts = {r["state"]: int(r["n"]) for r in dbmod.rows(
            conn, "SELECT state, COUNT(*) n FROM hash_jobs GROUP BY state")}
        pending = dbmod.one(
            conn,
            "SELECT COALESCE(SUM(size),0) total, COALESCE(SUM(bytes_done),0) done "
            "FROM hash_jobs WHERE state IN ('queued','running')",
        )
        total = int(pending["total"] or 0) if pending else 0
        done = int(pending["done"] or 0) if pending else 0
        hashed = int(dbmod.scalar(
            conn, "SELECT COUNT(*) FROM model_files WHERE hash_state='done'") or 0)
        unhashed = int(dbmod.scalar(
            conn, "SELECT COUNT(*) FROM model_files WHERE hash_state='unhashed'") or 0)
        active = list(self._active.values())
        rate = 0.0
        if self._started_at and active:
            elapsed = max(0.001, (dbmod.now_ms() - self._started_at) / 1000)
            rate = round(done / elapsed / (1024 * 1024), 1)
        return {
            "running": self.running(), "states": counts,
            "queued": counts.get("queued", 0), "active": active,
            "bytes_total": total, "bytes_done": done,
            "mbps": rate,
            "eta_ms": progress.eta_ms(done, total,
                                      max(0.001, (dbmod.now_ms() - (self._started_at or 0)) / 1000))
            if self._started_at and total else None,
            "hashed": hashed, "unhashed": unhashed,
            "concurrency": config_service.get_config().hash_concurrency,
            "throttle_mbps": config_service.get_config().hash_throttle_mbps,
        }

    async def subscribe(self) -> AsyncIterator[tuple[str, dict]]:
        async for item in self.bus.subscribe():
            yield item

    # -- workers ----------------------------------------------------------
    def running(self) -> bool:
        return any(t.is_alive() for t in self._workers)

    def resume_pending(self) -> int:
        """Startup recovery: a 'running' row from a dead process goes back to queued."""
        def _op(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE hash_jobs SET state='queued', started_at=NULL, bytes_done=0 "
                "WHERE state='running'")
            n = cur.rowcount or 0
            conn.execute(
                "UPDATE model_files SET hash_state='queued' WHERE hash_state='hashing'")
            conn.commit()
            return n

        try:
            n = int(dbmod.writer().run(_op))
        except BaseException:  # noqa: BLE001
            return 0
        if _queued_count() > 0:
            self._ensure_workers()
        return n

    def shutdown(self) -> None:
        self._stopping = True
        self._cancel.set()

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._cancel = threading.Event() if self._cancel.is_set() else self._cancel
            want = max(1, min(4, config_service.get_config().hash_concurrency))
            self._workers = [t for t in self._workers if t.is_alive()]
            if self._started_at is None or not self._workers:
                self._started_at = dbmod.now_ms()
            while len(self._workers) < want:
                t = threading.Thread(target=self._worker, name="vault-hash", daemon=True)
                t.start()
                self._workers.append(t)

    def _claim(self) -> dict | None:
        def _op(conn: sqlite3.Connection) -> dict | None:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT j.id, j.model_file_id, j.batch_id, j.size, j.attempts, "
                "f.abs_path, f.size AS fsize, f.probe_sha256 FROM hash_jobs j "
                "JOIN model_files f ON f.id = j.model_file_id "
                "WHERE j.state='queued' ORDER BY j.priority, j.enqueued_at LIMIT 1"
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE hash_jobs SET state='running', started_at=?, bytes_done=0 WHERE id=?",
                (dbmod.now_ms(), int(row["id"])),
            )
            conn.execute("UPDATE model_files SET hash_state='hashing' WHERE id=?",
                         (int(row["model_file_id"]),))
            conn.commit()
            return dict(row)

        try:
            return dbmod.writer().run(_op)
        except BaseException:  # noqa: BLE001
            return None

    def _reuse(self, job: dict) -> str | None:
        """Content-addressed reuse: a moved file keeps its hash (ARCHITECTURE 4.2)."""
        probe = job.get("probe_sha256")
        if not probe:
            return None
        conn = dbmod.get_ro()
        row = dbmod.one(
            conn,
            "SELECT sha256 FROM model_files WHERE size = ? AND probe_sha256 = ? "
            "AND sha256 IS NOT NULL AND id <> ? LIMIT 1",
            (int(job["fsize"]), str(probe), int(job["model_file_id"])),
        )
        return str(row["sha256"]) if row else None

    def _worker(self) -> None:
        cfg = config_service.get_config()
        while not self._cancel.is_set() and not self._stopping:
            job = self._claim()
            if job is None:
                break
            job_id = int(job["id"])
            file_id = int(job["model_file_id"])
            path = str(job["abs_path"])
            batch_id = job.get("batch_id")
            self._active[job_id] = {
                "model_file_id": file_id, "path": path, "size": int(job["fsize"] or 0),
                "bytes_done": 0,
            }
            if batch_id in self._batch_cancel:
                self._finish(job_id, file_id, state="cancelled")
                self._active.pop(job_id, None)
                continue

            reused = self._reuse(job)
            if reused:
                self._finish(job_id, file_id, state="done", sha=reused, reused=True)
                self._active.pop(job_id, None)
                continue

            digest, code, _read = compute_sha256(
                path, cancel=self._cancel, throttle_mbps=cfg.hash_throttle_mbps,
                on_chunk=self._chunk_reporter(job_id, path))
            self._active.pop(job_id, None)
            if digest:
                self._finish(job_id, file_id, state="done", sha=digest)
            elif code == "CANCELLED":
                self._finish(job_id, file_id, state="cancelled")
                break
            else:
                attempts = int(job["attempts"] or 0) + 1
                retry = attempts < MAX_ATTEMPTS and code == errors.FILE_LOCKED
                self._finish(job_id, file_id, state="queued" if retry else "failed",
                             code=code, attempts=attempts)
                if retry:
                    time.sleep(BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)])
        with self._lock:
            self._workers = [t for t in self._workers if t.is_alive() and t is not
                             threading.current_thread()]
            if not self._workers:
                self._started_at = None
                self.bus.publish("done", {"phase": "hash", "status": "idle",
                                          **{k: v for k, v in self.status().items()
                                             if k in ("hashed", "unhashed")}})

    def _chunk_reporter(self, job_id: int, path: str):
        """Bound per job, so the callback never closes over a loop variable."""
        last = [0.0]

        def _on_chunk(done: int) -> None:
            active = self._active.get(job_id)
            if active is None:
                return
            active["bytes_done"] = done
            now = time.monotonic()
            if now - last[0] >= 0.25:
                last[0] = now
                self._checkpoint(job_id, done)
                self.bus.publish("progress", {
                    "job_id": job_id, "phase": "hash", "done": done,
                    "total": active["size"], "current": path,
                }, coalesce_key=f"hash:{job_id}")

        return _on_chunk

    def _checkpoint(self, job_id: int, done: int) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE hash_jobs SET bytes_done=? WHERE id=?", (done, job_id))
            conn.commit()

        try:
            dbmod.writer().submit(_op)
        except BaseException as exc:  # noqa: BLE001
            log.debug("ignored (best effort): %s", exc)

    def _finish(self, job_id: int, file_id: int, *, state: str, sha: str | None = None,
                code: str | None = None, attempts: int | None = None,
                reused: bool = False) -> None:
        now = dbmod.now_ms()
        av = autov2(sha)

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE hash_jobs SET state=?, finished_at=?, error_code=?, "
                "attempts=COALESCE(?, attempts) WHERE id=?",
                (state, now, code, attempts, job_id),
            )
            if state == "done" and sha:
                conn.execute(
                    "UPDATE model_files SET hash_state='done', sha256=?, autov2=?, "
                    "hashed_at=?, hash_error=NULL WHERE id=?", (sha, av, now, file_id),
                )
                conn.execute(
                    "UPDATE models SET canonical_key=?, civitai_state=CASE WHEN "
                    "civitai_state='none' THEN 'pending' ELSE civitai_state END "
                    "WHERE primary_file_id=?", (sha, file_id),
                )
            elif state == "failed":
                conn.execute(
                    "UPDATE model_files SET hash_state='failed', hash_error=? WHERE id=?",
                    (code, file_id),
                )
            elif state == "cancelled":
                conn.execute(
                    "UPDATE model_files SET hash_state='unhashed' WHERE id=? "
                    "AND hash_state<>'done'", (file_id,),
                )
            elif state == "queued":
                conn.execute(
                    "UPDATE model_files SET hash_state='queued' WHERE id=?", (file_id,))
            conn.commit()

        try:
            dbmod.writer().run(_op)
        except BaseException:
            log.warning("could not persist hash result for %s", file_id, exc_info=True)
        self.bus.publish("item", {
            "kind": "model_file", "id": file_id, "state": state, "autov2": av,
            "sha256": sha, "reused": reused, "error_code": code,
        })


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------

def _default_priority(scope: str, uids) -> int:
    if uids:
        return PRIORITY_SINGLE
    if scope.startswith(("category:", "folder:")):
        return PRIORITY_CATEGORY
    return PRIORITY_BULK


def _ids_from_uids(uids) -> list[int]:
    out: list[int] = []
    for uid in uids or []:
        text = str(uid)
        if ":" in text:
            kind, _sep, num = text.partition(":")
            if kind not in ("model", "model_file"):
                continue
            text = num
        try:
            out.append(int(text))
        except (TypeError, ValueError):
            continue
    return out


def _scope_where(scope: str, uids, kw) -> tuple[str, list]:
    scope = (scope or "unhashed_only").strip()
    if uids:
        ids = _ids_from_uids(uids)
        if not ids:
            return "1=0", []
        ph = ",".join("?" * len(ids))
        return f"(m.id IN ({ph}) OR f.id IN ({ph}))", [*ids, *ids]
    if scope == "all":
        return "f.hash_state <> 'done'", []
    if scope == "unhashed_only":
        return "f.hash_state IN ('unhashed','failed','stale')", []
    if scope.startswith("category:"):
        return "m.category = ? AND f.hash_state <> 'done'", [scope.split(":", 1)[1]]
    if scope.startswith("folder:"):
        folder = scope.split(":", 1)[1].replace("\\", "/").strip("/")
        return "f.folder LIKE ? AND f.hash_state <> 'done'", [folder + "%"]
    return "f.hash_state IN ('unhashed','failed','stale')", []


def _queued_count() -> int:
    try:
        conn = dbmod.get_ro()
        return int(dbmod.scalar(
            conn, "SELECT COUNT(*) FROM hash_jobs WHERE state='queued'") or 0)
    except sqlite3.DatabaseError:
        return 0


_service: HashService | None = None
_lock = threading.Lock()


def get_hash_service() -> HashService:
    global _service
    with _lock:
        if _service is None:
            _service = HashService()
        return _service


def shutdown_hash_service() -> None:
    global _service
    with _lock:
        if _service is not None:
            _service.shutdown()
            _service = None


def _unused(_: os) -> None:  # pragma: no cover
    return None
