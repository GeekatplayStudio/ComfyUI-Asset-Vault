"""The Enable job: durable, cancellable, resumable, with SSE progress (R11).

Deliberately the same shape as ``jobs/hash_service.py`` - a real table-backed
queue claimed by a worker thread, a checkpointed byte counter, a coalesced
progress bus, and startup recovery that puts a ``running`` row from a dead
process back to ``queued``.  There is no second job system here; a multi-gigabyte
download and a multi-gigabyte hash have the same failure modes and deserve the
same machinery.

Concurrency is one worker on purpose.  Two 20 GB downloads racing for the same
spindle finish no sooner than one after the other and make the free-space
arithmetic (R6) a moving target.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator
from typing import Any

from ..core import config_service, progress
from ..core import db as dbmod
from ..core.errors import AppError, ConflictError, NotConfigured, ValidationError
from . import download, git_fetch, plan, report

log = logging.getLogger(__name__)

CHANNEL = "enable"
MAX_ITEMS = plan.MAX_ITEMS
ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("done", "failed", "cancelled", "quarantined", "skipped")


class EnableService:
    """One instance per process; obtained through :func:`get_enable_service`."""

    def __init__(self) -> None:
        self.bus = progress.bus(CHANNEL)
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._batch_cancel: set[str] = set()
        self._worker: threading.Thread | None = None
        self._active: dict[int, dict] = {}
        self._stopping = False
        self._started_at: int | None = None
        self._scan_job_ids: dict[str, int | None] = {}
        self._triggers: dict[str, str] = {}

    # -- public interface -------------------------------------------------
    def fetch(self, workflow_id: int, *, plan_token: str | None,
              item_ids: list[str] | None, confirm: bool = False,
              on_conflict: str = "fail", trigger: str = "api") -> dict:
        """Queue the confirmed items.  Refuses long before a socket is opened."""
        cfg = config_service.get_config()
        if not cfg.is_configured or cfg.comfyui_path is None:
            raise NotConfigured(
                "ComfyUI path is not configured, so there is nowhere to put "
                "anything. Run the setup wizard first.")
        if not confirm:
            raise ValidationError(
                "Fetching downloads files from the internet into your ComfyUI "
                "install and requires confirm=true. Nothing was downloaded.",
                details={"reason": "not_confirmed"})
        if on_conflict not in download.ON_CONFLICT:
            raise ValidationError(
                f"on_conflict must be one of {'|'.join(download.ON_CONFLICT)}; "
                "this endpoint never overwrites an existing file.",
                details={"allowed": list(download.ON_CONFLICT)})

        items = plan.redeem(plan_token, int(workflow_id), item_ids)
        if self.running():
            raise ConflictError(
                "An Enable fetch is already running. Wait for it or cancel it.",
                details=self.status())

        specs = [self._spec(item, on_conflict) for item in items]
        self._precheck_space(specs)

        batch_id = uuid.uuid4().hex[:12]
        job_id = self._open_scan_job(int(workflow_id), batch_id, specs)
        queued = self._enqueue(batch_id, int(workflow_id), specs)
        self._batch_cancel.discard(batch_id)
        self._scan_job_ids[batch_id] = job_id
        self._triggers[batch_id] = str(trigger)
        self._ensure_worker()
        bytes_total = sum(int(s["expected_size"] or 0) for s in specs)
        self.bus.publish("phase", {"phase": "enable", "batch_id": batch_id,
                                   "queued": queued, "bytes_total": bytes_total})
        return {
            "batch_id": batch_id, "workflow_id": int(workflow_id),
            "queued": queued, "bytes_total": bytes_total,
            "items": [{"item_id": s["item_key"], "kind": s["kind"],
                       "ref_name": s["ref_name"], "host": s["source_host"],
                       "target_abs_path": s["target_abs_path"],
                       "size": int(s["expected_size"] or 0)} for s in specs],
            "stream": "/api/v1/enable/stream",
            "started_at": dbmod.now_ms(),
            "scan_job_id": job_id,
        }

    def cancel(self, batch_id: str | None = None) -> dict:
        if batch_id:
            self._batch_cancel.add(str(batch_id))
        else:
            self._cancel.set()

        def _op(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            if batch_id:
                cur = conn.execute(
                    "UPDATE enable_jobs SET state='cancelled', finished_at=? "
                    "WHERE batch_id=? AND state IN ('queued','running')",
                    (dbmod.now_ms(), str(batch_id)))
            else:
                cur = conn.execute(
                    "UPDATE enable_jobs SET state='cancelled', finished_at=? "
                    "WHERE state IN ('queued','running')", (dbmod.now_ms(),))
            n = cur.rowcount or 0
            conn.commit()
            return n

        cancelled = int(dbmod.writer().run(_op))
        self.bus.publish("phase", {"phase": "enable", "status": "cancelling",
                                   "batch_id": batch_id, "cancelled": cancelled})
        return {"cancelled": cancelled, "batch_id": batch_id}

    def status(self, batch_id: str | None = None,
               workflow_id: int | None = None) -> dict:
        conn = dbmod.get_ro()
        where, args = "1=1", []
        if batch_id:
            where, args = "batch_id = ?", [str(batch_id)]
        elif workflow_id is not None:
            where, args = "workflow_id = ?", [int(workflow_id)]
        counts = {r["state"]: int(r["n"]) for r in dbmod.rows(
            conn, f"SELECT state, COUNT(*) n FROM enable_jobs WHERE {where} "  # noqa: S608
                  "GROUP BY state", tuple(args))}
        totals = dbmod.one(
            conn,
            f"SELECT COALESCE(SUM(expected_size),0) total, "  # noqa: S608
            f"COALESCE(SUM(bytes_done),0) done FROM enable_jobs WHERE {where} "
            "AND state IN ('queued','running')", tuple(args))
        rows = dbmod.rows(
            conn,
            f"SELECT id,batch_id,workflow_id,item_key,kind,ref_name,category,"  # noqa: S608
            f"source_host,expected_size,bytes_done,state,error_code,error_message,"
            f"target_abs_path,result_json,finished_at FROM enable_jobs WHERE {where} "
            "ORDER BY id DESC LIMIT 200", tuple(args))
        return {
            "running": self.running(),
            "states": counts,
            "queued": counts.get("queued", 0),
            "bytes_total": int((totals["total"] if totals else 0) or 0),
            "bytes_done": int((totals["done"] if totals else 0) or 0),
            "active": list(self._active.values()),
            "items": [_row_out(r) for r in rows],
            "quarantine": download.quarantine_list(),
            "git_available": git_fetch.available(),
        }

    async def subscribe(self) -> AsyncIterator[tuple[str, dict]]:
        async for item in self.bus.subscribe():
            yield item

    def running(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def resume_pending(self) -> int:
        """Startup recovery: a crashed download goes back to the queue.

        The partial bytes on disk are kept - the fetcher re-hashes the whole
        file at completion, so a resumed prefix is never trusted (R11).
        """
        def _op(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE enable_jobs SET state='queued', started_at=NULL "
                "WHERE state='running'")
            n = cur.rowcount or 0
            conn.commit()
            return n

        try:
            n = int(dbmod.writer().run(_op))
        except BaseException:  # noqa: BLE001 - startup must never hard-fail
            return 0
        if n:
            with contextlib.suppress(BaseException):
                self._ensure_worker()
        return n

    def shutdown(self) -> None:
        self._stopping = True
        self._cancel.set()

    # -- internals --------------------------------------------------------
    def _spec(self, item: plan.PlanItem, on_conflict: str) -> dict:
        payload = dict(item.payload)
        payload["item_key"] = item.item_id
        payload["on_conflict"] = (on_conflict if payload.get("kind") == "model"
                                  else "fail")
        return payload

    def _precheck_space(self, specs: list[dict]) -> None:
        """R6: refuse the whole batch before the first socket is opened."""
        need: dict[str, int] = {}
        counts: dict[str, int] = {}
        for spec in specs:
            if spec.get("kind") != "model":
                continue
            directory = os.path.dirname(str(spec["target_abs_path"]))
            need[directory] = need.get(directory, 0) + int(spec.get("expected_size") or 0)
            counts[directory] = counts.get(directory, 0) + 1
        for directory, total in need.items():
            download.check_space(directory, total, items=counts.get(directory, 1))

    def _open_scan_job(self, workflow_id: int, batch_id: str,
                       specs: list[dict]) -> int | None:
        """One ``scan_jobs`` row per batch, so quarantine can write ``scan_errors``.

        Reusing the existing job/error tables means a quarantined download shows
        up in the same error list as every other problem the vault found,
        instead of in a private log the owner never opens.
        """
        scope = json.dumps({"phases": ["enable"], "workflow_id": int(workflow_id),
                            "batch_id": batch_id, "items": len(specs)},
                           ensure_ascii=False)
        now = dbmod.now_ms()

        def _op(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO scan_jobs(kind,scope_json,status,phase,items_total,"
                "trigger,started_at,heartbeat_at,created_at) "
                "VALUES ('targeted',?,'running','enable',?,'enable',?,?,?)",
                (scope, len(specs), now, now, now))
            conn.commit()
            return int(cur.lastrowid or 0)

        try:
            return int(dbmod.writer().run(_op))
        except BaseException as exc:  # noqa: BLE001 - a missing job row is not fatal
            log.warning("could not open an enable scan_jobs row: %s", exc)
            return None

    def _close_scan_job(self, batch_id: str) -> None:
        job_id = self._scan_job_ids.get(batch_id)
        if not job_id:
            return
        conn = dbmod.get_ro()
        done = int(dbmod.scalar(
            conn, "SELECT COUNT(*) FROM enable_jobs WHERE batch_id=? AND state='done'",
            (batch_id,)) or 0)
        bad = int(dbmod.scalar(
            conn, "SELECT COUNT(*) FROM enable_jobs WHERE batch_id=? AND state IN "
                  "('failed','quarantined')", (batch_id,)) or 0)
        now = dbmod.now_ms()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE scan_jobs SET status=?, items_done=?, error_count=?, "
                "finished_at=?, heartbeat_at=? WHERE id=?",
                ("completed" if not bad else "failed", done, bad, now, now, job_id))
            conn.commit()

        try:
            dbmod.writer().run(_op)
        except BaseException as exc:  # noqa: BLE001
            log.debug("ignored (best effort): %s", exc)

    def _record_error(self, batch_id: str, *, code: str, message: str,
                      abs_path: str | None) -> None:
        job_id = self._scan_job_ids.get(batch_id)
        if not job_id:
            return

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO scan_errors(job_id,phase,kind,abs_path,code,message,"
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, "enable", "download", abs_path, str(code)[:60],
                 str(message)[:1000], dbmod.now_ms()))
            conn.commit()

        try:
            dbmod.writer().run(_op)
        except BaseException as exc:  # noqa: BLE001 - reporting must not break the job
            log.debug("ignored (best effort): %s", exc)

    def _enqueue(self, batch_id: str, workflow_id: int, specs: list[dict]) -> int:
        now = dbmod.now_ms()
        # The column carries a foreign key so history survives with
        # ON DELETE SET NULL.  A workflow that vanished between the plan and the
        # confirmation must not turn into an opaque IntegrityError: the files
        # the user chose are still the files they chose, so the link is dropped
        # and the fetch proceeds.
        wf_ref = _workflow_ref(workflow_id)
        rows = [(batch_id, wf_ref, s["item_key"], s["kind"], s["ref_name"],
                 s.get("category"), s.get("provider"), s["source_url"],
                 s["source_host"], int(s.get("expected_size") or 0),
                 s.get("expected_sha256"), s.get("root_id"), s["target_abs_path"],
                 (s["target_abs_path"] + ".part") if s["kind"] == "model" else None,
                 now, json.dumps({"on_conflict": s.get("on_conflict", "fail"),
                                  "root_path": s.get("root_path"),
                                  "expected_commit": s.get("expected_commit")},
                                 ensure_ascii=False))
                for s in specs]

        def _op(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO enable_jobs(batch_id,workflow_id,item_key,kind,ref_name,"
                "category,provider,source_url,source_host,expected_size,"
                "expected_sha256,root_id,target_abs_path,part_abs_path,enqueued_at,"
                "result_json,state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued') "
                "ON CONFLICT(batch_id,item_key) DO NOTHING", rows)
            conn.commit()
            return len(rows)

        return int(dbmod.writer().run(_op))

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._stopping:
                return
            if self._cancel.is_set():
                self._cancel = threading.Event()
            if self._worker is not None and self._worker.is_alive():
                return
            self._started_at = dbmod.now_ms()
            self._worker = threading.Thread(target=self._run, name="vault-enable",
                                            daemon=True)
            self._worker.start()

    def _claim(self) -> dict | None:
        def _op(conn: sqlite3.Connection) -> dict | None:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM enable_jobs WHERE state='queued' "
                "ORDER BY enqueued_at, id LIMIT 1").fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE enable_jobs SET state='running', started_at=? WHERE id=?",
                (dbmod.now_ms(), int(row["id"])))
            conn.commit()
            return dict(row)

        try:
            return dbmod.writer().run(_op)
        except BaseException:  # noqa: BLE001
            return None

    def _run(self) -> None:
        touched: set[str] = set()
        placed = 0
        while not self._cancel.is_set() and not self._stopping:
            job = self._claim()
            if job is None:
                break
            batch_id = str(job["batch_id"])
            touched.add(batch_id)
            if batch_id in self._batch_cancel:
                self._finish(job, state="cancelled")
                continue
            result = self._run_one(job)
            if result.get("state") == "done":
                placed += 1
        for batch_id in touched:
            self._close_scan_job(batch_id)
        with self._lock:
            self._worker = None
            self._started_at = None
        if placed:
            self._schedule_rescan()
        self.bus.publish("done", {"phase": "enable", "status": "idle",
                                  "placed": placed,
                                  "batches": sorted(touched)})

    def _run_one(self, job: dict) -> dict:
        job_id = int(job["id"])
        kind = str(job["kind"])
        extra = _json(job.get("result_json")) or {}
        self._active[job_id] = {
            "id": job_id, "kind": kind, "ref_name": job["ref_name"],
            "host": job["source_host"], "bytes_done": int(job["bytes_done"] or 0),
            "size": int(job["expected_size"] or 0),
            "target_abs_path": job["target_abs_path"],
        }
        self.bus.publish("item", {"id": job_id, "state": "running", "kind": kind,
                                  "ref_name": job["ref_name"],
                                  "host": job["source_host"]})
        try:
            result = (self._clone(job, extra) if kind == "node_package"
                      else self._download(job, extra))
        except AppError as exc:
            result = {"state": "failed", "error_code": exc.code,
                      "error_message": exc.message}
        except Exception as exc:
            log.warning("enable item %s failed", job_id, exc_info=True)
            result = {"state": "failed", "error_code": "INTERNAL",
                      "error_message": str(exc)[:300]}
        finally:
            self._active.pop(job_id, None)
        self._finish(job, **result)
        return result

    def _download(self, job: dict, extra: dict) -> dict:
        spec = download.FetchSpec(
            url=str(job["source_url"]), host=str(job["source_host"]),
            target_abs_path=str(job["target_abs_path"]),
            root_path=str(extra.get("root_path") or ""),
            expected_size=int(job["expected_size"] or 0),
            expected_sha256=(str(job["expected_sha256"])
                             if job["expected_sha256"] else None),
            on_conflict=str(extra.get("on_conflict") or "fail"),
            ref_name=str(job["ref_name"]), category=str(job["category"] or ""))
        job_id = int(job["id"])
        result = download.fetch(spec, cancel=self._cancel,
                               on_progress=self._reporter(job_id, spec))
        if result.state == "quarantined":
            self._record_error(str(job["batch_id"]), code="INTEGRITY_MISMATCH",
                               message=f"{spec.ref_name}: {result.error_message}",
                               abs_path=result.quarantine_path)
        elif result.state == "failed":
            self._record_error(str(job["batch_id"]),
                               code=str(result.error_code or "UNKNOWN"),
                               message=f"{spec.ref_name}: {result.error_message}",
                               abs_path=spec.target_abs_path)
        return {"state": result.state, "bytes_done": result.bytes_written,
                "error_code": result.error_code,
                "error_message": result.error_message,
                "result": result.as_dict()}

    def _clone(self, job: dict, extra: dict) -> dict:
        result = git_fetch.clone(str(job["source_url"]), str(job["target_abs_path"]),
                                 expected_commit=extra.get("expected_commit"))
        if result.state == "failed":
            self._record_error(str(job["batch_id"]),
                               code=str(result.error_code or "UNKNOWN"),
                               message=f"{job['ref_name']}: {result.error_message}",
                               abs_path=str(job["target_abs_path"]))
        return {"state": result.state, "bytes_done": result.bytes_written,
                "error_code": result.error_code,
                "error_message": result.error_message,
                "result": result.as_dict()}

    def _reporter(self, job_id: int, spec: download.FetchSpec):
        def _on_progress(done: int, total: int) -> None:
            active = self._active.get(job_id)
            if active is not None:
                active["bytes_done"] = done
                active["size"] = total or active["size"]
            self._checkpoint(job_id, done)
            self.bus.publish("progress", {
                "job_id": job_id, "phase": "enable", "done": int(done),
                "total": int(total or 0), "current": spec.ref_name,
                "host": spec.host,
            }, coalesce_key=f"enable:{job_id}")

        return _on_progress

    def _checkpoint(self, job_id: int, done: int) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE enable_jobs SET bytes_done=? WHERE id=?",
                         (int(done), int(job_id)))
            conn.commit()

        try:
            dbmod.writer().submit(_op)
        except BaseException as exc:  # noqa: BLE001 - checkpointing is best effort
            log.debug("ignored (best effort): %s", exc)

    def _finish(self, job: dict, *, state: str, bytes_done: int | None = None,
                error_code: str | None = None, error_message: str | None = None,
                result: dict | None = None) -> None:
        job_id = int(job["id"])
        blob = json.dumps(result, ensure_ascii=False, default=str) if result else None
        now = dbmod.now_ms()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE enable_jobs SET state=?, finished_at=?, error_code=?, "
                "error_message=?, result_json=COALESCE(?, result_json), "
                "bytes_done=COALESCE(?, bytes_done), attempts=attempts+1 WHERE id=?",
                (state, now, error_code, (error_message or "")[:1000] or None,
                 blob, bytes_done, job_id))
            conn.commit()

        try:
            dbmod.writer().run(_op)
        except BaseException:
            log.warning("could not persist enable result for %s", job_id, exc_info=True)
        self.bus.publish("item", {
            "id": job_id, "kind": job["kind"], "ref_name": job["ref_name"],
            "state": state, "error_code": error_code,
            "error_message": error_message,
            "target_abs_path": job["target_abs_path"],
            **({"result": result} if result else {}),
        })

    def _schedule_rescan(self) -> None:
        """Newly placed files are invisible until they are indexed (C9.8)."""
        try:
            from ..indexing.service import get_indexer

            indexer = get_indexer()
            if indexer.running():
                return
            indexer.start(mode="incremental", trigger="enable")
        except Exception as exc:  # noqa: BLE001 - a skipped rescan is informational
            log.info("post-fetch rescan not started: %s", exc)


def _workflow_ref(workflow_id: int) -> int | None:
    try:
        row = dbmod.one(dbmod.get_ro(), "SELECT id FROM workflows WHERE id = ?",
                        (int(workflow_id),))
    except Exception as exc:  # noqa: BLE001 - a probe never breaks the fetch
        log.debug("workflow probe failed: %s", exc)
        return None
    return int(row["id"]) if row else None


def _json(blob: Any) -> dict | None:
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _row_out(row) -> dict:
    out = dict(row)
    out["uid"] = f"enable_job:{int(out['id'])}"
    out["result"] = _json(out.pop("result_json", None))
    return out


_service: EnableService | None = None
_service_lock = threading.Lock()


def get_enable_service() -> EnableService:
    global _service
    with _service_lock:
        if _service is None:
            _service = EnableService()
        return _service


def shutdown_enable_service() -> None:
    global _service
    with _service_lock:
        if _service is not None:
            _service.shutdown()
            _service = None


def build_report(workflow_id: int, *, on_conflict: str = "fail") -> dict:
    """Thin re-export so callers only ever import one module."""
    return report.build(int(workflow_id), on_conflict=on_conflict)


def recheck(workflow_id: int) -> dict:
    return report.recheck(int(workflow_id))


__all__ = ["EnableService", "build_report", "get_enable_service", "recheck",
           "shutdown_enable_service"]
