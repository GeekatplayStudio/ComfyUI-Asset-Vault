"""The write-ahead log must not grow without bound.

The owner's `-wal` reached 2.03 GB — 492,123 frames of 4 KB — against a 35.6 MB,
8,688-page database, and `PRAGMA wal_checkpoint(TRUNCATE)` answered
`(busy=1, log_pages=492123, checkpointed=34)`.

Two independent things have to be true for that to happen, and both are asserted
here:

1. **No reader may pin an old snapshot.**  A checkpoint can only copy frames
   older than the oldest open read transaction, so one stuck reader caps every
   checkpoint forever — the measured 34 of 492,123 frames.  The wal-index made
   it explicit: `aReadMark[2] == 34 == nBackfill`.
2. **Something has to actually truncate.**  SQLite's automatic checkpoint is
   PASSIVE, and PASSIVE never shrinks the file; it only rewinds the write
   cursor.  Nothing in the app asked for a checkpoint at all, so the file only
   ever grew.

`_read_marks` reads the wal-index (`-shm`) directly rather than inferring state
from a checkpoint's return value, because a checkpoint *changes* the thing it
reports on: it resets the read marks it manages to acquire.
"""

from __future__ import annotations

import sqlite3
import struct
import threading
import time
from pathlib import Path

import pytest

from app.core import db as dbmod
from app.indexing.service import get_indexer

TIMEOUT_S = 180

#: Offsets into the wal-index header (SQLite `wal.c`): two 48-byte `WalIndexHdr`
#: copies, then `WalCkptInfo` = nBackfill, aReadMark[5], aLock[8],
#: nBackfillAttempted.
_MXFRAME_OFF = 16
_CKPT_OFF = 96
_READMARK_NOT_USED = 0xFFFFFFFF


def _read_marks(db: Path) -> dict | None:
    shm = Path(str(db) + "-shm")
    if not shm.exists():
        return None
    raw = shm.read_bytes()[:160]
    if len(raw) < 160:
        return None
    return {
        "mx_frame": struct.unpack_from("<I", raw, _MXFRAME_OFF)[0],
        "backfill": struct.unpack_from("<I", raw, _CKPT_OFF)[0],
        "read_marks": list(struct.unpack_from("<5I", raw, _CKPT_OFF + 4)),
    }


def _pinned_marks(db: Path) -> list[int]:
    """Read marks that are set to a real frame, i.e. a snapshot someone may hold."""
    info = _read_marks(db)
    if info is None:
        return []
    return [m for m in info["read_marks"][1:]
            if m != _READMARK_NOT_USED and m != 0]


def _wal_bytes(db: Path) -> int:
    wal = Path(str(db) + "-wal")
    return wal.stat().st_size if wal.exists() else 0


def _external_checkpoint(db: Path, mode: str = "TRUNCATE") -> tuple[int, int, int]:
    """Checkpoint from a connection this app knows nothing about.

    Asking the app's own writer would prove less: the point is that an outside
    observer sees a log that can be fully reclaimed.
    """
    conn = sqlite3.connect(str(db), timeout=20.0)
    try:
        conn.execute("PRAGMA busy_timeout = 15000")
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        return (int(row[0]), int(row[1]), int(row[2]))
    finally:
        conn.close()


def _scan(mode: str = "full") -> None:
    indexer = get_indexer()
    indexer.start(mode=mode, trigger="test")
    deadline = time.monotonic() + TIMEOUT_S
    while indexer.running():
        if time.monotonic() > deadline:
            indexer.cancel()
            pytest.fail(f"{mode} scan exceeded {TIMEOUT_S}s")
        time.sleep(0.01)


# ---------------------------------------------------------------------------


def test_a_checkpoint_on_an_idle_app_is_not_busy_and_reclaims_the_whole_log(
        temp_vault, hermetic_client):
    """The headline gate: `(busy=1, ..., checkpointed=34)` must never recur.

    The app is exercised the way it is used — a scan, then the read paths that
    serve the UI — and then left idle.  At that point every frame in the log
    must be reclaimable by an outsider, with nothing pinning it.
    """
    db = dbmod.db_path()

    _scan("full")
    for path in ("/api/v1/system/stats", "/api/v1/models?limit=50",
                 "/api/v1/outputs?limit=50", "/api/v1/workflows?limit=50",
                 "/api/v1/search?q=probe&limit=20", "/api/v1/albums",
                 "/api/v1/tags", "/api/v1/index/jobs"):
        hermetic_client.get(path)
    _scan("incremental")

    busy, log_pages, checkpointed = _external_checkpoint(db)

    assert busy == 0, (
        f"wal_checkpoint(TRUNCATE) was busy on an idle app "
        f"({checkpointed} of {log_pages} pages reclaimed) — a reader is pinning "
        f"the log; read marks {_pinned_marks(db)}")
    assert checkpointed == log_pages, (
        f"only {checkpointed} of {log_pages} pages were reclaimed; "
        f"read marks {_pinned_marks(db)}")
    assert not _pinned_marks(db), (
        f"a read snapshot is still pinned after a full checkpoint: "
        f"{_read_marks(db)}")


def test_the_scan_truncates_the_log_it_wrote(temp_vault):
    """A scan is the only bulk writer, so it is where the log has to be reclaimed.

    Without this the file only ever grows: SQLite's own checkpoint is PASSIVE
    and rewinds the write cursor without shrinking the file, so the `-wal`
    stays at its high-water mark for the life of the process.
    """
    db = dbmod.db_path()

    _scan("full")
    after_full = _wal_bytes(db)
    assert after_full == 0, (
        f"the -wal is {after_full} bytes after a full scan; the scan must "
        "checkpoint(TRUNCATE) when it finishes")

    _scan("incremental")
    assert _wal_bytes(db) == 0, "an incremental scan left the -wal behind"


def test_repeated_scans_do_not_grow_the_log(temp_vault):
    """Unbounded growth is a trend, not a single measurement, so measure the trend."""
    db = dbmod.db_path()
    sizes = []
    for _ in range(4):
        _scan("full")
        sizes.append(_wal_bytes(db))
    assert max(sizes) == 0, f"the -wal grew across repeated scans: {sizes}"


def test_a_reader_on_a_dead_thread_cannot_pin_the_log(temp_vault):
    """The failure mode the rollback-on-acquire guard cannot reach.

    `get_ro` rolls a stale transaction back when the *same thread* asks for the
    connection again.  A worker thread that reads once and exits never asks
    again, so its connection — and its read mark — would otherwise outlive it.
    """
    db = dbmod.db_path()
    _scan("full")

    started = threading.Event()

    def read_and_die() -> None:
        conn = dbmod.get_ro()
        conn.execute("SELECT COUNT(*) FROM model_files").fetchone()
        # Leave a half-consumed cursor alive on purpose: a statement that was
        # stepped but never reset holds its read transaction open, and
        # `in_transaction` stays False throughout, so the existing guard is
        # blind to it.
        cur = conn.execute("SELECT id FROM model_files")
        cur.fetchone()
        started.set()

    t = threading.Thread(target=read_and_die, name="reader-that-exits")
    t.start()
    assert started.wait(30), "the reader thread never ran"
    t.join(timeout=30)
    assert not t.is_alive()

    reaped = dbmod.reap_dead_readers()
    assert reaped >= 0  # the connection may already have been collected

    busy, log_pages, checkpointed = _external_checkpoint(db)
    assert busy == 0 and checkpointed == log_pages, (
        f"a reader from an exited thread pinned the log: busy={busy}, "
        f"{checkpointed} of {log_pages} pages, marks {_pinned_marks(db)}")


def test_shutdown_closes_every_reader_and_removes_the_log(temp_vault):
    """SQLite deletes the -wal when the *last* connection closes.

    Closing only the calling thread's readers meant the writer was never the
    last one out, so the log survived process exit at whatever size it had
    reached.
    """
    db = dbmod.db_path()
    _scan("full")

    done = threading.Event()

    def read_on_another_thread() -> None:
        dbmod.get_ro().execute("SELECT COUNT(*) FROM outputs").fetchone()
        done.set()
        time.sleep(0.2)

    t = threading.Thread(target=read_on_another_thread, name="reader-elsewhere")
    t.start()
    assert done.wait(30)

    assert dbmod.reader_count() >= 1
    closed = dbmod.close_all_connections()
    assert closed >= 1, "close_all_connections reached no reader"
    assert dbmod.reader_count() == 0

    dbmod.checkpoint("TRUNCATE", reap=False)
    dbmod.shutdown_writer()
    t.join(timeout=5)

    assert _wal_bytes(db) == 0, "the -wal outlived the last connection"


def test_checkpoint_reports_a_pin_instead_of_hiding_it(temp_vault):
    """A busy checkpoint is information, not an error to swallow.

    A second connection holding an open read transaction is exactly the
    production case (another process with the vault open), and the result must
    say so rather than reporting success.
    """
    db = dbmod.db_path()
    _scan("full")

    pin = sqlite3.connect(str(db), timeout=10.0)
    try:
        pin.execute("BEGIN")
        pin.execute("SELECT COUNT(*) FROM model_files").fetchone()

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO config(key,value,value_type,updated_at) VALUES "
                "('wal_probe','1','str',?) ON CONFLICT(key) DO UPDATE SET value='2'",
                (dbmod.now_ms(),))
            conn.commit()

        dbmod.writer().run(_write)

        result = dbmod.checkpoint("TRUNCATE")
        assert result["busy"] == 1, (
            f"a pinned reader was not reported: {result}")
        assert result["ok"] is False
    finally:
        pin.close()

    # With the pin gone the very next checkpoint must succeed and reclaim it all.
    result = dbmod.checkpoint("TRUNCATE")
    assert result["busy"] == 0 and result["ok"] is True, result
    assert _wal_bytes(db) == 0
