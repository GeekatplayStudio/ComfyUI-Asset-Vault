"""IndexerService: executors, the phase machine, and the batched-commit engine.

The commit strategy is the direct fix for B1's total data loss: batches of 256
rows, every item wrapped in its own ``SAVEPOINT``, so one malformed file rolls
back only itself while the other 255 commit.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
import traceback
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import config_service, errors, progress
from ..core import db as dbmod
from ..core.pathsafe import Root, normalize, path_key

log = logging.getLogger(__name__)

BATCH = 256
PHASES = ("roots", "walk", "models", "nodes", "workflows", "outputs", "links",
          "index", "prune")
PHASE_LABELS = {
    "roots": "Roots", "walk": "Scanning folders", "models": "Models",
    "nodes": "Node packages", "workflows": "Workflows", "outputs": "Outputs",
    "links": "Linking", "index": "Search index", "prune": "Cleanup",
}

_CPU = os.cpu_count() or 4


@dataclass
class ScanContext:
    job_id: int
    kind: str
    trigger: str
    force: bool
    enrich_online: bool
    phases: tuple[str, ...]
    cfg: Any
    cancel: threading.Event
    bus: progress.ProgressBus
    ex_io: ThreadPoolExecutor
    ex_ast: ThreadPoolExecutor
    ex_img: ThreadPoolExecutor
    root_ids: dict[str, int] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    error_count: int = 0
    items_total: int = 0
    items_done: int = 0
    items_skipped: int = 0
    started: float = field(default_factory=time.perf_counter)
    _errors: list[tuple] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- errors -----------------------------------------------------------
    def record_error(self, phase: str, kind: str, path: str | None, code: str,
                     message: str, tb: str | None = None) -> None:
        """Bind only TEXT/INTEGER so the error path can never itself fail."""
        with self._lock:
            self.error_count += 1
            self._errors.append((
                int(self.job_id), str(phase)[:40], str(kind)[:40],
                str(path)[:1024] if path else None,
                str(code)[:60] if code in errors.SCAN_ERROR_CODES else errors.UNKNOWN,
                str(message)[:2000], (str(tb)[:2000] if tb else None),
                dbmod.now_ms(),
            ))
        self.bus.publish("error", {
            "job_id": self.job_id, "kind": kind, "path": path, "code": code,
            "message": str(message)[:400],
        })

    def record_exception(self, phase: str, kind: str, path: str | None,
                         exc: BaseException) -> None:
        code = errors.classify_os_error(exc)
        tb = "".join(traceback.format_exception_only(type(exc), exc))
        head = "".join(traceback.format_tb(exc.__traceback__, limit=3)) if exc.__traceback__ else None
        self.record_error(phase, kind, path, code, tb.strip(), head)

    def drain_errors(self) -> list[tuple]:
        with self._lock:
            out = self._errors
            self._errors = []
            return out

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def root_id(self, root: Root) -> int:
        return self.root_ids.get(path_key(root.path), 0)

    def bump(self, n: int = 0, skipped: int = 0) -> None:
        """A skipped item counts toward the total but never toward 'done', so a
        no-change scan reports items_skipped == items_total."""
        with self._lock:
            self.items_done += n
            self.items_skipped += skipped
            self.items_total += skipped


# ---------------------------------------------------------------------------
# Batched, per-item-isolated commit engine
# ---------------------------------------------------------------------------

def commit_batches(ctx: ScanContext, phase: str, kind: str,
                   items: Sequence[Any],
                   upsert: Callable[[sqlite3.Connection, Any], Any],
                   *, on_progress: Callable[[int, int], None] | None = None,
                   batch: int = BATCH) -> list[Any]:
    """Write ``items`` in transactions of ``batch``, isolating each with a SAVEPOINT.

    Returns whatever each successful ``upsert`` returned, in order.
    """
    results: list[Any] = []
    total = len(items)
    if not total:
        return results
    writer = dbmod.writer()
    seq = 0
    for start in range(0, total, batch):
        if ctx.cancelled():
            break
        chunk = list(items[start:start + batch])
        base_seq = seq
        seq += len(chunk)

        def _op(conn: sqlite3.Connection, chunk=chunk, base_seq=base_seq) -> list[Any]:
            out: list[Any] = []
            conn.execute("BEGIN IMMEDIATE")
            for i, item in enumerate(chunk):
                sp = f"sp_{base_seq + i}"
                conn.execute(f"SAVEPOINT {sp}")
                try:
                    out.append(upsert(conn, item))
                    conn.execute(f"RELEASE {sp}")
                except BaseException as exc:  # noqa: BLE001 - isolated per item
                    try:
                        conn.execute(f"ROLLBACK TO {sp}")
                        conn.execute(f"RELEASE {sp}")
                    except sqlite3.Error:
                        pass
                    ctx.record_exception(phase, kind, _item_path(item), exc)
                    out.append(None)
            _flush_errors(conn, ctx)
            conn.commit()
            return out

        try:
            results.extend(writer.run(_op))
        except BaseException as exc:  # noqa: BLE001 - a whole batch failing is recorded
            ctx.record_exception(phase, kind, None, exc)
            results.extend([None] * len(chunk))
        if on_progress is not None:
            on_progress(min(start + len(chunk), total), total)
    return results


def _item_path(item: Any) -> str | None:
    for attr in ("abs_path", "path"):
        v = getattr(item, attr, None)
        if isinstance(v, str):
            return v
    if isinstance(item, dict):
        v = item.get("abs_path") or item.get("path")
        if isinstance(v, str):
            return v
    return None


def _flush_errors(conn: sqlite3.Connection, ctx: ScanContext) -> None:
    rows = ctx.drain_errors()
    if not rows:
        return
    try:
        conn.executemany(
            "INSERT INTO scan_errors(job_id,phase,kind,abs_path,code,message,"
            "traceback_head,created_at) VALUES (?,?,?,?,?,?,?,?)", rows,
        )
    except sqlite3.Error as exc:
        log.warning("could not persist %d scan errors: %s", len(rows), exc)


def flush_errors_now(ctx: ScanContext) -> None:
    rows = ctx.drain_errors()
    if not rows:
        return

    def _op(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        with contextlib.suppress(sqlite3.Error):
            conn.executemany(
                "INSERT INTO scan_errors(job_id,phase,kind,abs_path,code,message,"
                "traceback_head,created_at) VALUES (?,?,?,?,?,?,?,?)", rows,
            )
        conn.commit()

    try:
        dbmod.writer().run(_op)
    except BaseException:
        log.warning("could not flush scan errors", exc_info=True)


def map_parallel(ctx: ScanContext, executor: ThreadPoolExecutor,
                 fn: Callable[[Any], Any], items: Iterable[Any], *,
                 phase: str, kind: str) -> list[Any]:
    """Run ``fn`` over ``items`` on ``executor``, isolating per-item failures."""
    items = list(items)
    if not items:
        return []

    def _safe(item: Any) -> Any:
        if ctx.cancelled():
            return None
        try:
            return fn(item)
        except BaseException as exc:  # noqa: BLE001 - one bad file, one error row
            ctx.record_exception(phase, kind, _item_path(item), exc)
            return None

    return list(executor.map(_safe, items))


# ---------------------------------------------------------------------------
# Off-GIL analysis (QA-PERF-1)
# ---------------------------------------------------------------------------
#
# Two of the scan's parsers hold the GIL for their whole call and cannot be
# preempted at a bytecode boundary, because both are single C calls:
#
#   * ``ast.parse`` over a custom node's source - measured up to 152 ms on one
#     563 KB file, p99 40 ms across 415 calls;
#   * ``json.loads`` over a safetensors header - measured up to 94 ms on a
#     390 KB header.
#
# A request cannot be served while one of those is running, however many worker
# threads exist, so this is not a concurrency-tuning problem: the total GIL-held
# time is invariant under thread count.  Measured per-phase with `/ping` polled
# at 100 Hz during a forced full scan of the real install: `nodes` alone gave
# p95 235 ms, `models` alone 77 ms, while `outputs` (3,834 files) gave 7 ms and
# `workflows` 18 ms.  Fewer AST threads made it no better (p95 282 ms at one
# worker vs 250 ms at four), exactly as a GIL-bound workload predicts.
#
# So the analysis moves out of the process instead.  Both functions are pure -
# a picklable dataclass in, the same dataclass back, no database, no shared
# state - which is what makes this a change of executor rather than a redesign.
_CPU_POOL_MIN_ITEMS = 4
_cpu_pool: Any = None
_cpu_pool_lock = threading.Lock()
_cpu_pool_broken = False


def cpu_pool_workers() -> int:
    """Worker processes for off-GIL analysis.

    Deliberately a quarter of the cores and never more than four: these are real
    interpreters with real memory, and the point is to get the parsers off this
    process's GIL, not to saturate the machine.
    """
    return max(1, min(4, _CPU // 4))


def _cpu_pool_for(n_items: int):
    """The shared process pool, created on first real use.  ``None`` to fall back."""
    global _cpu_pool, _cpu_pool_broken
    if _cpu_pool_broken or os.environ.get("VAULT_NO_CPU_POOL") == "1":
        return None
    if n_items < _CPU_POOL_MIN_ITEMS:
        # A warm incremental scan has nothing to analyse; it must not pay for a
        # process spawn to discover that.
        return None
    with _cpu_pool_lock:
        if _cpu_pool is None:
            try:
                import multiprocessing
                from concurrent.futures import ProcessPoolExecutor

                _cpu_pool = ProcessPoolExecutor(
                    max_workers=cpu_pool_workers(),
                    mp_context=multiprocessing.get_context("spawn"),
                )
            except BaseException:  # any failure means "use threads"
                log.warning("could not start the analysis process pool; "
                            "falling back to threads", exc_info=True)
                _cpu_pool = None
                _cpu_pool_broken = True
        return _cpu_pool


def shutdown_cpu_pool() -> None:
    """Drop the worker processes.  Called when a scan ends, so an idle vault
    does not sit on four spare interpreters."""
    global _cpu_pool
    with _cpu_pool_lock:
        pool, _cpu_pool = _cpu_pool, None
    if pool is not None:
        with contextlib.suppress(Exception):
            pool.shutdown(wait=False, cancel_futures=True)


def map_cpu(ctx: ScanContext, fn: Callable[[Any], Any], items: Iterable[Any], *,
            phase: str, kind: str, fallback: ThreadPoolExecutor) -> list[Any]:
    """Run a pure, CPU-bound ``fn`` over ``items`` in worker *processes*.

    ``fn`` must be importable by name and both its argument and its result must
    pickle.  Per-item isolation matches :func:`map_parallel`: one bad file
    produces one error row, not a failed phase.  If the pool cannot start, or
    dies mid-batch, everything still runs - on ``fallback``, exactly as before.
    """
    items = list(items)
    if not items:
        return []
    pool = _cpu_pool_for(len(items))
    if pool is None:
        return map_parallel(ctx, fallback, fn, items, phase=phase, kind=kind)

    try:
        futures = [pool.submit(fn, item) for item in items]
    except BaseException:  # a dead pool must not lose the scan
        log.warning("analysis process pool rejected work; using threads",
                    exc_info=True)
        shutdown_cpu_pool()
        return map_parallel(ctx, fallback, fn, items, phase=phase, kind=kind)

    out: list[Any] = []
    for i, fut in enumerate(futures):
        if ctx.cancelled():
            fut.cancel()
            out.append(None)
            continue
        try:
            out.append(fut.result())
        except BrokenProcessPool:
            log.warning("analysis process pool broke after %d/%d items; "
                        "finishing the rest on threads", i, len(items))
            for f in futures[i:]:
                f.cancel()
            shutdown_cpu_pool()
            global _cpu_pool_broken
            _cpu_pool_broken = True
            out.extend(map_parallel(ctx, fallback, fn, items[i:],
                                    phase=phase, kind=kind))
            return out
        except BaseException as exc:  # noqa: BLE001 - one bad file, one error row
            ctx.record_exception(phase, kind, _item_path(items[i]), exc)
            out.append(None)
    return out


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class IndexerService:
    """Owns the executors and runs at most one scan at a time."""

    def __init__(self) -> None:
        self.bus = progress.bus("index")
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._job_id: int | None = None
        self._phase: str | None = None
        self._state: dict = {"status": "idle"}
        self.ex_io = ThreadPoolExecutor(max_workers=min(8, _CPU * 2), thread_name_prefix="ix-io")
        self.ex_ast = ThreadPoolExecutor(max_workers=min(4, _CPU), thread_name_prefix="ix-ast")
        self.ex_img = ThreadPoolExecutor(max_workers=min(6, _CPU), thread_name_prefix="ix-img")

    # -- lifecycle --------------------------------------------------------
    def shutdown(self) -> None:
        self._cancel.set()
        shutdown_cpu_pool()
        for ex in (self.ex_io, self.ex_ast, self.ex_img):
            ex.shutdown(wait=False, cancel_futures=True)

    def running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    # -- frozen interface -------------------------------------------------
    def start(self, mode: str = "incremental", phases: list[str] | None = None,
              root_ids: list[int] | None = None, force: bool = False,
              enrich_online: bool = False, trigger: str = "api") -> int:
        with self._lock:
            if self.running():
                raise errors.ConflictError(
                    "A scan is already running.",
                    details={"job_id": self._job_id, "phase": self._phase},
                )
            cfg = config_service.get_config()
            if not cfg.comfyui_path:
                raise errors.NotConfigured(
                    "ComfyUI path is not configured. Run the setup wizard first."
                )
            kind = "full" if mode == "full" else ("targeted" if phases else "incremental")
            selected = tuple(p for p in PHASES if not phases or p in phases
                             or p in ("roots", "walk", "links", "index", "prune"))
            job_id = self._create_job(kind, selected, trigger, root_ids)
            self._cancel = threading.Event()
            self._job_id = job_id
            self._phase = "roots"
            ctx = ScanContext(
                job_id=job_id, kind=kind, trigger=trigger,
                force=bool(force) or mode == "full",
                enrich_online=bool(enrich_online),
                phases=tuple(phases) if phases else PHASES,
                cfg=cfg, cancel=self._cancel, bus=self.bus,
                ex_io=self.ex_io, ex_ast=self.ex_ast, ex_img=self.ex_img,
            )
            self._state = {
                "status": "running", "job_id": job_id, "phase": "roots",
                "kind": kind, "trigger": trigger, "started_at": dbmod.now_ms(),
                "items_done": 0, "items_total": 0, "items_skipped": 0, "errors": 0,
            }
            self._thread = threading.Thread(target=self._run, args=(ctx,),
                                            name=f"vault-scan-{job_id}", daemon=True)
            self._thread.start()
            return job_id

    def cancel(self, job_id: int | None = None) -> dict:
        if job_id is not None and self._job_id != job_id:
            return {"cancelled": False, "reason": "not_running"}
        if not self.running():
            return {"cancelled": False, "reason": "not_running"}
        self._cancel.set()
        return {"cancelled": True, "job_id": self._job_id}

    def status(self) -> dict:
        state = dict(self._state)
        state["running"] = self.running()
        if state.get("status") == "running":
            started = state.get("started_at") or dbmod.now_ms()
            state["elapsed_ms"] = dbmod.now_ms() - started
        return state

    async def subscribe(self) -> AsyncIterator[tuple[str, dict]]:
        async for item in self.bus.subscribe():
            yield item

    # -- job rows ---------------------------------------------------------
    def _create_job(self, kind: str, phases: Sequence[str], trigger: str,
                    root_ids: list[int] | None) -> int:
        import json

        scope = json.dumps({"phases": list(phases), "roots": root_ids or []})
        now = dbmod.now_ms()

        def _op(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO scan_jobs(kind,scope_json,status,phase,trigger,started_at,"
                "heartbeat_at,created_at) VALUES (?,?,'running','roots',?,?,?,?)",
                (kind, scope, trigger, now, now, now),
            )
            job_id = int(cur.lastrowid)
            conn.commit()
            return job_id

        return dbmod.writer().run(_op)

    def _update_job(self, ctx: ScanContext, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = [dbmod.bind(v, kind="json" if k.endswith("_json") else "text"
                           if isinstance(v, str) else "int")
                for k, v in fields.items()]
        vals.append(ctx.job_id)

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"UPDATE scan_jobs SET {cols} WHERE id = ?", vals)  # noqa: S608
            conn.commit()

        try:
            dbmod.writer().run(_op)
        except BaseException:
            log.warning("could not update scan job %s", ctx.job_id, exc_info=True)

    # -- the phase machine ------------------------------------------------
    def _run(self, ctx: ScanContext) -> None:
        from .phases import index as p_index
        from .phases import links as p_links
        from .phases import models as p_models
        from .phases import nodes as p_nodes
        from .phases import outputs as p_outputs
        from .phases import prune as p_prune
        from .phases import roots as p_roots
        from .phases import workflows as p_workflows

        phase_fns = [
            ("roots", p_roots.run),
            ("models", p_models.run),
            ("nodes", p_nodes.run),
            ("workflows", p_workflows.run),
            ("outputs", p_outputs.run),
            ("links", p_links.run),
            ("index", p_index.run),
            ("prune", p_prune.run),
        ]
        selected = [(name, fn) for name, fn in phase_fns
                    if name in ("roots", "links", "index", "prune") or name in ctx.phases]
        status = "completed"
        t0 = time.perf_counter()
        try:
            for i, (name, fn) in enumerate(selected):
                if ctx.cancelled():
                    status = "cancelled"
                    break
                if name in ("links", "index") and not _needs_relink(ctx):
                    ctx.stats[name] = {"skipped": True}
                    continue
                self._phase = name
                self._state["phase"] = name
                ctx.bus.publish("phase", {
                    "job_id": ctx.job_id, "phase": name, "index": i + 1,
                    "of": len(selected), "label": PHASE_LABELS.get(name, name.title()),
                })
                self._update_job(ctx, phase=name, heartbeat_at=dbmod.now_ms(),
                                 items_done=ctx.items_done, error_count=ctx.error_count)
                try:
                    result = fn(ctx)
                except BaseException as exc:
                    ctx.record_exception(name, "phase", None, exc)
                    log.exception("phase %s failed", name)
                    result = {"failed": True}
                ctx.stats[name] = result or {}
                self._state["items_done"] = ctx.items_done
                self._state["items_total"] = ctx.items_total
                self._state["items_skipped"] = ctx.items_skipped
                self._state["errors"] = ctx.error_count
            if ctx.cancelled() and status != "cancelled":
                status = "cancelled"
        except BaseException as exc:
            log.exception("scan failed")
            ctx.record_exception("scan", "job", None, exc)
            status = "failed"
        finally:
            flush_errors_now(ctx)
            duration = int((time.perf_counter() - t0) * 1000)
            import json
            self._update_job(
                ctx, status=status, finished_at=dbmod.now_ms(), duration_ms=duration,
                items_total=ctx.items_total, items_done=ctx.items_done,
                items_skipped=ctx.items_skipped, error_count=ctx.error_count,
                stats_json=json.dumps(ctx.stats, default=str)[:200_000],
                phase=None, heartbeat_at=dbmod.now_ms(),
            )
            self._state = {
                "status": status, "job_id": ctx.job_id, "phase": None,
                "kind": ctx.kind, "trigger": ctx.trigger,
                "items_done": ctx.items_done, "items_total": ctx.items_total,
                "items_skipped": ctx.items_skipped, "errors": ctx.error_count,
                "duration_ms": duration, "stats": ctx.stats,
                "finished_at": dbmod.now_ms(),
            }
            ctx.bus.publish("done", {
                "job_id": ctx.job_id, "status": status, "stats": ctx.stats,
                "duration_ms": duration, "errors": ctx.error_count,
            })
            config_service.invalidate()
            shutdown_cpu_pool()
            self._checkpoint_wal(ctx)

    # -- WAL --------------------------------------------------------------
    def _checkpoint_wal(self, ctx: ScanContext) -> None:
        """Truncate the write-ahead log now that the scan's writes have landed.

        A scan is the app's only bulk writer, so this is where the log is at its
        biggest and - just as importantly - where the readers that were open
        across it have finished.  Doing it here rather than on a timer means the
        checkpoint runs at the one moment it can actually succeed.

        The scan thread's own reader is released first: it read every phase's
        starting state, so it holds the *oldest* snapshot in the process and
        would cap the checkpoint at the frame the scan began on.
        """
        try:
            dbmod.release_read_snapshot()
            result = dbmod.checkpoint("TRUNCATE")
        except BaseException:  # never fail a scan over housekeeping
            log.debug("wal checkpoint after scan failed", exc_info=True)
            return
        ctx.stats["wal"] = {
            "busy": result.get("busy"), "log_pages": result.get("log_pages"),
            "checkpointed": result.get("checkpointed"),
            "bytes_before": result.get("wal_before"),
            "bytes_after": result.get("wal_after"),
        }
        if result.get("busy"):
            # Not fatal, but it means something outside this process is holding a
            # read snapshot; the log cannot be recycled past it.
            log.warning(
                "wal_checkpoint(TRUNCATE) was busy: %s of %s pages reclaimed, "
                "wal still %d bytes - another reader is pinning the log",
                result.get("checkpointed"), result.get("log_pages"),
                result.get("wal_after"))
        else:
            log.info("wal_checkpoint(TRUNCATE): %s pages, wal %d -> %d bytes",
                     result.get("checkpointed"), result.get("wal_before"),
                     result.get("wal_after"))

    # -- helpers used by the API layer ------------------------------------
    def jobs(self, limit: int = 20, offset: int = 0) -> list[dict]:
        conn = dbmod.get_ro()
        rows = dbmod.rows(
            conn,
            "SELECT * FROM scan_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        )
        return [dict(r) for r in rows]

    def job_errors(self, job_id: int | None = None, limit: int = 200,
                   offset: int = 0) -> list[dict]:
        conn = dbmod.get_ro()
        if job_id is None:
            rows = dbmod.rows(
                conn, "SELECT * FROM scan_errors ORDER BY id DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            )
        else:
            rows = dbmod.rows(
                conn,
                "SELECT * FROM scan_errors WHERE job_id = ? ORDER BY id DESC "
                "LIMIT ? OFFSET ?",
                (int(job_id), int(limit), int(offset)),
            )
        return [dict(r) for r in rows]

    def mark_interrupted(self) -> int:
        """Startup recovery: a 'running' job from a previous process is stale."""
        def _op(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE scan_jobs SET status='interrupted', finished_at=? "
                "WHERE status IN ('running','queued')", (dbmod.now_ms(),),
            )
            n = cur.rowcount
            conn.commit()
            return n

        try:
            return int(dbmod.writer().run(_op))
        except BaseException:  # noqa: BLE001
            return 0


_CONTENT_PHASES = ("models", "nodes", "workflows", "outputs")
_WROTE_KEYS = ("parsed", "analyzed", "written")


def _needs_relink(ctx: ScanContext) -> bool:
    """Linking and search indexing are pure derivations: skip them when nothing
    changed.  That is what keeps a warm no-change scan inside its 1.5 s budget."""
    if ctx.force:
        return True
    for phase in _CONTENT_PHASES:
        stats = ctx.stats.get(phase) or {}
        if any(int(stats.get(k) or 0) for k in _WROTE_KEYS):
            return True
        if stats.get("failed"):
            return True
    try:
        return bool(config_service.get_config().raw.get("needs_relink"))
    except Exception:  # noqa: BLE001 - a config read must never block a scan
        return True


_service: IndexerService | None = None
_service_lock = threading.Lock()


def get_indexer() -> IndexerService:
    global _service
    with _service_lock:
        if _service is None:
            _service = IndexerService()
        return _service


def shutdown_indexer() -> None:
    global _service
    with _service_lock:
        if _service is not None:
            _service.shutdown()
            _service = None


def resolve_root_path(root: Root) -> Path:
    return normalize(root.path)
