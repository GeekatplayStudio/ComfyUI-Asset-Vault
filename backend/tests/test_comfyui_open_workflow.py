"""Opening a workflow inside ComfyUI: discovery, deep links, and the two gates.

No test here ever starts ComfyUI.  The one that gets closest replaces
``subprocess.Popen`` with a recorder and asserts the *shape* of the argv the
launcher would receive; every other test asserts a refusal, because starting a
program and writing into someone's ComfyUI installation are user decisions, not
test fixtures.

Everything happens inside ``tmp_path``: a synthetic portable layout with its own
``vault.db``.  The owner's real install is never read from and never written to.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core import config_service  # noqa: E402
from app.core import db as dbmod  # noqa: E402
from app.core.errors import ConflictError, NotFoundError, ValidationError  # noqa: E402
from app.core.migrations import migrate  # noqa: E402
from app.services import comfyui_service as cs  # noqa: E402

GRAPH = '{"nodes": [], "links": []}'


def _portable(base: Path) -> Path:
    """The real portable layout: ComfyUI, python_embeded and the launchers."""
    root = base / "ComfyUI"
    (root / "custom_nodes").mkdir(parents=True, exist_ok=True)
    (root / "user" / "default" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "workflows").mkdir(parents=True, exist_ok=True)
    # SECURITY_REVIEW S-20: a folder must carry comfyui_version.py, main.py and
    # models/ before it is allowed to nominate an executable, so the fixture
    # stages a real install rather than the two files the launcher used to need.
    (root / "models" / "checkpoints").mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("# ComfyUI\n", encoding="utf-8")
    (root / "comfyui_version.py").write_text('__version__ = "0.33.0"\n',
                                             encoding="utf-8")

    site = base / "python_embeded" / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (base / "python_embeded" / "python.exe").write_bytes(b"MZ")
    (site / "comfyui_frontend_package-1.49.6.dist-info").mkdir(exist_ok=True)

    # Two launchers, one of them the preferred one, and only one naming a port.
    (base / "run_cpu.bat").write_text(
        ".\\python_embeded\\python.exe -s ComfyUI\\main.py --cpu\n", encoding="utf-8")
    (base / "run_nvidia_gpu.bat").write_text(
        ".\\python_embeded\\python.exe -s ComfyUI\\main.py "
        "--windows-standalone-build --listen 0.0.0.0 --port 8189\n",
        encoding="utf-8")
    return root


def _add_workflow(root: Path, rel: str, *, name: str | None = None) -> int:
    """Write a workflow file under ``root`` and index a row that points at it."""
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
             str(Path(rel).parent).replace("/", "\\"),
             name or path.stem, len(GRAPH), now * 1_000_000, "fp:" + rel, now, now))
        conn.commit()
        return cur.lastrowid

    return int(dbmod.writer().run(_op))


@pytest.fixture
def portable(tmp_path):
    db = tmp_path / "openwf.db"
    original = dbmod.db_path()
    dbmod.set_db_path(db)
    migrate()
    config_service.invalidate()
    root = _portable(tmp_path)
    config_service.set_config({"comfyui_path": str(root), "is_configured": True})
    cs._launch_state.clear()
    cs._launch_state.update({"status": "idle"})
    yield {"root": root, "base": tmp_path}
    cs._launch_state.clear()
    cs._launch_state.update({"status": "idle"})
    dbmod.shutdown_writer()
    dbmod.close_thread_connections()
    dbmod.set_db_path(original)
    config_service.invalidate()


@pytest.fixture
def not_running(monkeypatch):
    monkeypatch.setattr(cs, "is_running", lambda: {
        "running": False, "ports": [], "method": "loopback tcp probe",
        "confidence": "inferred", "note": "nothing is listening"})


# ---------------------------------------------------------------------------
# Launcher discovery - found on disk, never hard-coded
# ---------------------------------------------------------------------------

def test_launchers_are_discovered_beside_the_comfyui_folder(portable):
    install = cs.probe()
    by_id = {entry["id"]: entry for entry in install.launchers}
    assert "run_nvidia_gpu" in by_id
    assert "run_cpu" in by_id

    chosen = by_id["run_nvidia_gpu"]
    assert chosen["path"] == str(portable["base"] / "run_nvidia_gpu.bat")
    assert Path(chosen["path"]).is_file()
    # The launcher runs from the folder it lives in: its own command line uses
    # relative paths (`.\python_embeded\...`) and breaks anywhere else.
    assert chosen["working_dir"] == str(portable["base"])
    assert chosen["command"] == [chosen["path"]]
    assert chosen["recommended"] is True


def test_the_port_is_read_out_of_the_launcher_not_guessed(portable):
    by_id = {e["id"]: e for e in cs.probe().launchers}
    assert by_id["run_nvidia_gpu"]["port"] == 8189
    assert by_id["run_nvidia_gpu"]["port_source"] == "launcher command line"
    # run_cpu.bat names no port, so nothing is invented for it.
    assert by_id["run_cpu"]["port"] is None


def test_a_python_fallback_is_offered_when_main_py_exists(portable):
    by_id = {e["id"]: e for e in cs.probe().launchers}
    entry = by_id["python_main"]
    assert entry["command"][0].endswith("python.exe")
    assert entry["command"][-1].endswith("main.py")
    assert entry["working_dir"] == str(portable["root"])


def test_an_install_with_no_launcher_is_a_404_naming_what_was_looked_for(
        portable, tmp_path):
    bare = tmp_path / "bare" / "ComfyUI"
    bare.mkdir(parents=True)
    config_service.set_config({"comfyui_path": str(bare)})
    with pytest.raises(NotFoundError) as excinfo:
        cs.resolve_launcher()
    assert excinfo.value.details["looked_for"]


def test_an_unknown_launcher_id_is_refused(portable):
    with pytest.raises(NotFoundError):
        cs.resolve_launcher("run_something_else")


# ---------------------------------------------------------------------------
# Deep links - what this frontend actually supports
# ---------------------------------------------------------------------------

def test_a_bundled_example_graph_has_a_real_deep_link(portable):
    link = cs.deep_link_for(str(portable["root"] / "custom_nodes" / "ComfyUI-Pack"
                                / "example_workflows" / "basic_flow.json"))
    assert link["supported"] is True
    assert link["params"] == {"template": "basic_flow", "source": "ComfyUI-Pack"}
    assert link["query"] == "?template=basic_flow&source=ComfyUI-Pack"


@pytest.mark.parametrize("folder", cs.EXAMPLE_WORKFLOW_DIRS)
def test_every_folder_name_comfyui_serves_is_addressable(portable, folder):
    link = cs.deep_link_for(str(portable["root"] / "custom_nodes" / "Pack"
                                / folder / "graph.json"))
    assert link["supported"] is True, folder


def test_a_graph_nested_deeper_than_comfyui_serves_has_no_link(portable):
    link = cs.deep_link_for(str(portable["root"] / "custom_nodes" / "Pack"
                                / "example_workflows" / "extra" / "graph.json"))
    assert link["supported"] is False
    assert link["reason"] == "not_served_by_comfyui"


def test_a_name_the_frontend_would_reject_is_reported_not_shipped(portable):
    """The frontend refuses anything outside ``[A-Za-z0-9_.-]``.  Emitting a URL
    it will silently drop would be worse than saying it cannot be linked."""
    link = cs.deep_link_for(str(portable["root"] / "custom_nodes" / "Pack"
                                / "example_workflows" / "my great flow.json"))
    assert link["supported"] is False
    assert link["reason"] == "name_not_url_addressable"
    assert "my great flow" in link["explanation"]


def test_a_disabled_package_is_not_served(portable):
    link = cs.deep_link_for(str(portable["root"] / "custom_nodes" / "Pack.disabled"
                                / "example_workflows" / "graph.json"))
    assert link["supported"] is False
    assert link["reason"] == "package_disabled"


def test_a_user_workflow_has_no_deep_link_in_this_frontend(portable):
    link = cs.deep_link_for(str(portable["root"] / "user" / "default"
                                / "workflows" / "mine.json"))
    assert link["supported"] is False
    assert link["reason"] == "user_workflow_has_no_deep_link"
    assert "Workflows sidebar" in link["explanation"]
    assert link["verified_against"] == cs.DEEP_LINK_VERIFIED_AGAINST


def test_an_official_template_is_addressable_as_source_default(portable):
    site = portable["base"] / "python_embeded" / "Lib" / "site-packages"
    link = cs.deep_link_for(str(site / "comfyui_workflow_templates_json"
                                / "templates" / "alpha_flow.json"))
    assert link["supported"] is True
    assert link["params"] == {"template": "alpha_flow", "source": "default"}


# ---------------------------------------------------------------------------
# The plan - shown before anything happens
# ---------------------------------------------------------------------------

def test_the_plan_names_the_launcher_the_port_and_the_url(portable, not_running):
    wid = _add_workflow(portable["root"],
                        "custom_nodes/ComfyUI-Pack/example_workflows/basic_flow.json")
    plan = cs.open_workflow_plan(f"workflow:{wid}")

    assert plan["needs_start"] is True
    assert plan["launcher"]["id"] == "run_nvidia_gpu"
    assert plan["launcher_confirm_path"] == plan["launcher"]["path"]
    # The port comes from the launcher that would actually run.
    assert plan["port"] == 8189
    assert plan["url"] == ("http://127.0.0.1:8189/"
                           "?template=basic_flow&source=ComfyUI-Pack")
    assert plan["open_method"] == "deep_link"
    assert any(plan["launcher"]["path"] in step for step in plan["steps"])


def test_the_plan_admits_when_there_is_no_deep_link(portable, not_running):
    wid = _add_workflow(portable["root"], "user/default/workflows/mine.json")
    plan = cs.open_workflow_plan(f"workflow:{wid}")
    assert plan["open_method"] == "manual"
    assert plan["url"] == "http://127.0.0.1:8189/"
    assert "Workflows sidebar" in plan["manual_hint"]
    # Already where ComfyUI keeps user workflows, so no copy is proposed.
    assert plan["copy"]["possible"] is False
    assert plan["copy"]["reason"] == "already_in_the_workflows_folder"


def test_the_plan_names_the_exact_copy_destination(portable, not_running):
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")
    plan = cs.open_workflow_plan(f"workflow:{wid}")
    assert plan["copy"]["possible"] is True
    assert plan["copy"]["destination"] == str(
        portable["root"] / "user" / "default" / "workflows" / "root_flow.json")
    # It must not pretend the copy buys a link it cannot buy.
    assert plan["copy"]["creates_deep_link"] is False


def test_a_running_comfyui_is_reported_and_its_port_wins(portable, monkeypatch):
    monkeypatch.setattr(cs, "is_running", lambda: {
        "running": True, "ports": [8188], "method": "loopback tcp probe",
        "confidence": "inferred", "note": "listening"})
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")
    plan = cs.open_workflow_plan(f"workflow:{wid}")
    assert plan["needs_start"] is False
    assert plan["port"] == 8188
    assert plan["url"].startswith("http://127.0.0.1:8188/")


def test_only_a_workflow_uid_is_accepted(portable):
    with pytest.raises(ValidationError):
        cs.open_workflow_plan("model:1")
    with pytest.raises(NotFoundError):
        cs.open_workflow_plan("workflow:99999")


# ---------------------------------------------------------------------------
# Gate 1 - starting ComfyUI is never implicit
# ---------------------------------------------------------------------------

def test_opening_does_not_start_comfyui_on_its_own(portable, not_running,
                                                   monkeypatch):
    """Clicking "open" is not consent to run a program."""
    calls: list = []
    monkeypatch.setattr(cs.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)))
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")

    with pytest.raises(ConflictError) as excinfo:
        cs.open_workflow(f"workflow:{wid}")

    assert calls == []
    details = excinfo.value.details
    assert details["confirm_launcher_path"].endswith("run_nvidia_gpu.bat")


def test_a_start_confirmed_for_the_wrong_path_starts_nothing(portable, not_running,
                                                             monkeypatch):
    calls: list = []
    monkeypatch.setattr(cs.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)))
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")

    with pytest.raises(ValidationError) as excinfo:
        cs.open_workflow(f"workflow:{wid}", start=True,
                         confirm_launcher_path=str(portable["base"] / "run_cpu.bat"))

    assert calls == []
    assert excinfo.value.details["resolved_path"].endswith("run_nvidia_gpu.bat")


def test_an_empty_confirmation_starts_nothing(portable, not_running, monkeypatch):
    calls: list = []
    monkeypatch.setattr(cs.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ValidationError):
        cs.start_comfyui(None, confirm_path="")
    assert calls == []


class _FakeProc:
    pid = 4242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


def test_a_confirmed_start_runs_the_discovered_argv_with_no_shell(
        portable, not_running, monkeypatch):
    """The one test that reaches the spawn, and it spawns nothing real.

    What it pins is the shape the security review cares about: a list argv, no
    ``shell``, no user-supplied string anywhere in it, and the working directory
    the launcher needs.
    """
    seen: dict = {}

    def _fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(cs.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(cs, "_port_open", lambda port, timeout=0.25: True)

    launcher = cs.resolve_launcher()
    started = cs.start_comfyui(None, confirm_path=launcher["path"])
    assert started["started"] is True
    assert started["stream"] == "/api/v1/comfyui/launch/stream"

    deadline = time.monotonic() + 10
    while cs.launch_status()["status"] == "starting" and time.monotonic() < deadline:
        time.sleep(0.02)

    assert seen["argv"] == [launcher["path"]]
    assert isinstance(seen["argv"], list)
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["cwd"] == str(portable["base"])
    assert seen["kwargs"]["stdin"] == cs.subprocess.DEVNULL

    status = cs.launch_status()
    assert status["status"] == "ready"
    assert status["ready"] is True
    assert status["port"] == 8189
    assert status["pid"] == 4242


def test_a_launcher_that_exits_before_the_port_opens_is_reported_honestly(
        portable, not_running, monkeypatch):
    class _Dead:
        pid = 7
        def poll(self):
            return 1

    monkeypatch.setattr(cs.subprocess, "Popen", lambda argv, **k: _Dead())
    monkeypatch.setattr(cs, "_port_open", lambda port, timeout=0.25: False)

    launcher = cs.resolve_launcher()
    cs.start_comfyui(None, confirm_path=launcher["path"])
    deadline = time.monotonic() + 10
    while cs.launch_status()["status"] == "starting" and time.monotonic() < deadline:
        time.sleep(0.02)

    status = cs.launch_status()
    assert status["status"] == "failed"
    assert status["ready"] is False
    assert status["exit_code"] == 1
    assert "exited with code 1" in status["error"]


def test_starting_is_refused_while_comfyui_is_already_running(portable,
                                                              monkeypatch):
    monkeypatch.setattr(cs, "is_running", lambda: {"running": True, "ports": [8188]})
    calls: list = []
    monkeypatch.setattr(cs.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)))
    launcher = cs.resolve_launcher()
    with pytest.raises(ConflictError):
        cs.start_comfyui(None, confirm_path=launcher["path"])
    assert calls == []


def test_an_already_running_comfyui_is_opened_without_starting_anything(
        portable, monkeypatch):
    monkeypatch.setattr(cs, "is_running", lambda: {
        "running": True, "ports": [8188], "method": "loopback tcp probe",
        "confidence": "inferred", "note": "listening"})
    calls: list = []
    monkeypatch.setattr(cs.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)))
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")

    result = cs.open_workflow(f"workflow:{wid}")

    assert calls == []
    assert result["already_running"] is True
    assert result["started"] is False
    assert result["ready"] is True
    assert result["url"].startswith("http://127.0.0.1:8188/")


# ---------------------------------------------------------------------------
# Gate 2 - writing into the ComfyUI installation
# ---------------------------------------------------------------------------

def test_a_copy_without_its_own_confirmation_writes_nothing(portable, not_running):
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")
    destination = (portable["root"] / "user" / "default" / "workflows"
                   / "root_flow.json")
    with pytest.raises(ValidationError):
        cs.copy_into_user_workflows(f"workflow:{wid}", confirm_destination="")
    assert not destination.exists()


def test_a_copy_confirmed_for_another_path_writes_nothing(portable, not_running):
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")
    destination = (portable["root"] / "user" / "default" / "workflows"
                   / "root_flow.json")
    with pytest.raises(ValidationError):
        cs.copy_into_user_workflows(
            f"workflow:{wid}",
            confirm_destination=str(portable["root"] / "user" / "default"
                                    / "workflows" / "something_else.json"))
    assert not destination.exists()


def test_a_confirmed_copy_lands_exactly_where_the_plan_said(portable, not_running):
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")
    plan = cs.open_workflow_plan(f"workflow:{wid}")
    result = cs.copy_into_user_workflows(
        f"workflow:{wid}", confirm_destination=plan["copy"]["destination"])

    destination = Path(result["destination"])
    assert destination == Path(plan["copy"]["destination"])
    assert destination.read_text(encoding="utf-8") == GRAPH


def test_a_copy_never_overwrites_a_file_that_is_already_there(portable,
                                                             not_running):
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")
    existing = (portable["root"] / "user" / "default" / "workflows"
                / "root_flow.json")
    existing.write_text("ALREADY HERE", encoding="utf-8")

    plan = cs.open_workflow_plan(f"workflow:{wid}")
    assert plan["copy"]["exists"] is True
    assert plan["copy"]["possible"] is False

    with pytest.raises(ConflictError):
        cs.copy_into_user_workflows(f"workflow:{wid}",
                                    confirm_destination=str(existing))
    assert existing.read_text(encoding="utf-8") == "ALREADY HERE"


def test_open_workflow_can_copy_and_then_refuse_to_start(portable, not_running,
                                                         monkeypatch):
    """The two gates are independent: consenting to the copy is not consenting
    to start a program, and the copy still happens before the refusal."""
    calls: list = []
    monkeypatch.setattr(cs.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)))
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")
    plan = cs.open_workflow_plan(f"workflow:{wid}")

    with pytest.raises(ConflictError):
        cs.open_workflow(f"workflow:{wid}", copy_to_user_workflows=True,
                         confirm_copy_destination=plan["copy"]["destination"])

    assert calls == []
    assert Path(plan["copy"]["destination"]).exists()


def test_the_copy_target_is_inside_a_configured_root(portable, not_running):
    """The destination is derived, but it is still resolved through pathsafe
    against the configured roots - the vault writes only where it already reaches.
    """
    wid = _add_workflow(portable["root"], "workflows/root_flow.json")
    plan = cs.open_workflow_plan(f"workflow:{wid}")
    roots = [Path(r.path) for r in config_service.get_config().roots]
    destination = Path(plan["copy"]["destination"])
    assert any(destination.is_relative_to(root) for root in roots)
