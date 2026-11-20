"""2.5.1 - a clean stop, and a start that says what it is doing.

The field failure: ``stop_app.bat`` ended the engine with ``taskkill /F``, so the
shutdown checkpoint never ran and the 2.5 upgrade left a 1.3 GB write-ahead log
behind.  The next start folded it silently inside its first write - 15 minutes on
a hard disk - while the launcher gave up after 45 seconds and called a working
engine failed.  Three rails, one test group each:

* the engine can be asked to stop cleanly (and only by a request that carries
  the CSRF header);
* a large leftover log is folded at startup, out loud, and truncated;
* the launchers wait for a live engine instead of a fixed 45 seconds, and the
  stop scripts ask before they force.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import core  # noqa: E402
from app.core import config_service  # noqa: E402
from app.core import db as dbmod  # noqa: E402
from app.core.migrations import migrate  # noqa: E402

# ---------------------------------------------------------------------------
# The shutdown endpoint
# ---------------------------------------------------------------------------


def test_shutdown_refuses_a_request_without_the_csrf_header(hermetic_client,
                                                            no_real_engine_exit):
    before = len(no_real_engine_exit)
    r = hermetic_client.post("/api/v1/system/shutdown")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CSRF_HEADER_MISSING"
    time.sleep(0.6)
    assert len(no_real_engine_exit) == before, "a refused request must not stop anything"


def test_shutdown_answers_first_then_asks_for_a_graceful_exit(hermetic_client,
                                                              no_real_engine_exit):
    before = len(no_real_engine_exit)
    r = hermetic_client.post("/api/v1/system/shutdown", headers={"X-Vault-Request": "1"})
    assert r.status_code == 202
    assert r.json()["stopping"] is True
    deadline = time.monotonic() + 5
    while len(no_real_engine_exit) == before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(no_real_engine_exit) == before + 1, "the exit was never requested"


# ---------------------------------------------------------------------------
# The startup fold
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch_db(tmp_path):
    original = dbmod.db_path()
    dbmod.set_db_path(tmp_path / "fold.db")
    migrate()
    config_service.invalidate()
    yield
    dbmod.shutdown_writer()
    dbmod.close_thread_connections()
    dbmod.set_db_path(original)
    config_service.invalidate()


def _grow_wal() -> None:
    def _op(conn):
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("CREATE TABLE IF NOT EXISTS ballast(b BLOB)")
        for _ in range(8):
            conn.execute("INSERT INTO ballast VALUES (randomblob(262144))")
        conn.commit()
    dbmod.writer().run(_op)


def test_a_large_leftover_log_is_folded_and_truncated(scratch_db, caplog):
    _grow_wal()
    before = dbmod.wal_bytes()
    assert before > 1_000_000
    with caplog.at_level("INFO", logger="app.core"):
        result = core.fold_wal("left by a test", threshold=1)
    assert result is not None and result["ok"], result
    assert result["wal_before"] == before
    assert dbmod.wal_bytes() == 0, "TRUNCATE must leave an empty log"
    text = caplog.text
    assert "folding" in text and "folded in" in text, "the wait must be explained in the log"


def test_a_small_log_is_left_to_sqlite(scratch_db):
    _grow_wal()
    assert core.fold_wal("left by a test", threshold=1 << 40) is None
    assert dbmod.wal_bytes() > 0


def test_startup_folds_before_it_migrates():
    """Order matters: the fold has to come first, or the first migration write
    pays for it silently - which is exactly what made the start look hung."""
    source = (BACKEND / "app" / "core" / "__init__.py").read_text(encoding="utf-8")
    body = source[source.index("def startup("):source.index("def fold_wal(")]
    assert body.index("fold_wal(") < body.index("migrate()")


# ---------------------------------------------------------------------------
# Launchers and stop scripts
# ---------------------------------------------------------------------------


def _text(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("name", ["start_app.bat", "start_app.ps1", "start_app.sh"])
def test_launchers_no_longer_give_up_on_a_live_engine_at_45_seconds(name):
    text = _text(name)
    assert "within 45 seconds" not in text
    assert "1..45" not in text and "(1,1,45)" not in text and "seq 1 45" not in text
    assert "stopped before it was ready" in text, "a dead engine must still be reported"
    assert "still starting" in text, "a slow start must show progress"


@pytest.mark.parametrize("name", ["start_app.bat", "start_app.ps1"])
def test_windows_launchers_wait_for_an_engine_that_is_already_starting(name):
    text = _text(name)
    assert "already starting" in text or "earlier launch is still getting ready" in text
    assert "app.main:app*--port" in text, "the engine is found by its command line"


def test_stop_asks_the_engine_before_it_forces():
    ps1 = _text("stop_app.ps1")
    ask = ps1.index("/api/v1/system/shutdown")
    force = ps1.index("Stop-Process")
    assert ask < force, "stop_app.ps1 must ask for a clean shutdown before forcing"
    assert "X-Vault-Request" in ps1
    bat = _text("stop_app.bat")
    assert bat.index("stop_app.ps1") < bat.index("taskkill"), \
        "stop_app.bat must use the clean stop; taskkill is only the fallback"


def test_batch_files_keep_crlf_line_endings():
    """cmd.exe misreads labels and goto in LF batch files."""
    for name in ("start_app.bat", "stop_app.bat"):
        data = (REPO / name).read_bytes()
        assert data.replace(b"\r\n", b"").count(b"\n") == 0, f"{name} has bare LF lines"
