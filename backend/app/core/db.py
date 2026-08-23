"""SQLite access: pragmas, thread-local readers, a single writer thread, bind guard."""

from __future__ import annotations

import json
import math
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from ..config import DB_PATH

_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous  = NORMAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
    "PRAGMA temp_store   = MEMORY",
    "PRAGMA mmap_size    = 268435456",
    "PRAGMA cache_size   = -65536",
)

_local = threading.local()
_db_path: Path = Path(DB_PATH)
_writer: WriteQueue | None = None
_writer_lock = threading.Lock()

#: Every read-only connection handed out by :func:`get_ro`, keyed by ``id``.
#: Thread-locals are invisible from any other thread, so without this the app
#: could neither count its readers nor close them - and a reader nobody can
#: close is what pins a WAL snapshot forever.
_readers: dict[int, tuple[sqlite3.Connection, threading.Thread, str]] = {}
_readers_lock = threading.Lock()


def set_db_path(path: str | Path) -> None:
    """Point the whole layer at a different DB file (tests, alternate vaults)."""
    global _db_path, _writer
    with _writer_lock:
        if _writer is not None:
            _writer.stop()
            _writer = None
    # Dropping the references without closing leaks a reader per thread, and a
    # leaked reader keeps a read mark on the old database's WAL.
    close_all_connections()
    _db_path = Path(path)
    _local.__dict__.clear()


def db_path() -> Path:
    return _db_path


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Bind guard (ARCHITECTURE 3.5) - makes B1's crash class structurally impossible
# ---------------------------------------------------------------------------

def bind(v: Any, *, kind: str = "text") -> Any:
    """Coerce any Python value into a SQLite-bindable scalar.  Never raises."""
    try:
        if v is None:
            return None
        if kind == "json":
            if isinstance(v, str):
                return v
            if isinstance(v, (bytes, bytearray)):
                return bytes(v).decode("utf-8", "replace")
            try:
                return json.dumps(v, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return json.dumps(str(v)[:2000])
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, (list, tuple, set, dict)):
            # A non-scalar bound to a scalar column is exactly the B1 crash.
            return None
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v)
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                return None
            return int(v) if kind == "int" else v
        if isinstance(v, int):
            if kind == "text":
                return str(v)
            if abs(v) > 0x7FFFFFFFFFFFFFFF:
                return None
            return v
        if isinstance(v, Path):
            return str(v)
        if isinstance(v, str):
            if kind == "int":
                try:
                    return int(v.strip())
                except (TypeError, ValueError):
                    return None
            if kind == "real":
                try:
                    return float(v.strip())
                except (TypeError, ValueError):
                    return None
            return v
        if kind in ("int", "real"):
            return None
        return str(v)[:2000]
    except Exception:  # noqa: BLE001 - the guard itself must never raise
        try:
            return str(v)[:2000]
        except Exception:  # noqa: BLE001
            return None


def bind_row(values: Iterable[Any], kinds: Iterable[str]) -> tuple:
    return tuple(bind(v, kind=k) for v, k in zip(values, kinds, strict=False))


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def _configure(conn: sqlite3.Connection, *, read_only: bool) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    if read_only:
        # Autocommit, and it is a correctness requirement rather than a tuning
        # knob.  Legacy transaction control issues an implicit BEGIN before any
        # INSERT/UPDATE/DELETE - *including* writes to a TEMP table, which a
        # `mode=ro` connection is perfectly entitled to make.  Python never
        # commits that implicit transaction, so it pins a WAL read snapshot and
        # every later SELECT on this thread-local connection keeps serving
        # pre-mutation data until the process restarts.  That is exactly how
        # `/outputs` page.total and `/system/stats` froze after a cleanup.
        # Autocommit makes the leak impossible instead of asking every caller to
        # remember to commit a temp-table write.
        conn.isolation_level = None
    cur = conn.cursor()
    for p in _PRAGMAS:
        if read_only and ("journal_mode" in p or "synchronous" in p):
            continue
        with suppress(sqlite3.DatabaseError):
            cur.execute(p)
    cur.close()
    return conn


def _ro_uri(p: Path) -> str:
    s = str(p).replace("\\", "/")
    s = s.replace("?", "%3f").replace("#", "%23")
    return "file:" + s + "?mode=ro"


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    """Fresh connection.  Callers own it and must close it."""
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    if read_only and _db_path.exists():
        # ``check_same_thread=False`` is *not* an invitation to share a reader.
        # ``get_ro`` keys these by thread, so no connection is ever handed to two
        # threads.  The flag exists so :func:`close_all_connections` can close a
        # reader whose owning thread has already exited - the guard would
        # otherwise refuse, and a reader nobody can close is exactly what keeps a
        # WAL read mark alive and stops the log being recycled.
        conn = sqlite3.connect(_ro_uri(_db_path), uri=True, timeout=5.0,
                               check_same_thread=False)
    else:
        conn = sqlite3.connect(str(_db_path), timeout=15.0)
    return _configure(conn, read_only=read_only)


def get_ro() -> sqlite3.Connection:
    """Thread-local read-only connection.  WAL means it never blocks on writes.

    A reader that is still inside a transaction is holding a stale snapshot, so
    it is rolled back on acquisition.  Combined with the autocommit setting in
    ``_configure`` this is belt and braces: no transaction opened by any caller
    can outlive the request that opened it, and a reader can therefore never
    serve data older than the last committed write.

    Rollback-on-acquire only fires when the *same* thread comes back, so it can
    do nothing for a connection on a thread that never runs another query.  That
    is why every reader is also registered in ``_readers``: it makes the set of
    live readers enumerable, so shutdown and the checkpoint path can close them
    instead of hoping their owners return.
    """
    key = "ro_" + os.path.normcase(str(_db_path))
    conn = _local.__dict__.get(key)
    if conn is None:
        conn = connect(read_only=True)
        _local.__dict__[key] = conn
        with _readers_lock:
            _readers[id(conn)] = (conn, threading.current_thread(), key)
    elif conn.in_transaction:
        with suppress(sqlite3.Error):
            conn.rollback()
    return conn


def release_read_snapshot() -> None:
    """End this thread's read snapshot, releasing its WAL read mark.

    Closing is the only way to guarantee it.  ``rollback()`` is a no-op on an
    autocommit connection, and a statement that was stepped but never reset
    holds its read transaction open while ``sqlite3_get_autocommit`` still
    reports autocommit - so ``in_transaction`` cannot even see that state, let
    alone clear it.  Closing finalises every statement on the connection.

    Reconnecting costs microseconds and the next :func:`get_ro` does it lazily,
    so this is cheap enough to run at the end of any long-lived unit of work.
    """
    for k in [k for k in list(_local.__dict__) if k.startswith("ro_")]:
        conn = _local.__dict__.pop(k, None)
        if conn is None:
            continue
        with _readers_lock:
            _readers.pop(id(conn), None)
        with suppress(sqlite3.Error):
            conn.close()


def close_all_connections() -> int:
    """Close every registered reader, on every thread.  Returns how many.

    ``close_thread_connections`` only ever reached the caller's own connection,
    so at shutdown the readers belonging to request-pool threads, scan-executor
    threads and job workers stayed open.  That matters beyond tidiness: SQLite
    checkpoints and *deletes* the ``-wal`` when the last connection to the
    database closes, and with strays still open the writer's close was never the
    last one.  The log therefore survived process exit at whatever size it had
    reached.
    """
    with _readers_lock:
        entries = list(_readers.values())
        _readers.clear()
    closed = 0
    for conn, _thread, _key in entries:
        with suppress(sqlite3.Error):
            conn.close()
            closed += 1
    for k in [k for k in list(_local.__dict__) if k.startswith("ro_")]:
        conn = _local.__dict__.pop(k, None)
        if conn is not None:
            with suppress(sqlite3.Error):
                conn.close()
    return closed


def reap_dead_readers() -> int:
    """Close readers whose owning thread has exited.  Returns how many."""
    dead = []
    with _readers_lock:
        for key, (conn, thread, _k) in list(_readers.items()):
            if not thread.is_alive():
                dead.append(conn)
                _readers.pop(key, None)
    for conn in dead:
        with suppress(sqlite3.Error):
            conn.close()
    return len(dead)


def reader_count() -> int:
    with _readers_lock:
        return len(_readers)


def data_version(conn: sqlite3.Connection | None = None) -> int:
    """``PRAGMA data_version`` - bumped whenever *another* connection commits.

    Every write in this app goes through the single writer thread on its own
    connection, so this one integer changes on every committed mutation from
    every path: rename, move, trash, restore, permanent delete, tag and album
    edits, and scan completion alike.  Derived caches key on it instead of
    asking each mutation site to remember to invalidate them - the same reason
    ``search/sync.write_synced`` reindexes inside the write rather than trusting
    callers to do it afterwards.
    """
    conn = conn or get_ro()
    try:
        row = conn.execute("PRAGMA data_version").fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0]) if row else 0


@contextmanager
def ro_conn() -> Iterator[sqlite3.Connection]:
    yield get_ro()


def close_thread_connections() -> None:
    release_read_snapshot()


# ---------------------------------------------------------------------------
# Writer thread
# ---------------------------------------------------------------------------

class Future:
    __slots__ = ("_event", "_exc", "_value")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._value: Any = None
        self._exc: BaseException | None = None

    def set_result(self, v: Any) -> None:
        self._value = v
        self._event.set()

    def set_exception(self, e: BaseException) -> None:
        self._exc = e
        self._event.set()

    def result(self, timeout: float | None = None) -> Any:
        if not self._event.wait(timeout):
            raise TimeoutError("write queue timeout")
        if self._exc is not None:
            raise self._exc
        return self._value

    def done(self) -> bool:
        return self._event.is_set()


class WriteQueue:
    """The only thread that writes to SQLite.  Serialized writes = no SQLITE_BUSY."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._conn: sqlite3.Connection | None = None
        self._thread = threading.Thread(target=self._run, name="vault-writer", daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[sqlite3.Connection], Any]) -> Future:
        fut = Future()
        self._q.put((fn, fut))
        return fut

    def run(self, fn: Callable[[sqlite3.Connection], Any], timeout: float = 300.0) -> Any:
        return self.submit(fn).result(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._q.put((None, None))
        self._thread.join(timeout)

    def _run(self) -> None:
        self._conn = connect()
        try:
            while True:
                try:
                    fn, fut = self._q.get(timeout=0.5)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                if fn is None:
                    break
                try:
                    result = fn(self._conn)
                except BaseException as exc:  # noqa: BLE001 - reported to the caller
                    if self._conn.in_transaction:
                        with suppress(sqlite3.Error):
                            self._conn.rollback()
                    if fut is not None:
                        fut.set_exception(exc)
                else:
                    if fut is not None:
                        fut.set_result(result)
        finally:
            if self._conn is not None:
                with suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None


def writer() -> WriteQueue:
    global _writer
    with _writer_lock:
        if _writer is None:
            _writer = WriteQueue()
        return _writer


def shutdown_writer() -> None:
    global _writer
    with _writer_lock:
        if _writer is not None:
            _writer.stop()
            _writer = None


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    cur.close()
    return None if row is None else row[0]


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    cur = conn.execute(sql, params)
    out = cur.fetchall()
    cur.close()
    return out


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    cur = conn.execute(sql, params)
    out = cur.fetchone()
    cur.close()
    return out


def db_stat() -> dict:
    p = _db_path
    wal = Path(str(p) + "-wal")
    return {
        "path": str(p),
        "exists": p.exists(),
        "size_bytes": p.stat().st_size if p.exists() else 0,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
    }


# ---------------------------------------------------------------------------
# WAL checkpointing
# ---------------------------------------------------------------------------

#: Spelled out as whole literal statements rather than an f-string over the mode.
#: ``PRAGMA`` arguments cannot be bound, so the only way for this to be provably
#: injection-free is for no caller value to reach the SQL text at all.
_CHECKPOINT_SQL = {
    "PASSIVE": "PRAGMA wal_checkpoint(PASSIVE)",
    "FULL": "PRAGMA wal_checkpoint(FULL)",
    "RESTART": "PRAGMA wal_checkpoint(RESTART)",
    "TRUNCATE": "PRAGMA wal_checkpoint(TRUNCATE)",
}


def wal_bytes() -> int:
    wal = Path(str(_db_path) + "-wal")
    try:
        return wal.stat().st_size if wal.exists() else 0
    except OSError:
        return 0


def checkpoint(mode: str = "TRUNCATE", *, reap: bool = True) -> dict:
    """``PRAGMA wal_checkpoint(mode)`` on the writer connection.

    SQLite's own automatic checkpoint is PASSIVE, and PASSIVE never shrinks the
    file - it only rewinds the write cursor.  Nothing else in this app asked for
    a checkpoint at all, so the ``-wal`` could only ever grow: it was measured at
    2.03 GB (492,123 frames of 4 KB) against a 35.6 MB, 8,688-page database.

    A checkpoint can also only copy frames older than the *oldest open read
    snapshot*.  When a reader is pinned near the start of the log, every
    checkpoint returns ``busy`` having reclaimed almost nothing - the measured
    case was 34 of 492,123 frames - and the log grows without limit however
    often it is asked.  So the readers this process can account for are closed
    first; ``busy`` in the result then means the pin is somewhere this process
    cannot reach (another process holding the same vault open), which is worth
    reporting rather than silently retrying.

    Returns ``{"busy", "log_pages", "checkpointed", "mode", "wal_before",
    "wal_after", "reaped"}``.  Never raises.
    """
    mode = (mode or "TRUNCATE").upper()
    if mode not in _CHECKPOINT_SQL:
        mode = "TRUNCATE"
    statement = _CHECKPOINT_SQL[mode]
    out = {"mode": mode, "busy": 1, "log_pages": -1, "checkpointed": -1,
           "wal_before": wal_bytes(), "wal_after": 0, "reaped": 0, "ok": False}
    if not _db_path.exists():
        out.update({"busy": 0, "log_pages": 0, "checkpointed": 0, "ok": True})
        return out
    if reap:
        out["reaped"] = reap_dead_readers()
    # The caller's own reader would otherwise pin the very snapshot it is asking
    # to reclaim.
    release_read_snapshot()

    def _op(conn: sqlite3.Connection) -> tuple:
        if conn.in_transaction:
            with suppress(sqlite3.Error):
                conn.commit()
        row = conn.execute(statement).fetchone()
        return tuple(row) if row else (1, -1, -1)

    try:
        busy, log_pages, done = writer().run(_op, timeout=60.0)
        out.update({"busy": int(busy), "log_pages": int(log_pages),
                    "checkpointed": int(done), "ok": int(busy) == 0})
    except BaseException as exc:  # noqa: BLE001 - a checkpoint is best effort
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["wal_after"] = wal_bytes()
    return out
