"""B5 — ``start_app.bat`` must actually start the backend.

The audit's finding was blunt: the launcher ran

    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --cwd backend

and uvicorn has no ``--cwd`` option, so the process died on the command line
before it ever imported the app.  ``backend_log.txt`` in the repo root still
holds the captured proof::

    Error: No such option '--cwd'. (Did you mean one of: '--fd', '--uds', '--ws'?)

Two layers are asserted here.  The **static** tests read the batch file and are
the ones that would have caught B5 on day one — they need nothing installed.
The **live** test actually launches it and waits for ``/api/v1/ping``.

The correct invocation is ``--app-dir backend`` (BUILD_PLAN 9) on port 8127 (D1).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from c6 import bare_token_hits

REPO = Path(__file__).resolve().parent.parent.parent.parent
COMSPEC = os.environ.get("COMSPEC") or shutil.which("cmd") or "cmd.exe"
START_BAT = REPO / "start_app.bat"
PING_TIMEOUT_S = 30


def launcher_text() -> str:
    if not START_BAT.exists():
        pytest.fail(f"{START_BAT} does not exist; there is no way to start the app")
    return START_BAT.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Static — no install required
# ---------------------------------------------------------------------------

def test_the_launcher_exists():
    assert START_BAT.exists(), f"{START_BAT} is missing"


def test_the_launcher_does_not_pass_the_nonexistent_cwd_option():
    """The literal B5 defect.  ``uvicorn --cwd`` is not an option and never was."""
    text = launcher_text()
    assert "--cwd" not in text, (
        "start_app.bat still passes --cwd to uvicorn; the backend cannot start. "
        "The correct flag is --app-dir. Owner: docs agent (BUILD_PLAN 9).")


def test_the_launcher_uses_app_dir():
    text = launcher_text()
    if "uvicorn" not in text:
        pytest.skip("launcher does not invoke uvicorn directly")
    assert "--app-dir" in text, (
        "uvicorn is invoked without --app-dir, so app.main will not be importable "
        "from the repo root. Owner: docs agent.")


def _expand_batch_vars(text: str) -> str:
    """Resolve ``set "NAME=value"`` assignments so %NAME% reads as its literal.

    The launcher pins its port once in a variable and reuses it, which is the
    right way to write a batch file; this lets the assertions below still see
    the concrete value.
    """
    pattern = r'(?im)^[ \t]*set[ \t]+"?([A-Za-z_]\w*)=([^"\r\n]*)"?[ \t]*$'
    for name, value in re.findall(pattern, text):
        text = text.replace("%" + name + "%", value)
    return text


def test_the_launcher_uses_the_agreed_port():
    """D1: 8000 was retired because it collides constantly."""
    text = _expand_batch_vars(launcher_text())
    ports = set(re.findall(r"--port\s+(\d+)", text))
    assert ports, "the launcher does not pin a port"
    assert ports == {"8127"}, (
        f"launcher starts the backend on {sorted(ports)}; D1 fixed the port at 8127. "
        "Owner: docs agent.")


def test_the_launcher_does_not_reference_retired_names():
    """No stale v0 API path, old DB filename, or old port anywhere."""
    text = launcher_text()
    stale = [n for n in ("asset_vault.db", "localhost:3000", ":8000") if n in text]
    assert not stale, f"launcher still references retired names: {stale}. Owner: docs agent."


def test_the_launcher_fails_loudly_when_the_venv_is_missing():
    text = launcher_text().lower()
    assert "venv" in text, "the launcher must check for the virtual environment"


def test_the_launcher_waits_for_the_backend_before_opening_a_browser():
    """Opening the UI before the API answers shows the user an error page."""
    text = launcher_text().lower()
    opens_browser = "start http" in text or "explorer http" in text
    if not opens_browser:
        pytest.skip("launcher does not open a browser")
    waits = any(tok in text for tok in ("ping", "timeout", "curl", "waitfor", ":waitloop"))
    assert waits, (
        "the launcher opens a browser without waiting for the backend to listen. "
        "Owner: docs agent (BUILD_PLAN 9 requires a wait-for-listening loop).")


def test_no_ai_tool_attribution_in_the_launcher():
    """C6 — the product is authored by Geekatplay, full stop."""
    hits = bare_token_hits(launcher_text())
    assert not hits, f"C6 violation: the launcher mentions {hits}"


# ---------------------------------------------------------------------------
# Live — actually run it
# ---------------------------------------------------------------------------

def port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


@pytest.mark.live
@pytest.mark.slow
def test_the_launcher_starts_a_backend_that_answers_ping(tmp_path):
    """The end-to-end B5 gate: run the launcher, then call ``/api/v1/ping``.

    Skipped when something already holds 8127, because the assertion would then
    be satisfied by the wrong process.
    """
    import httpx

    if port_is_open(8127):
        pytest.skip("port 8127 is already in use; cannot attribute a ping to the launcher")
    if not (REPO / "venv" / "Scripts" / "python.exe").exists():
        pytest.skip("no venv in the repo; the launcher would install one")

    log = tmp_path / "launcher.log"
    proc = subprocess.Popen(  # noqa: S603
        [COMSPEC, "/c", str(START_BAT)],
        cwd=str(REPO), stdout=log.open("wb"), stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        deadline = time.monotonic() + PING_TIMEOUT_S
        answered = False
        while time.monotonic() < deadline:
            try:
                r = httpx.get("http://127.0.0.1:8127/api/v1/ping", timeout=1.0)
                if r.status_code == 200 and r.json().get("pong"):
                    answered = True
                    break
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.5)
        tail = log.read_text(encoding="utf-8", errors="replace")[-2000:]
        assert answered, (
            f"start_app.bat did not bring up a backend within {PING_TIMEOUT_S}s.\n"
            f"--- launcher output ---\n{tail}")
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        _kill_port(8127)


def _kill_port(port: int) -> None:
    """Leave no orphaned server behind, whatever happened above."""
    powershell = shutil.which("powershell")
    if not powershell:
        return
    subprocess.run(  # noqa: S603
        [powershell, "-NoProfile", "-Command",
         f"Get-NetTCPConnection -LocalPort {port} -State Listen -EA SilentlyContinue | "
         "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue }"],
        capture_output=True, timeout=20, check=False)
