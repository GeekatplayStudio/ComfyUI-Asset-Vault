"""End-to-end traversal and root-containment tests against the live API.

These do not test ``pathsafe`` in isolation (test_pathsafe.py does that); they
drive the actual HTTP surface with hostile input and then look at the *disk* to
see where anything landed.  Every run is hermetic - a synthetic ComfyUI tree in
``tmp_path``; the owner's real library is never touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="Windows-specific path semantics")

#: Folder specs that must never place a file outside the chosen root.
HOSTILE_FOLDERS = [
    "../../ESCAPED",
    "..\\..\\ESCAPED",
    ".. /.. /ESCAPED",
    "... /ESCAPED",
    ".../ESCAPED",
    ".. .\\ESCAPED",
    "models/../../../ESCAPED",
    "C:/Windows/Temp",
    "\\\\?\\C:\\Windows\\Temp",
    "\\\\127.0.0.1\\C$\\Temp",
    "%2e%2e/ESCAPED",
    "//server/share",
    "sub/./../../ESCAPED",
]

#: New names that must never rename a file to another directory or a device.
HOSTILE_NAMES = [
    "../evil.safetensors",
    "..\\evil.safetensors",
    "sub/evil.safetensors",
    "C:\\evil.safetensors",
    "\\\\server\\share\\evil.safetensors",
    "CON.safetensors",
    "NUL",
    "evil<>.safetensors",
]


def _first_model(client):
    items = client.get("/api/v1/models", params={"limit": 50}).json()["items"]
    assert items, "the synthetic install should index at least one model"
    return items[0]


def _snapshot(base: Path) -> set[str]:
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(str(base)):
        for name in list(dirnames) + list(filenames):
            out.add(os.path.join(dirpath, name))
    return out


# ---------------------------------------------------------------------------
# fileops/move
# ---------------------------------------------------------------------------

def test_move_never_places_a_file_outside_its_root(indexed_client, sec_comfyui,
                                                   tmp_path):
    client = indexed_client
    model = _first_model(client)
    outside_before = _snapshot(tmp_path) - _snapshot(sec_comfyui)

    for folder in HOSTILE_FOLDERS:
        response = client.post("/api/v1/fileops/move",
                               json={"uids": [model["uid"]],
                                     "target_root_id": model["root_id"],
                                     "target_folder": folder})
        # The call may be refused (422/403) or accepted with the spec treated as
        # a literal folder name - what it must never do is land outside the root.
        assert response.status_code in (200, 403, 422), folder
        current = client.get(f"/api/v1/models/{model['id']}").json()
        assert Path(current["abs_path"]).is_relative_to(sec_comfyui), (
            f"{folder!r} moved the file to {current['abs_path']}")

    strays = (_snapshot(tmp_path) - _snapshot(sec_comfyui)) - outside_before
    strays = {s for s in strays if "vault.db" not in s and Path(s).name != "ComfyUI"}
    assert not strays, f"files appeared outside the root: {sorted(strays)[:5]}"


def test_move_to_an_absolute_target_folder_is_refused(indexed_client, tmp_path):
    client = indexed_client
    model = _first_model(client)
    response = client.post("/api/v1/fileops/move",
                           json={"uids": [model["uid"]],
                                 "target_root_id": model["root_id"],
                                 "target_folder": str(tmp_path / "ESCAPED")})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PATH_NOT_ALLOWED"


def test_move_rejects_a_literal_dotdot_component(indexed_client):
    client = indexed_client
    model = _first_model(client)
    response = client.post("/api/v1/fileops/move",
                           json={"uids": [model["uid"]],
                                 "target_root_id": model["root_id"],
                                 "target_folder": "../.."})
    assert response.status_code == 422
    assert "'..'" in response.json()["error"]["message"]


def test_move_to_an_unknown_root_is_refused(indexed_client):
    client = indexed_client
    model = _first_model(client)
    response = client.post("/api/v1/fileops/move",
                           json={"uids": [model["uid"]], "target_root_id": 9999,
                                 "target_folder": "x"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# fileops/rename
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_rename_refuses_anything_that_is_not_a_single_component(indexed_client,
                                                               sec_comfyui, name):
    client = indexed_client
    model = _first_model(client)
    response = client.post("/api/v1/fileops/rename",
                           json={"uid": model["uid"], "new_name": name})
    assert response.status_code == 422, f"{name!r} was accepted"
    assert response.json()["error"]["code"] in ("PATH_INVALID", "VALIDATION_ERROR")
    current = client.get(f"/api/v1/models/{model['id']}").json()
    assert Path(current["abs_path"]).is_relative_to(sec_comfyui)


def test_rename_never_creates_an_alternate_data_stream(indexed_client, sec_comfyui):
    """``x.safetensors:hidden`` must not produce a file with an ADS."""
    client = indexed_client
    model = _first_model(client)
    response = client.post("/api/v1/fileops/rename",
                           json={"uid": model["uid"],
                                 "new_name": "streamed.safetensors:hidden"})
    if response.status_code == 200:
        assert ":" not in Path(response.json()["new_path"]).name
    else:
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# fileops/create-folder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("folder", [
    "../../ESCAPED", "..\\ESCAPED", ".. /ESCAPED", "C:/Windows/Temp",
    "a/../../b", "CON", "trailing ", "trailing.",
])
def test_create_folder_refuses_traversal(indexed_client, sec_comfyui, tmp_path,
                                         folder):
    client = indexed_client
    roots = client.get("/api/v1/system/roots").json()["items"]
    root_id = roots[0]["id"]
    response = client.post("/api/v1/fileops/create-folder",
                           json={"root_id": root_id, "folder": folder})
    assert response.status_code in (403, 422), f"{folder!r} was accepted"
    assert not (tmp_path / "ESCAPED").exists()


def test_create_folder_accepts_a_plain_nested_folder(indexed_client, sec_comfyui):
    client = indexed_client
    roots = client.get("/api/v1/system/roots").json()["items"]
    response = client.post("/api/v1/fileops/create-folder",
                           json={"root_id": roots[0]["id"],
                                 "folder": "models/checkpoints/new-shelf"})
    assert response.status_code == 201
    assert Path(response.json()["path"]).is_relative_to(sec_comfyui)


# ---------------------------------------------------------------------------
# No client-supplied filesystem path reaches a file endpoint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", ["/api/v1/files/raw", "/api/v1/files/download",
                                      "/api/v1/files/thumbnail"])
def test_file_endpoints_require_a_uid_and_ignore_path(client, endpoint):
    """The deleted ``/api/outputs/file?path=`` shape must not have come back."""
    assert client.get(endpoint, params={"path": r"C:\Windows\win.ini"}
                      ).status_code == 422
    assert client.get(endpoint, params={"file": r"C:\Windows\win.ini"}
                      ).status_code == 422


@pytest.mark.parametrize("uid", [
    r"../../Windows/win.ini", r"model:../../x", "model:1;DROP", "model:-1",
    "output:1 OR 1=1", r"C:\Windows\win.ini", "model:99999999999999999999",
    "", "model:", ":1", "model:1:1",
])
def test_uid_shape_is_enforced(client, uid):
    response = client.get("/api/v1/files/raw", params={"uid": uid})
    assert response.status_code == 422
    assert response.json()["error"]["field_errors"][0]["field"] == "uid"


def test_no_endpoint_in_the_openapi_schema_takes_a_filesystem_path(client):
    """Only the three documented configuration endpoints may accept a path."""
    allowed = {
        ("/api/v1/system/config", "patch"),
        ("/api/v1/system/validate-path", "post"),
        ("/api/v1/system/wizard/complete", "post"),
        ("/api/v1/system/roots", "post"),
        ("/api/v1/comfyui/update/run", "post"),   # confirm_path, echo-only
    }
    spec = client.get("/openapi.json").json()
    offenders = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if (path, method) in allowed:
                continue
            names = {p.get("name", "") for p in op.get("parameters") or []}
            if names & {"path", "file", "filename", "abs_path", "dir", "folder_path"}:
                offenders.append((path, method, sorted(names)))
    assert not offenders, f"endpoints accepting a caller path: {offenders}"


# ---------------------------------------------------------------------------
# NTFS junctions - SECURITY_REVIEW finding S-01
# ---------------------------------------------------------------------------

def _junction(link: Path, target: Path) -> bool:
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return subprocess.run(  # noqa: S603
        [comspec, "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, check=False).returncode == 0


@pytest.fixture
def junctioned_install(client, sec_comfyui, tmp_path):
    outside = tmp_path / "OUTSIDE_THE_ROOT"
    outside.mkdir(exist_ok=True)
    (outside / "private.safetensors").write_bytes(b"\0" * 300_000)
    (outside / "evilnode.py").write_text(
        "NODE_CLASS_MAPPINGS = {'LeakedFromOutsideRoot': 'X'}\n", encoding="utf-8")
    package = sec_comfyui / "custom_nodes" / "pkg"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n",
                                         encoding="utf-8")
    made = _junction(sec_comfyui / "models" / "checkpoints" / "linked", outside)
    made = _junction(package / "linked", outside) and made
    if not made:
        pytest.skip("could not create an NTFS junction here")

    import time as _t

    from app.indexing.service import get_indexer

    indexer = get_indexer()
    indexer.start(mode="full", force=True, enrich_online=False, trigger="test")
    deadline = _t.monotonic() + 60
    while _t.monotonic() < deadline and indexer.status().get("running"):
        _t.sleep(0.05)
    return client, outside


def test_file_operations_cannot_reach_through_a_junction(junctioned_install):
    """Containment on the *write* path holds: realpath resolves the junction."""
    client, outside = junctioned_install
    leaked = [m for m in client.get("/api/v1/models", params={"limit": 200}
                                    ).json()["items"]
              if "linked" in (m["abs_path"] or "")]
    if not leaked:
        pytest.skip("the walker did not cross the junction on this filesystem")
    uid = leaked[0]["uid"]

    assert client.get("/api/v1/files/raw", params={"uid": uid}).status_code == 403
    assert client.get("/api/v1/files/download", params={"uid": uid}).status_code == 403
    assert client.post("/api/v1/fileops/rename",
                       json={"uid": uid, "new_name": "x.safetensors"}
                       ).status_code == 403

    deleted = client.post("/api/v1/fileops/delete", json={"uids": [uid]}).json()
    assert deleted["results"][0]["error"]["code"] == "PATH_NOT_ALLOWED"
    assert (outside / "private.safetensors").is_file(), (
        "a file outside every root was deleted through a junction")


# S-01 regression gate: this was an open finding and is now fixed.
# It must never be marked xfail again - a failure here is a reopened breach.
def test_indexing_walker_does_not_descend_a_junction(junctioned_install):
    client, _outside = junctioned_install
    models = client.get("/api/v1/models", params={"limit": 200}).json()["items"]
    assert not [m for m in models if "linked" in (m["abs_path"] or "")], (
        "the walker indexed a file that lives outside every configured root")


# S-01 regression gate: this was an open finding and is now fixed.
# It must never be marked xfail again - a failure here is a reopened breach.
def test_node_scanner_does_not_descend_a_junction(junctioned_install):
    client, _outside = junctioned_install
    classes = client.get("/api/v1/node-classes", params={"limit": 500}).json()["items"]
    names = {str(c.get("node_id") or c.get("name")) for c in classes}
    assert "LeakedFromOutsideRoot" not in names
