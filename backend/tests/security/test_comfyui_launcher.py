"""The ComfyUI launcher (REQUIREMENTS_R2 C8, "Open in ComfyUI").

The second place this application starts a process on purpose, and the one the
2026-08-22 review left unexamined (`SECURITY_REVIEW` §8).  It is a harder
surface than the updater: the updater runs one of three *fixed* filenames under
a fixed ``update\\`` subdirectory, while the launcher runs whatever matches
``run_*.bat`` beside the ComfyUI folder - and on a portable build that folder is
a drive root.

Six conditions are asserted here, each by running the code rather than reading
it:

* only a *verified* ComfyUI install may nominate an executable (S-20),
* only a script that actually starts ComfyUI is offered at all (S-21),
* the argv is inert - including the part ``cmd.exe`` re-parses (S-19),
* the resolved path must be confirmed verbatim, and a confirmation can only
  ever loosen the *spelling*, never redirect the spawn,
* the copy into the ComfyUI install is separately consented, contained, and
  never overwrites,
* a launch that fails, times out, or cannot be watched ends and says so.

**Nothing in this file starts ComfyUI, and nothing in it runs a batch file.**
Every hostile case stages a launcher that is genuinely capable - it writes a
marker file, and its payload sits on ``PATH`` - and then asserts both that
``subprocess.Popen`` was never reached and that the marker does not exist.  The
one benign case replaces ``Popen`` with a recorder.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core import config_service
from app.core import db as dbmod
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.services import comfyui_service as cs

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="the launcher is Windows-only")

GRAPH = '{"nodes": [], "links": []}'

#: Content every real portable launcher has: it names ComfyUI's entry point.
REAL_LAUNCHER = (".\\python_embeded\\python.exe -s ComfyUI\\main.py "
                 "--windows-standalone-build --port 8189\r\npause\r\n")


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _install(base: Path, *, verified: bool = True) -> Path:
    """A portable layout: ComfyUI, python_embeded, launchers beside them."""
    root = base / "ComfyUI"
    (root / "models" / "checkpoints").mkdir(parents=True, exist_ok=True)
    (root / "custom_nodes").mkdir(exist_ok=True)
    (root / "user" / "default" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "workflows").mkdir(exist_ok=True)
    (root / "main.py").write_text("# ComfyUI\n", encoding="utf-8")
    if verified:
        (root / "comfyui_version.py").write_text('__version__ = "0.33.0"\n',
                                                 encoding="utf-8")
    site = base / "python_embeded" / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (base / "python_embeded" / "python.exe").write_bytes(b"MZ")
    return root


def _launcher(base: Path, name: str, marker: Path, *, real: bool = True) -> Path:
    """A launcher that *would* leave proof if it ever ran."""
    body = "@echo off\r\n"
    if real:
        body += ".\\python_embeded\\python.exe -s ComfyUI\\main.py\r\n"
    body += f'echo ran> "{marker}"\r\n'
    path = base / name
    path.write_text(body, encoding="utf-8")
    return path


def _payload(tmp_path: Path, marker: Path, monkeypatch) -> Path:
    """A command reachable by name, so an injected token would really land."""
    folder = tmp_path / "PayloadOnPath"
    folder.mkdir(exist_ok=True)
    (folder / "injected.bat").write_text(
        f'@echo off\r\necho injected> "{marker}"\r\n', encoding="utf-8")
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    return folder


#: Captured before ``no_spawn`` replaces it, so the fixture's tripwire stays a
#: tripwire: it must fire for the *application* starting a process, never for a
#: test creating its own fixture on disk.
_REAL_POPEN = subprocess.Popen


def _junction(link: Path, target: Path) -> bool:
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    proc = _REAL_POPEN(
        [comspec, "/c", "mklink", "/J", str(link), str(target)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.wait(timeout=30) == 0


def _use(client, root: Path) -> None:
    response = client.patch("/api/v1/system/config",
                            json={"comfyui_path": str(root)})
    assert response.status_code == 200, response.text


def _workflow(root: Path, rel: str) -> int:
    path = root / rel.replace("/", "\\")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GRAPH, encoding="utf-8")
    now = dbmod.now_ms()

    def _op(conn):
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO workflows(root_id, abs_path, path_key, rel_path, folder, "
            "name, size, mtime_ns, fingerprint, created_at, updated_at) "
            "VALUES (NULL,?,?,?,?,?,?,?,?,?,?)",
            (str(path), str(path).lower(), rel.replace("/", "\\"),
             str(Path(rel).parent).replace("/", "\\"), path.stem, len(GRAPH),
             now * 1_000_000, "fp:" + rel, now, now))
        conn.commit()
        return cur.lastrowid

    return int(dbmod.writer().run(_op))


@pytest.fixture
def no_spawn(monkeypatch):
    """Records any attempt to start a process; the suite must record none."""
    calls: list = []

    def _refuse(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError(f"a process was started: {args!r}")

    monkeypatch.setattr(cs.subprocess, "Popen", _refuse)
    return calls


@pytest.fixture
def not_running(monkeypatch):
    """The machine running the suite may have the real ComfyUI up."""
    monkeypatch.setattr(cs, "is_running", lambda: {
        "running": False, "ports": [], "method": "test",
        "confidence": "inferred", "note": "nothing is listening"})


@pytest.fixture
def idle_launch_state():
    cs._launch_state.clear()
    cs._launch_state.update({"status": "idle"})
    yield
    cs._launch_state.clear()
    cs._launch_state.update({"status": "idle"})


@pytest.fixture
def portable(client, tmp_path, not_running, idle_launch_state):
    """A verified portable install with two ordinary launchers."""
    base = tmp_path / "Portable"
    root = _install(base)
    (base / "run_cpu.bat").write_text(REAL_LAUNCHER, encoding="utf-8")
    (base / "run_nvidia_gpu.bat").write_text(REAL_LAUNCHER, encoding="utf-8")
    _use(client, root)
    return {"client": client, "base": base, "root": root}


# ---------------------------------------------------------------------------
# S-20 - only a verified install may nominate an executable
# ---------------------------------------------------------------------------

# S-20 fixed: launcher discovery requires comfyui_version.py + main.py +
# models/, the same proof S-04 imposed on the updater.  A failure here means a
# staged directory the config endpoint accepted can once again get its own
# batch file offered - and run - as "the way to start ComfyUI".
def test_the_launcher_must_live_under_a_verified_comfyui_install(
        client, tmp_path, not_running, idle_launch_state, no_spawn, monkeypatch):
    marker = tmp_path / "STAGED_RAN.txt"
    staging = tmp_path / "AttackerStaging"
    root = _install(staging, verified=False)      # models/ + main.py only
    staged = _launcher(staging, "run_staged.bat", marker)
    _payload(tmp_path, marker, monkeypatch)
    _use(client, root)

    info = client.get("/api/v1/comfyui/info").json()
    assert info["launchers"] == [], (
        "an unverified directory nominated its own executable")
    assert info["recommended_launcher"] is None

    with pytest.raises(NotFoundError) as excinfo:
        cs.resolve_launcher()
    assert excinfo.value.details["missing"] == ["comfyui_version.py"]

    with pytest.raises(NotFoundError):
        cs.start_comfyui(None, confirm_path=str(staged))

    wid = _workflow(root, "workflows/flow.json")
    response = client.post("/api/v1/comfyui/open-workflow",
                           json={"uid": f"workflow:{wid}", "start": True,
                                 "confirm_launcher_path": str(staged)})
    assert response.status_code == 404, response.text
    assert no_spawn == []
    assert not marker.exists(), "the staged launcher ran"


def test_the_plan_says_why_an_unverified_install_offers_no_launcher(
        client, tmp_path, not_running, idle_launch_state, no_spawn):
    staging = tmp_path / "Staging"
    root = _install(staging, verified=False)
    _launcher(staging, "run_staged.bat", tmp_path / "never.txt")
    _use(client, root)
    wid = _workflow(root, "workflows/flow.json")

    plan = client.get("/api/v1/comfyui/open-workflow/plan",
                      params={"uid": f"workflow:{wid}"}).json()
    assert plan["launcher"] is None
    assert plan["launcher_confirm_path"] is None
    assert plan["launcher_alternatives"] == []
    assert plan["can_open"] is False
    assert plan["blocked_reason"] == "no_launcher_found"
    assert plan["launcher_error"]


def test_the_updater_and_the_launcher_use_one_definition_of_a_real_install():
    from app.api.v1 import comfyui_router

    assert comfyui_router.INSTALL_PROOF is cs.INSTALL_PROOF
    assert set(cs.INSTALL_PROOF) == {"comfyui_version.py", "main.py"}


# ---------------------------------------------------------------------------
# S-21 - a file is not a launcher just because it matches the glob
# ---------------------------------------------------------------------------

# S-21 fixed: `run_*.bat` is globbed in the *parent* of the ComfyUI folder,
# which on the owner's portable build is a drive root.  A script that does not
# start ComfyUI is not offered as a way to start ComfyUI.
def test_a_batch_file_that_does_not_start_comfyui_is_never_offered(
        client, tmp_path, not_running, idle_launch_state, no_spawn, monkeypatch):
    marker = tmp_path / "DROPPED_RAN.txt"
    base = tmp_path / "Portable"
    root = _install(base)
    (base / "run_nvidia_gpu.bat").write_text(REAL_LAUNCHER, encoding="utf-8")
    dropped = _launcher(base, "run_setup.bat", marker, real=False)
    _payload(tmp_path, marker, monkeypatch)
    _use(client, root)

    ids = {entry["id"] for entry in cs.probe().launchers}
    assert "run_nvidia_gpu" in ids
    assert "run_setup" not in ids, "a dropped batch file was offered as a launcher"

    with pytest.raises(NotFoundError):
        cs.resolve_launcher("run_setup")
    with pytest.raises(ValidationError):
        cs.start_comfyui(None, confirm_path=str(dropped))
    assert no_spawn == []
    assert not marker.exists()


def test_a_launcher_records_the_evidence_it_was_accepted_on(portable):
    entry = cs.resolve_launcher()
    assert "main.py" in entry["evidence"]
    assert "python_embed" in entry["evidence"]


def test_only_the_parent_folder_is_globbed_not_the_install_tree(
        portable, no_spawn):
    """A node package cannot drop a launcher into the folder it is installed in."""
    root = portable["root"]
    for where in (root, root / "custom_nodes", root / "custom_nodes" / "evil-pack",
                  root / "user" / "default" / "workflows"):
        where.mkdir(parents=True, exist_ok=True)
        (where / "run_evil.bat").write_text(REAL_LAUNCHER, encoding="utf-8")
    ids = {entry["id"] for entry in cs.probe().launchers}
    assert "run_evil" not in ids
    assert no_spawn == []


def test_discovery_is_deterministic_when_several_launchers_match(portable):
    base = portable["base"]
    for name in ("run_aaa.bat", "run_zzz.bat", "run_cpu.bat"):
        (base / name).write_text(REAL_LAUNCHER, encoding="utf-8")
    first = [e["id"] for e in cs.probe().launchers]
    second = [e["id"] for e in cs.probe().launchers]
    assert first == second
    assert first == [*sorted(first[:-1], key=str.lower), first[-1]]
    # The preferred name wins over alphabetical order, so "run_aaa.bat" landing
    # beside the install does not silently become the recommended launcher.
    assert cs.resolve_launcher()["label"] == "run_nvidia_gpu.bat"


def test_a_port_that_is_not_a_port_is_ignored_rather_than_believed(
        client, tmp_path, not_running, idle_launch_state):
    base = tmp_path / "Portable"
    root = _install(base)
    (base / "run_nvidia_gpu.bat").write_text(
        ".\\python_embeded\\python.exe -s ComfyUI\\main.py --port 99999\r\n",
        encoding="utf-8")
    _use(client, root)
    entry = cs.resolve_launcher()
    assert entry["port"] is None, "an unusable port was carried into the launch"
    assert entry["port_source"] is None
    assert cs._port_open(99999) is False       # and it never raises


# ---------------------------------------------------------------------------
# S-19 - "list argv, shell=False" is not the whole story for a .bat on Windows
# ---------------------------------------------------------------------------

# S-19 fixed.  A .bat/.cmd target is never executed directly on Windows:
# CreateProcess runs it through cmd.exe, which re-parses the whole command line,
# and subprocess.list2cmdline quotes a token only when it holds a space.  A
# failure here means a filename can once again smuggle a second command past a
# confirmation dialog that showed one file.
@pytest.mark.parametrize("name", [
    "run_a&injected.bat",
    "run_a&&injected.bat",
    "run_a^injected.bat",
    "run_a(injected).bat",
    "run_%PATH%injected.bat",
])
def test_a_cmd_metacharacter_in_the_launcher_name_starts_nothing(
        client, tmp_path, not_running, idle_launch_state, no_spawn, monkeypatch,
        name):
    marker = tmp_path / "INJECTED.txt"
    base = tmp_path / "Portable"
    root = _install(base)
    hostile = _launcher(base, name, tmp_path / "BENIGN.txt")
    _payload(tmp_path, marker, monkeypatch)
    _use(client, root)

    entry = next(e for e in cs.probe().launchers if e["path"] == str(hostile))
    assert entry["available"] is False
    assert entry["unsafe_reason"] == "cmd_metacharacter_in_path"

    with pytest.raises(ConflictError) as excinfo:
        cs.start_comfyui(entry["id"], confirm_path=str(hostile))
    assert excinfo.value.details["reason"] == "cmd_metacharacter_in_path"
    # ...and it is never what "just start ComfyUI" resolves to, either.
    assert cs.resolve_launcher()["path"] != str(hostile)
    assert no_spawn == []
    assert not marker.exists(), "an injected command ran"


def test_the_rule_is_not_wider_than_the_hazard(client, tmp_path, not_running,
                                               idle_launch_state):
    """``C:\\Program Files (x86)\\...`` must still be launchable.

    ``list2cmdline`` quotes a token that holds a space, and a quoted token is
    inert to everything cmd.exe would otherwise parse - so refusing parentheses
    outright would break a real install for no gain.
    """
    base = tmp_path / "Program Files (x86)"
    root = _install(base)
    (base / "run_nvidia_gpu.bat").write_text(REAL_LAUNCHER, encoding="utf-8")
    _use(client, root)
    entry = cs.resolve_launcher()
    assert entry["available"] is True
    assert cs.cmd_line_hazard(entry["command"]) is None
    assert subprocess.list2cmdline(entry["command"]).startswith('"')


def test_a_real_executable_is_not_subject_to_the_rule(portable):
    """Only ``.bat``/``.cmd`` reach a command interpreter."""
    assert cs.cmd_line_hazard([r"C:\p&q\python.exe", "-s", "main.py"]) is None
    assert cs.cmd_line_hazard([r"C:\p&q\run.bat"]) == "&"
    assert cs.cmd_line_hazard([]) is None
    assert cs.cmd_line_hazard(None) is None


def test_the_updater_is_held_to_the_same_rule(client, tmp_path):
    """The updater's filenames are fixed - the directory above them is not."""
    parent = tmp_path / "Portable&injected"
    root = _install(parent)
    (parent / "update").mkdir()
    (parent / "update" / "update_comfyui.bat").write_text("@echo off\r\n",
                                                          encoding="utf-8")
    _use(client, root)
    entry = next(e for e in cs.probe().updaters if e["id"] == "portable")
    assert entry["available"] is False
    assert entry["unsafe_reason"] == "cmd_metacharacter_in_path"
    response = client.get("/api/v1/comfyui/update/plan")
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# The argv itself
# ---------------------------------------------------------------------------

class _FakeProc:
    pid = 4242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


def test_the_argv_is_the_discovered_path_and_nothing_else(portable, monkeypatch):
    seen: dict = {}

    def _record(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return _FakeProc()

    monkeypatch.setattr(cs.subprocess, "Popen", _record)
    monkeypatch.setattr(cs, "_port_open", lambda port, timeout=0.25: True)

    launcher = cs.resolve_launcher()
    cs.start_comfyui(None, confirm_path=launcher["path"], port=8189)
    _settle()

    assert seen["argv"] == [launcher["path"]]
    assert isinstance(seen["argv"], list) and len(seen["argv"]) == 1
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["cwd"] == str(portable["base"])
    assert seen["kwargs"]["stdin"] == cs.subprocess.DEVNULL
    # The environment is inherited, never assembled: nothing the caller sends
    # can shape what the child sees.
    assert "env" not in seen["kwargs"]


def test_no_request_field_reaches_the_argv(portable, no_spawn):
    """uid, launcher and the confirmation are all matched, never interpolated."""
    client = portable["client"]
    resolved = cs.resolve_launcher()["path"]
    wid = _workflow(portable["root"], "workflows/flow.json")
    for launcher in ("run_nvidia_gpu & calc", "../../evil", "run_nvidia_gpu.bat",
                     "%PATH%", "run_nvidia_gpu\x00"):
        response = client.post(
            "/api/v1/comfyui/open-workflow",
            json={"uid": f"workflow:{wid}", "launcher": launcher, "start": True,
                  "confirm_launcher_path": resolved})
        assert response.status_code in (404, 422), (launcher, response.text)
    assert no_spawn == []


# ---------------------------------------------------------------------------
# Confirmation integrity
# ---------------------------------------------------------------------------

def _settle(timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while cs.launch_status()["status"] == "starting" and time.monotonic() < deadline:
        time.sleep(0.02)
    return cs.launch_status()


@pytest.mark.parametrize("shape", [
    "cmd", "relative", "other_launcher", "tail_amp", "tail_quote", "newline",
    "empty", "parent_dir", "unc",
])
def test_a_confirmation_that_is_not_the_resolved_path_starts_nothing(
        portable, no_spawn, shape):
    resolved = cs.resolve_launcher()["path"]
    base = str(portable["base"])
    confirm = {
        "cmd": r"C:\Windows\System32\cmd.exe",
        "relative": "run_nvidia_gpu.bat",
        "other_launcher": str(portable["base"] / "run_cpu.bat"),
        "tail_amp": resolved + " & calc.exe",
        "tail_quote": resolved + '" "extra-arg',
        "newline": resolved + "\nnet user",
        "empty": "",
        "parent_dir": base,
        "unc": r"\\127.0.0.1\C$\Windows\System32\cmd.exe",
    }[shape]
    with pytest.raises(ValidationError):
        cs.start_comfyui(None, confirm_path=confirm)
    assert no_spawn == []


@pytest.mark.parametrize("spelling", ["upper", "long_prefix", "dot_segment",
                                      "forward_slash", "short_name"])
def test_a_different_spelling_of_the_same_file_still_starts_only_that_file(
        portable, monkeypatch, spelling):
    """A confirmation can loosen the *spelling*; it can never steer the spawn.

    ``normalize`` is ``os.path.realpath``, so a case variant, a ``\\\\?\\``
    prefix, a ``.`` segment, forward slashes and an 8.3 short name all collapse
    onto the one file discovery chose - and the argv is that file either way,
    because it comes from discovery and never from the request.
    """
    resolved = cs.resolve_launcher()["path"]
    if spelling == "short_name":
        buffer = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(resolved, buffer, 1024)
        confirm = buffer.value
        if not confirm or confirm == resolved:
            pytest.skip("8.3 short names are disabled on this volume")
    else:
        confirm = {
            "upper": resolved.upper(),
            "long_prefix": "\\\\?\\" + resolved,
            "dot_segment": str(Path(resolved).parent / "." / Path(resolved).name),
            "forward_slash": resolved.replace("\\", "/"),
        }[spelling]

    seen: dict = {}

    def _record(argv, **kwargs):
        seen["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(cs.subprocess, "Popen", _record)
    monkeypatch.setattr(cs, "_port_open", lambda port, timeout=0.25: True)
    try:
        cs.start_comfyui(None, confirm_path=confirm)
    except ValidationError:
        # Refusing a spelling is also a correct answer - but then nothing at all
        # may have been started.
        assert seen == {}
        return
    _settle()
    assert seen["argv"] == [resolved], (
        "the caller's spelling, not the discovered path, reached the argv")


def test_a_confirmation_that_predates_a_config_change_is_refused(
        client, tmp_path, not_running, idle_launch_state, no_spawn):
    first, second = tmp_path / "A", tmp_path / "B"
    for base in (first, second):
        _install(base)
        (base / "run_nvidia_gpu.bat").write_text(REAL_LAUNCHER, encoding="utf-8")
    _use(client, _install(first))
    stale = cs.resolve_launcher()["path"]
    _use(client, second / "ComfyUI")
    fresh = cs.resolve_launcher()["path"]
    assert stale != fresh
    with pytest.raises(ValidationError):
        cs.start_comfyui(None, confirm_path=stale)
    assert no_spawn == []


def test_a_junction_cannot_make_a_confirmation_name_a_different_file(
        portable, tmp_path, no_spawn, monkeypatch):
    """A junction resolves; it does not redirect what the spawn receives."""
    elsewhere = tmp_path / "Elsewhere"
    elsewhere.mkdir()
    (elsewhere / "run_nvidia_gpu.bat").write_text(
        REAL_LAUNCHER + 'echo hijacked\r\n', encoding="utf-8")
    link = tmp_path / "link"
    if not _junction(link, elsewhere):
        pytest.skip("could not create an NTFS junction here")

    resolved = cs.resolve_launcher()["path"]
    with pytest.raises(ValidationError):
        cs.start_comfyui(None, confirm_path=str(link / "run_nvidia_gpu.bat"))
    assert no_spawn == []
    assert cs.resolve_launcher()["path"] == resolved


def test_starting_is_refused_while_the_updater_is_running(portable, no_spawn,
                                                          monkeypatch):
    monkeypatch.setitem(cs._run_state, "status", "running")
    with pytest.raises(ConflictError):
        cs.start_comfyui(None, confirm_path=cs.resolve_launcher()["path"])
    assert no_spawn == []


# ---------------------------------------------------------------------------
# The copy into the ComfyUI install
# ---------------------------------------------------------------------------

def test_the_copy_is_refused_when_its_folder_is_outside_every_root(
        portable, tmp_path, monkeypatch):
    wid = _workflow(portable["root"], "workflows/flow.json")
    outside = tmp_path / "Outside" / "workflows"
    monkeypatch.setattr(cs, "user_workflows_dir", lambda cfg=None: outside)
    from app.core.errors import PathNotAllowed

    with pytest.raises(PathNotAllowed):
        cs.copy_into_user_workflows(f"workflow:{wid}",
                                    confirm_destination=str(outside / "flow.json"))
    assert not outside.exists()


@pytest.mark.parametrize("name", [
    "..\\..\\escaped.json",
    "C:\\Windows\\Temp\\absolute.json",
    "stream.json:ads",
    "CON.json",
    "trailing. ",
    "x" * 300 + ".json",
])
def test_a_hostile_destination_is_refused_even_once_it_is_confirmed(
        portable, monkeypatch, name):
    """The destination is derived from the indexed row - and validated anyway.

    A workflow's filename comes off the owner's own disk, so these shapes cannot
    be reached through the API today.  What is asserted is the second line of
    defence: if a row ever carried one, ``validate_filename`` and
    ``resolve_within_roots`` still run on the path that would be written, rather
    than the code trusting that deriving a path made it safe.
    """
    from app.core.errors import PathNotAllowed

    wid = _workflow(portable["root"], "workflows/flow.json")
    target = cs.user_workflows_dir()
    hostile = str(target / name)
    poisoned = dict(cs.copy_plan(cs._workflow_row(f"workflow:{wid}")))
    poisoned.update({"possible": True, "needed": True, "reason": None,
                     "destination": hostile, "exists": False})
    monkeypatch.setattr(cs, "copy_plan", lambda row, cfg=None: poisoned)

    before = sorted(p.name for p in target.iterdir())
    with pytest.raises((ValidationError, ConflictError, PathNotAllowed, OSError)):
        cs.copy_into_user_workflows(f"workflow:{wid}", confirm_destination=hostile)
    assert sorted(p.name for p in target.iterdir()) == before
    assert not (portable["base"] / "escaped.json").exists()
    assert not Path("C:\\Windows\\Temp\\absolute.json").exists()


def test_the_copy_never_overwrites_a_file_that_appears_after_the_check(
        portable, monkeypatch):
    """The existence check gives a good message; the exclusive create is the rule."""
    wid = _workflow(portable["root"], "workflows/flow.json")
    plan = cs.open_workflow_plan(f"workflow:{wid}")
    destination = Path(plan["copy"]["destination"])

    real_makedirs = cs.os.makedirs

    def _race(path, exist_ok=False):
        real_makedirs(path, exist_ok=exist_ok)
        if not destination.exists():
            destination.write_text("ALREADY HERE", encoding="utf-8")

    monkeypatch.setattr(cs.os, "makedirs", _race)
    with pytest.raises(ConflictError):
        cs.copy_into_user_workflows(f"workflow:{wid}",
                                    confirm_destination=str(destination))
    assert destination.read_text(encoding="utf-8") == "ALREADY HERE"


def test_the_copy_is_consented_independently_of_the_start(portable, no_spawn):
    """Two writes, two questions.  Neither answer is the other's."""
    client = portable["client"]
    wid = _workflow(portable["root"], "workflows/flow.json")
    plan = client.get("/api/v1/comfyui/open-workflow/plan",
                      params={"uid": f"workflow:{wid}"}).json()
    destination = Path(plan["copy"]["destination"])

    # The start confirmation is not a copy confirmation.
    response = client.post(
        "/api/v1/comfyui/open-workflow",
        json={"uid": f"workflow:{wid}", "copy_to_user_workflows": True,
              "confirm_copy_destination": plan["launcher_confirm_path"]})
    assert response.status_code == 422
    assert not destination.exists()

    # ...and the copy confirmation is not consent to start a program.
    response = client.post(
        "/api/v1/comfyui/open-workflow",
        json={"uid": f"workflow:{wid}", "copy_to_user_workflows": True,
              "confirm_copy_destination": str(destination)})
    assert response.status_code == 409
    assert destination.exists()
    assert no_spawn == []


# ---------------------------------------------------------------------------
# The launch stream (S-03 applies here too)
# ---------------------------------------------------------------------------

def test_the_launch_stream_honours_the_subscriber_cap():
    """S-03 applies to the launch channel too - executed, not read."""
    import asyncio

    from app.api.deps import require_stream_capacity
    from app.api.middleware import ApiError
    from app.core import progress

    bus = cs.LAUNCH_CHANNEL

    async def drive() -> None:
        held = [bus.subscribe() for _ in range(progress.MAX_SUBSCRIBERS)]
        tasks = [asyncio.ensure_future(anext(g)) for g in held]
        await asyncio.sleep(0.05)
        assert bus.subscriber_count == progress.MAX_SUBSCRIBERS
        assert not bus.has_capacity()
        with pytest.raises(ApiError) as caught:
            require_stream_capacity(bus)
        assert caught.value.http_status == 503
        with pytest.raises(progress.SubscriberLimitError):
            await anext(bus.subscribe())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(drive())
    assert bus.has_capacity()


def test_the_launch_stream_closes_itself_when_the_launch_ends(portable):
    """``close_on_done`` is what stops a finished launch holding a slot."""
    import inspect

    from app.api.v1 import comfyui_router

    source = inspect.getsource(comfyui_router.launch_stream)
    assert "require_stream_capacity" in source
    assert "close_on_done=True" in source


# ---------------------------------------------------------------------------
# Reach, disclosure and audit
# ---------------------------------------------------------------------------

def test_the_launcher_is_not_reachable_from_mcp():
    from app.mcp import registry

    joined = str(registry.TOOLS).lower()
    for needle in ("launch", "start_comfyui", "open_workflow", "launcher",
                   "run_nvidia", "confirm_launcher_path"):
        assert needle not in joined, needle
    assert not any("launch" in tool.name or "open" in tool.name
                   for tool in registry.TOOLS)


def test_no_module_outside_the_router_can_start_comfyui(app_dir):
    callers = set()
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "start_comfyui" in line and not line.strip().startswith("#"):
                callers.add(str(path.relative_to(app_dir)).replace("\\", "/"))
    assert callers == {"services/comfyui_service.py"}
    startup = (app_dir / "core" / "__init__.py").read_text(encoding="utf-8")
    assert "comfyui_service" not in startup


def test_starting_requires_the_csrf_header(naked_client):
    response = naked_client.post("/api/v1/comfyui/open-workflow",
                                 json={"uid": "workflow:1", "start": True,
                                       "confirm_launcher_path": "x"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CSRF_HEADER_MISSING"


def test_launch_status_leaks_no_environment(portable):
    body = portable["client"].get("/api/v1/comfyui/launch/status").json()
    flat = str(body)
    for key in ("env", "environ", "USERPROFILE", "APPDATA", "TEMP="):
        assert key not in flat
    for key in ("civitai", "api_key", "token", "password"):
        assert key not in flat.lower()


def test_a_launch_is_visible_to_the_owner_while_and_after_it_runs(
        portable, monkeypatch):
    """The record is in-process only - the same standing the updater has."""
    monkeypatch.setattr(cs.subprocess, "Popen", lambda argv, **k: _FakeProc())
    monkeypatch.setattr(cs, "_port_open", lambda port, timeout=0.25: True)
    launcher = cs.resolve_launcher()
    cs.start_comfyui(None, confirm_path=launcher["path"], trigger="test")
    status = _settle()
    assert status["status"] == "ready"
    assert status["path"] == launcher["path"]
    assert status["trigger"] == "test"
    assert status["started_at"] and status["finished_at"]
    body = portable["client"].get("/api/v1/comfyui/launch/status").json()
    assert body["path"] == launcher["path"]
    # SECURITY_REVIEW S-23: nothing is persisted, so a restart forgets it.
    row = dbmod.one(dbmod.get_ro(),
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('launch_audit','process_audit')", ())
    assert row is None


# ---------------------------------------------------------------------------
# Process hygiene
# ---------------------------------------------------------------------------

def test_a_launcher_that_exits_first_is_reported_not_hidden(portable, monkeypatch):
    class _Dead:
        pid = 7

        def poll(self):
            return 3

    monkeypatch.setattr(cs.subprocess, "Popen", lambda argv, **k: _Dead())
    monkeypatch.setattr(cs, "_port_open", lambda port, timeout=0.25: False)
    cs.start_comfyui(None, confirm_path=cs.resolve_launcher()["path"])
    status = _settle()
    assert status["status"] == "failed"
    assert status["ready"] is False
    assert status["exit_code"] == 3
    assert "exited with code 3" in status["error"]


def test_a_launch_that_cannot_be_watched_still_ends(portable, monkeypatch):
    """A wait that raises must not wedge the subsystem behind "already starting"."""
    monkeypatch.setattr(cs.subprocess, "Popen", lambda argv, **k: _FakeProc())

    def _explode(port, timeout=0.25):
        raise OverflowError("port must be 0-65535")

    monkeypatch.setattr(cs, "_port_open", _explode)
    cs.start_comfyui(None, confirm_path=cs.resolve_launcher()["path"])
    status = _settle()
    assert status["status"] == "failed"
    assert status["running"] is False
    assert status["error"]

    # ...and the next launch is not refused by a stranded state machine.
    monkeypatch.setattr(cs, "_port_open", lambda port, timeout=0.25: True)
    cs.start_comfyui(None, confirm_path=cs.resolve_launcher()["path"])
    assert _settle()["status"] == "ready"


def test_a_second_launch_is_refused_while_the_first_is_starting(portable,
                                                                monkeypatch):
    monkeypatch.setattr(cs.subprocess, "Popen", lambda argv, **k: _FakeProc())
    monkeypatch.setattr(cs, "_port_open", lambda port, timeout=0.25: False)
    monkeypatch.setattr(cs, "LAUNCH_TIMEOUT_S", 0.5)
    monkeypatch.setattr(cs, "LAUNCH_POLL_S", 0.05)
    resolved = cs.resolve_launcher()["path"]
    cs.start_comfyui(None, confirm_path=resolved)
    with pytest.raises(ConflictError):
        cs.start_comfyui(None, confirm_path=resolved)
    status = _settle()
    assert status["status"] == "failed"
    assert "within" in status["error"], "a timeout must say it timed out"


def test_the_launch_timeout_is_bounded_and_honest():
    assert 0 < cs.LAUNCH_TIMEOUT_S <= 3600
    assert 0 < cs.LAUNCH_POLL_S <= 5
    assert cs.MAX_LAUNCHER_BYTES <= 1024 * 1024


def test_the_owner_library_root_is_never_touched_by_this_file(portable, tmp_path):
    """Every path this module writes to is inside ``tmp_path``."""
    cfg = config_service.get_config()
    for root in cfg.roots:
        if root.kind == "data":
            continue
        assert str(tmp_path) in str(root.path) or not Path(root.path).exists()
