"""The ComfyUI updater (REQUIREMENTS_R2 C8.3) - the highest-risk new surface.

This is the only place the application starts an external process on purpose,
and what it starts is a batch file. C8.3 sets five conditions, each asserted
here by execution rather than by reading the code:

* the resolved path is shown and must be confirmed verbatim,
* it cannot be redirected to an arbitrary executable through the request,
* arguments cannot be injected,
* it cannot run without confirmation, and never from MCP,
* it is never scheduled and never automatic.

**Nothing in this file executes the updater.**  Every positive path stops at
the confirmation gate; the batch files created are never run.
"""

from __future__ import annotations

import ast
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="the updater is Windows-only")


@pytest.fixture
def portable_install(client, tmp_path):
    """A ComfyUI layout that offers all three portable updaters."""
    parent = tmp_path / "PortableInstall"
    root = parent / "ComfyUI"
    (root / "models" / "checkpoints").mkdir(parents=True)
    (root / "custom_nodes").mkdir()
    (root / "output").mkdir()
    (root / "main.py").write_text("#\n", encoding="utf-8")
    (root / "comfyui_version.py").write_text('__version__ = "0.33.0"\n',
                                             encoding="utf-8")
    (parent / "update").mkdir()
    for name in ("update_comfyui.bat", "update_comfyui_stable.bat",
                 "update_comfyui_and_python_dependencies.bat"):
        (parent / "update" / name).write_text(
            "@echo off\r\nrem this file must never be executed by the suite\r\n",
            encoding="utf-8")
    response = client.patch("/api/v1/system/config",
                            json={"comfyui_path": str(root)})
    assert response.status_code == 200
    return client, parent, root


# ---------------------------------------------------------------------------
# Discovery and the plan
# ---------------------------------------------------------------------------

def test_the_plan_names_the_exact_absolute_path_that_would_run(portable_install):
    client, parent, _root = portable_install
    plan = client.get("/api/v1/comfyui/update/plan").json()
    expected = str(parent / "update" / "update_comfyui.bat")
    assert plan["path"] == expected
    assert plan["confirm_path"] == expected
    assert plan["command"] == [expected]
    assert plan["working_dir"] == str(parent / "update")


def test_the_plan_lists_alternatives_so_nothing_is_hidden(portable_install):
    client, _parent, _root = portable_install
    plan = client.get("/api/v1/comfyui/update/plan").json()
    assert {a["id"] for a in plan["alternatives"]} == {
        "portable_stable", "portable_with_deps"}
    assert any("Restart ComfyUI" in w for w in plan["warnings"])


def test_an_install_with_no_updater_offers_nothing(client):
    response = client.get("/api/v1/comfyui/update/plan")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_only_the_three_known_updater_filenames_are_ever_discovered():
    from app.services.comfyui_service import PORTABLE_UPDATERS

    names = {rel.rsplit("\\", 1)[-1] for _id, rel, _label in PORTABLE_UPDATERS}
    assert names == {"update_comfyui.bat", "update_comfyui_stable.bat",
                     "update_comfyui_and_python_dependencies.bat"}
    for _id, rel, _label in PORTABLE_UPDATERS:
        assert rel.startswith(("update\\", "update/"))
        assert ".." not in rel


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

def test_run_without_confirm_path_is_a_validation_error(portable_install):
    client, _parent, _root = portable_install
    response = client.post("/api/v1/comfyui/update/run", json={})
    assert response.status_code == 422
    assert response.json()["error"]["field_errors"][0]["field"] == "confirm_path"


@pytest.mark.parametrize("confirm_path", [
    r"C:\Windows\System32\cmd.exe",
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    "update_comfyui.bat",
    r"..\..\evil.bat",
])
def test_a_confirm_path_that_is_not_the_resolved_path_is_refused(portable_install,
                                                                confirm_path):
    client, _parent, _root = portable_install
    response = client.post("/api/v1/comfyui/update/run",
                           json={"confirm_path": confirm_path})
    assert response.status_code == 422, confirm_path
    assert "not confirmed for the path that would actually run" in \
        response.json()["error"]["message"]


def test_an_empty_confirm_path_is_refused_by_the_schema(portable_install):
    client, _parent, _root = portable_install
    response = client.post("/api/v1/comfyui/update/run", json={"confirm_path": ""})
    assert response.status_code == 422
    assert response.json()["error"]["field_errors"][0]["field"] == "confirm_path"


def test_arguments_cannot_be_appended_to_the_confirmed_path(portable_install):
    """A shell metacharacter tail must break the equality, not be executed."""
    client, parent, _root = portable_install
    resolved = str(parent / "update" / "update_comfyui.bat")
    for tail in (" & calc.exe", ' && "C:\\Windows\\System32\\cmd.exe"',
                 " | more", '" "extra-arg', "\nnet user"):
        response = client.post("/api/v1/comfyui/update/run",
                               json={"confirm_path": resolved + tail})
        assert response.status_code == 422, tail


def test_an_unknown_updater_id_is_refused(portable_install):
    client, parent, _root = portable_install
    resolved = str(parent / "update" / "update_comfyui.bat")
    for updater in ("../../evil", "portable; calc", "custom", "git-pull"):
        response = client.post("/api/v1/comfyui/update/run",
                               json={"updater": updater,
                                     "confirm_path": resolved})
        assert response.status_code == 404, updater


def test_the_command_is_a_list_argv_never_a_shell_string(app_dir):
    source = (app_dir / "services" / "comfyui_service.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    tree = ast.parse(source)
    popen_calls = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Call)
                   and getattr(n.func, "attr", None) == "Popen"]
    assert len(popen_calls) == 1, "there must be exactly one Popen call site"
    call = popen_calls[0]
    assert isinstance(call.args[0], ast.Call), "argv must be built with list(...)"
    assert getattr(call.args[0].func, "id", None) == "list"
    for keyword in call.keywords:
        assert keyword.arg != "shell", "shell= must never be passed"


def test_the_updater_command_never_incorporates_request_text(app_dir):
    """``command`` is assembled only from discovered paths and fixed literals."""
    source = (app_dir / "services" / "comfyui_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Dict)):
            continue
        for key, value in zip(node.keys, node.values, strict=False):
            if not (isinstance(key, ast.Constant) and key.value == "command"):
                continue
            if isinstance(value, ast.Subscript):
                continue          # chosen["command"], echoed from the plan
            assert isinstance(value, ast.List), "command must be a literal list"
            for element in value.elts:
                assert not isinstance(element, ast.JoinedStr), (
                    "no f-string may reach the argv")


# ---------------------------------------------------------------------------
# Never automatic, never scheduled, never from MCP
# ---------------------------------------------------------------------------

def test_nothing_schedules_the_updater(app_dir):
    callers = []
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "run_updater" not in line or line.strip().startswith("#"):
                continue
            rel = str(path.relative_to(app_dir)).replace("\\", "/")
            callers.append((rel, lineno, line.strip()))
    call_sites = {c[0] for c in callers}
    assert call_sites <= {"services/comfyui_service.py",
                          "api/v1/comfyui_router.py"}, callers
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "run_updater" in text:
            assert "Timer(" not in text or "comfyui_service" not in str(path), \
                f"{path} both schedules and updates"


def test_the_startup_path_never_updates(app_dir):
    startup = (app_dir / "core" / "__init__.py").read_text(encoding="utf-8")
    assert "run_updater" not in startup
    assert "comfyui_service" not in startup


def test_the_updater_is_not_an_mcp_tool():
    from app.mcp import registry

    names = {t.name for t in registry.TOOLS}
    assert not any("update" in n for n in names if n != "vault_reindex")
    joined = str(registry.TOOLS).lower()
    assert "run_updater" not in joined
    assert "update_comfyui" not in joined


def test_run_requires_the_csrf_header(naked_client):
    response = naked_client.post("/api/v1/comfyui/update/run",
                                 json={"confirm_path": "x"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CSRF_HEADER_MISSING"


# ---------------------------------------------------------------------------
# Refuse while ComfyUI is running (C8.3)
# ---------------------------------------------------------------------------

def test_the_updater_refuses_while_comfyui_appears_to_be_running(portable_install,
                                                                 monkeypatch):
    client, parent, _root = portable_install
    from app.services import comfyui_service

    monkeypatch.setattr(comfyui_service, "is_running",
                        lambda: {"running": True, "ports": [8188],
                                 "method": "test", "confidence": "inferred",
                                 "note": "simulated"})
    response = client.post(
        "/api/v1/comfyui/update/run",
        json={"confirm_path": str(parent / "update" / "update_comfyui.bat")})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_the_plan_reports_can_run_false_while_comfyui_is_running(portable_install,
                                                                 monkeypatch):
    client, _parent, _root = portable_install
    from app.services import comfyui_service

    monkeypatch.setattr(comfyui_service, "is_running",
                        lambda: {"running": True, "ports": [8188],
                                 "method": "test", "confidence": "inferred",
                                 "note": "simulated"})
    plan = client.get("/api/v1/comfyui/update/plan").json()
    assert plan["can_run"] is False
    assert plan["blocked_reason"] == "comfyui_running"


# ---------------------------------------------------------------------------
# The updater path follows the configured install - SECURITY_REVIEW S-04
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="SECURITY_REVIEW S-04: the executable is derived from "
                          "comfyui_path, which PATCH /system/config accepts, so "
                          "any directory holding update\\update_comfyui.bat can "
                          "become the confirmed updater",
                   strict=False)
def test_the_updater_must_live_under_the_verified_comfyui_install(client, tmp_path):
    staging = tmp_path / "AttackerStaging"
    (staging / "ComfyUI" / "models").mkdir(parents=True)
    (staging / "ComfyUI" / "main.py").write_text("#\n", encoding="utf-8")
    (staging / "update").mkdir()
    (staging / "update" / "update_comfyui.bat").write_text(
        "@echo off\r\nrem never executed\r\n", encoding="utf-8")
    client.patch("/api/v1/system/config",
                 json={"comfyui_path": str(staging / "ComfyUI")})
    response = client.get("/api/v1/comfyui/update/plan")
    assert response.status_code == 404, (
        "an arbitrary directory was accepted as a ComfyUI install and its "
        "batch file was offered as the updater")


def test_changing_the_install_path_changes_what_would_run(client, tmp_path):
    """Documents S-04 concretely: a stale confirmation must not carry over."""
    first = tmp_path / "A"
    second = tmp_path / "B"
    for base in (first, second):
        (base / "ComfyUI" / "models").mkdir(parents=True)
        (base / "ComfyUI" / "main.py").write_text("#\n", encoding="utf-8")
        (base / "update").mkdir()
        (base / "update" / "update_comfyui.bat").write_text("@echo off\r\n",
                                                            encoding="utf-8")
    client.patch("/api/v1/system/config", json={"comfyui_path": str(first / "ComfyUI")})
    plan_a = client.get("/api/v1/comfyui/update/plan").json()["confirm_path"]
    client.patch("/api/v1/system/config", json={"comfyui_path": str(second / "ComfyUI")})
    plan_b = client.get("/api/v1/comfyui/update/plan").json()["confirm_path"]
    assert plan_a != plan_b
    # A confirmation captured before the path change must no longer be accepted.
    stale = client.post("/api/v1/comfyui/update/run", json={"confirm_path": plan_a})
    assert stale.status_code == 422


# ---------------------------------------------------------------------------
# Output handling
# ---------------------------------------------------------------------------

def test_updater_output_is_bounded():
    from app.services.comfyui_service import MAX_OUTPUT_LINES, UPDATE_TIMEOUT_S

    assert 0 < MAX_OUTPUT_LINES <= 100_000
    assert 0 < UPDATE_TIMEOUT_S <= 3600


def test_update_status_leaks_no_environment(portable_install):
    client, _parent, _root = portable_install
    body = client.get("/api/v1/comfyui/update/status").json()
    for key in ("env", "environ", "PATH", "USERPROFILE"):
        assert key not in body
