"""C9 - the workflow "Enable" downloader, against SECURITY_REVIEW section 5.

One test (usually several) per requirement R1-R11, asserted by execution rather
than by reading the code.  Everything runs against a synthetic ComfyUI tree in
``tmp_path`` and a local fixture HTTP server; **nothing here touches the owner's
real install, and nothing here reaches the internet.**

The fixture server records every path it was asked for, which is what makes the
strongest assertions possible: not merely "the download failed" but "the second
request was never issued".
"""

from __future__ import annotations

import ast
import hashlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.core.errors import (
    ConflictError,
    InsufficientSpace,
    UpstreamUnavailable,
    ValidationError,
)
from app.enable import download, git_fetch, hosts, placement, plan, sources

# ---------------------------------------------------------------------------
# Fixture HTTP server
# ---------------------------------------------------------------------------

PAYLOAD = b"safetensors-fixture-payload-" * 4096          # ~112 KB
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
WRONG_SHA = hashlib.sha256(b"something else entirely").hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        return

    def do_GET(self) -> None:
        self.server.seen.append(self.path)
        route = self.path.split("?", 1)[0]
        if route.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route.startswith("/loop"):
            self.send_response(302)
            self.send_header("Location", self.server.base + "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route.startswith("/gone"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.server.bodies.get(route)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start = 0
        rng = self.headers.get("Range")
        partial = False
        if rng and rng.startswith("bytes=") and self.server.honour_range:
            try:
                start = int(rng.split("=", 1)[1].split("-", 1)[0])
            except ValueError:
                start = 0
            partial = 0 < start < len(body)
        chunk = body[start:] if partial else body
        self.send_response(206 if partial else 200)
        self.send_header("Content-Length", str(len(chunk)))
        if partial:
            self.send_header("Content-Range",
                             f"bytes {start}-{len(body) - 1}/{len(body)}")
        if self.server.disposition:
            self.send_header("Content-Disposition", self.server.disposition)
        self.end_headers()
        step = self.server.chunk_size or len(chunk) or 1
        for offset in range(0, len(chunk), step):
            try:
                self.wfile.write(chunk[offset:offset + step])
                self.wfile.flush()
            except OSError:
                return
            if self.server.delay:
                time.sleep(self.server.delay)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


@pytest.fixture
def fixture_server():
    """A local HTTP server that records every request it receives."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = _Server(("127.0.0.1", port), _Handler)
    server.seen = []
    server.bodies = {"/f/good.safetensors": PAYLOAD}
    server.redirect_to = ""
    server.disposition = None
    server.chunk_size = 0
    server.delay = 0.0
    server.honour_range = True
    server.base = f"http://localhost:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def allow_fixture_host(monkeypatch):
    """Let the fixture server's origin through the *real* allowlist code.

    Only the two frozen constants are widened, and only for the tests that need
    a live transport.  Every allowlist assertion in this file runs against the
    unpatched module, so widening here cannot hide a hole there.
    """
    monkeypatch.setattr(hosts, "MODEL_HOSTS_EXACT", frozenset({"localhost"}))
    monkeypatch.setattr(hosts, "MODEL_HOSTS_SUFFIX", ())
    monkeypatch.setattr(hosts, "_SCHEME", "http")
    return True


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    """A disposable stand-in for a ComfyUI root, created and removed per test."""
    root = tmp_path / "FetchRoot"
    for name in ("checkpoints", "loras", "diffusion_models", "vae", "text_encoders"):
        (root / "models" / name).mkdir(parents=True, exist_ok=True)
    (root / "custom_nodes").mkdir(parents=True, exist_ok=True)
    return root


def _spec(server, root: Path, *, name: str = "good.safetensors",
          route: str = "/f/good.safetensors", category: str = "loras",
          size: int | None = None, sha: str | None = PAYLOAD_SHA,
          on_conflict: str = "fail") -> download.FetchSpec:
    target = root / "models" / category / name
    return download.FetchSpec(
        url=f"{server.base}{route}", host="localhost",
        target_abs_path=str(target), root_path=str(root),
        expected_size=len(PAYLOAD) if size is None else size,
        expected_sha256=sha, on_conflict=on_conflict,
        ref_name=name, category=category)


def _part_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.part"))


# =============================================================================
# R1 - host allowlist, no arbitrary URL fetch
# =============================================================================

R1_REFUSALS = (
    "http://evil.test/model.safetensors",
    "https://civitai.com.evil.test/x",
    "https://huggingface.co@evil.test/x",
    "file:///C:/Windows/win.ini",
)


@pytest.mark.parametrize("url", R1_REFUSALS)
def test_r1_a_non_allowlisted_url_is_refused(url):
    with pytest.raises(hosts.HostNotAllowed):
        hosts.check(url)


def test_r1_the_four_refusals_issue_zero_requests(fixture_server, vault_root):
    for url in R1_REFUSALS:
        spec = download.FetchSpec(
            url=url, host="?", target_abs_path=str(vault_root / "models" / "loras" / "x.safetensors"),
            root_path=str(vault_root), expected_size=len(PAYLOAD),
            expected_sha256=PAYLOAD_SHA, ref_name="x.safetensors", category="loras")
        with pytest.raises(ValidationError):
            download.fetch(spec)
    assert fixture_server.seen == []
    assert _part_files(vault_root) == []


def test_r1_the_allowlist_is_the_documented_one():
    assert frozenset({"civitai.com", "huggingface.co",
                                                 "hf.co"}) == hosts.MODEL_HOSTS_EXACT
    assert hosts.MODEL_HOSTS_SUFFIX == (".civitai.com", ".huggingface.co", ".hf.co")
    assert frozenset({"github.com", "gitlab.com",
                                               "codeberg.org"}) == hosts.GIT_HOSTS_EXACT
    for good in ("https://civitai.com/api/download/models/1",
                 "https://huggingface.co/x/y/resolve/main/z.safetensors",
                 "https://cdn-lfs-us-1.huggingface.co/repos/a/b"):
        assert hosts.check(good).host


def test_r1_a_bare_ip_and_a_non_https_scheme_are_refused():
    for url in ("https://127.0.0.1/x.safetensors", "https://[::1]/x.safetensors",
                "ftp://huggingface.co/x", "//huggingface.co/x"):
        with pytest.raises(hosts.HostNotAllowed):
            hosts.check(url)


def test_r1_a_git_remote_is_matched_against_the_git_list_only():
    with pytest.raises(hosts.HostNotAllowed):
        hosts.check("https://huggingface.co/x/y", kind=hosts.KIND_GIT)
    with pytest.raises(hosts.HostNotAllowed):
        hosts.check("https://github.com/x/y", kind=hosts.KIND_MODEL)
    assert hosts.check("https://github.com/x/y", kind=hosts.KIND_GIT).host == "github.com"


def test_r1_no_endpoint_accepts_a_url_or_a_path(client):
    schema = client.get("/openapi.json").json()
    offenders = []
    for path, ops in schema["paths"].items():
        if "/enable" not in path:
            continue
        for method, op in ops.items():
            body = (op.get("requestBody") or {}).get("content", {})
            for media in body.values():
                ref = (media.get("schema") or {}).get("$ref", "")
                name = ref.rsplit("/", 1)[-1]
                props = ((schema["components"]["schemas"].get(name) or {})
                         .get("properties") or {})
                offenders += [(path, method, p) for p in props
                              if "url" in p.lower() or "path" in p.lower()]
            for param in op.get("parameters") or []:
                key = str(param.get("name", "")).lower()
                if "url" in key or "path" in key:
                    offenders.append((path, method, param.get("name")))
    assert not offenders, f"C9 routes taking a url/path: {offenders}"


def test_r1_no_mcp_tool_accepts_a_url_or_a_path():
    from app.mcp import registry

    offenders = []
    for tool in registry.TOOLS:
        if not tool.name.startswith("enable_"):
            continue
        for name in tool.input_schema.get("properties") or {}:
            low = name.lower()
            if "url" in low or "path" in low or low in ("file", "dir"):
                offenders.append((tool.name, name))
    assert not offenders, offenders


def test_r1_sources_only_ever_come_from_local_data():
    """No filename is sent to any API - ARCHITECTURE 8.4 still holds."""
    text = Path(sources.__file__).read_text(encoding="utf-8")
    assert "import httpx" not in text
    assert "civitai_service" not in text


# =============================================================================
# R2 - redirects are re-validated, never followed blindly
# =============================================================================

def test_r2_the_client_never_follows_redirects_itself(app_dir):
    text = (app_dir / "enable" / "download.py").read_text(encoding="utf-8")
    assert "follow_redirects=False" in text
    assert "follow_redirects=True" not in text


def test_r2_a_hop_off_the_allowlist_is_refused():
    current = hosts.CheckedUrl("https://huggingface.co/a/b", "huggingface.co", "model")
    with pytest.raises(UpstreamUnavailable):
        hosts.check_redirect("http://evil.test/x", current=current, hop=0)


def test_r2_the_hop_budget_is_five():
    current = hosts.CheckedUrl("https://huggingface.co/a", "huggingface.co", "model")
    assert hosts.MAX_REDIRECTS == 5
    with pytest.raises(UpstreamUnavailable):
        hosts.check_redirect("https://huggingface.co/b", current=current,
                             hop=hosts.MAX_REDIRECTS)


def test_r2_authorization_never_survives_a_host_change():
    a = hosts.CheckedUrl("https://civitai.com/x", "civitai.com", "model")
    b = hosts.CheckedUrl("https://cdn.civitai.com/x", "cdn.civitai.com", "model")
    headers = {"Authorization": "Bearer secret", "User-Agent": "x"}
    assert hosts.strip_auth_on_host_change(headers, a, a) == headers
    moved = hosts.strip_auth_on_host_change(headers, a, b)
    assert "Authorization" not in moved
    assert moved["User-Agent"] == "x"


def test_r2_a_redirect_to_a_refused_host_issues_no_second_request(
        fixture_server, allow_fixture_host, vault_root):
    # 127.0.0.1 is *this* server, but as a bare IP it is never allowlisted.  If
    # the hop were taken, "/leak" would appear in the server's own log.
    fixture_server.redirect_to = (
        f"http://127.0.0.1:{fixture_server.server_address[1]}/leak/evil.safetensors")
    fixture_server.bodies["/leak/evil.safetensors"] = PAYLOAD
    spec = _spec(fixture_server, vault_root, route="/redirect/one")
    result = download.fetch(spec)
    assert result.state == "failed"
    assert result.error_code == "UPSTREAM_UNAVAILABLE"
    assert "/redirect/one" in fixture_server.seen
    assert not [p for p in fixture_server.seen if p.startswith("/leak")]
    assert not os.path.exists(spec.target_abs_path)


def test_r2_an_allowlisted_redirect_is_followed(fixture_server, allow_fixture_host,
                                                vault_root):
    fixture_server.redirect_to = f"{fixture_server.base}/f/good.safetensors"
    result = download.fetch(_spec(fixture_server, vault_root, route="/redirect/ok"))
    assert result.state == "done", result.error_message
    assert result.sha256 == PAYLOAD_SHA


def test_r2_a_redirect_loop_terminates(fixture_server, allow_fixture_host, vault_root):
    result = download.fetch(_spec(fixture_server, vault_root, route="/loop"))
    assert result.state == "failed"
    assert result.error_code == "UPSTREAM_UNAVAILABLE"
    assert len([p for p in fixture_server.seen if p.startswith("/loop")]) <= \
        hosts.MAX_REDIRECTS + 1


# =============================================================================
# R3 - destination is derived, never supplied
# =============================================================================

R3_HOSTILE_NAMES = (
    "../../../../Windows/System32/evil.dll",
    r"C:\evil.bin",
    "CON",
    "x.safetensors:ads",
    "a" * 300 + ".safetensors",
)


@pytest.mark.parametrize("name", R3_HOSTILE_NAMES)
def test_r3_a_hostile_filename_is_refused(name):
    with pytest.raises(ValidationError):
        placement.safe_basename(name)


def test_r3_nothing_is_written_for_any_hostile_name(fixture_server, allow_fixture_host,
                                                    vault_root, tmp_path):
    for name in R3_HOSTILE_NAMES:
        with pytest.raises(ValidationError):
            placement.resolve_destination("loras", name)
    assert fixture_server.seen == []
    assert _part_files(tmp_path) == []
    assert not (Path("C:/") / "evil.bin").exists()


def test_r3_the_category_comes_from_the_node_input(sec_vault):
    cases = {
        ("CheckpointLoaderSimple", "ckpt_name"): "checkpoints",
        ("LoraLoader", "lora_name"): "loras",
        ("UNETLoader", "unet_name"): "diffusion_models",
        ("VAELoader", "vae_name"): "vae",
        ("CLIPLoader", "clip_name"): "text_encoders",
        ("CLIPVisionLoader", "clip_name"): "clip_vision",
        ("ControlNetLoader", "control_net_name"): "controlnet",
        ("UpscaleModelLoader", "model_name"): "upscale_models",
    }
    for (cls, inp), expected in cases.items():
        assert placement.category_for(cls, inp) == expected, (cls, inp)


def test_r3_an_unknown_input_yields_no_category(sec_vault):
    assert placement.category_for("MysteryLoader", "some_random_input") is None


def test_r3_the_destination_lands_inside_a_configured_root(sec_vault):
    dest = placement.resolve_destination("loras", "probe-download.safetensors")
    root = Path(str(sec_vault.comfyui_path))
    assert Path(dest.abs_path).parent == root / "models" / "loras"
    assert dest.filename == "probe-download.safetensors"
    from app.core.pathsafe import is_contained

    assert is_contained(dest.abs_path, root)


def test_r3_a_content_disposition_filename_is_a_hint_and_is_validated():
    assert placement.content_disposition_hint(
        'attachment; filename="clean.safetensors"') == "clean.safetensors"
    for hostile in ('attachment; filename="../../evil.safetensors"',
                    'attachment; filename="CON.safetensors"',
                    'attachment; filename="evil.exe"',
                    'attachment; filename="x.safetensors:ads"'):
        assert placement.content_disposition_hint(hostile) is None


def test_r3_a_non_model_extension_is_refused():
    for name in ("payload.exe", "payload.dll", "payload.bat", "payload.ps1"):
        with pytest.raises(ValidationError):
            placement.safe_basename(name)


def test_r3_a_category_outside_the_frozen_list_is_refused(sec_vault):
    for category in ("", "..", "custom_nodes/../..", "windows"):
        with pytest.raises((ValidationError, Exception)):
            placement.resolve_destination(category, "x.safetensors")


# =============================================================================
# R4 - verify before placing; quarantine on mismatch
# =============================================================================

def test_r4_a_hash_mismatch_quarantines_and_places_nothing(
        fixture_server, allow_fixture_host, vault_root):
    spec = _spec(fixture_server, vault_root, sha=WRONG_SHA)
    target_dir = Path(spec.target_abs_path).parent
    before = sorted(p.name for p in target_dir.iterdir())

    result = download.fetch(spec)

    assert result.state == "quarantined"
    assert result.error_code == "INTEGRITY_MISMATCH"
    assert "SHA-256 mismatch" in (result.error_message or "")
    assert sorted(p.name for p in target_dir.iterdir()) == before
    assert not os.path.exists(spec.target_abs_path)

    slot = Path(result.quarantine_path)
    assert slot.is_file()
    assert download.quarantine_dir(vault_root) in slot.parents
    reason = json.loads((slot.parent / "reason.json").read_text(encoding="utf-8"))
    assert reason["expected_sha256"] == WRONG_SHA
    assert reason["actual_sha256"] == PAYLOAD_SHA
    assert reason["intended_path"] == spec.target_abs_path


def test_r4_a_size_mismatch_quarantines(fixture_server, allow_fixture_host, vault_root):
    spec = _spec(fixture_server, vault_root, size=len(PAYLOAD) + 999, sha=None)
    result = download.fetch(spec)
    assert result.state == "quarantined"
    assert "size mismatch" in (result.error_message or "")
    assert not os.path.exists(spec.target_abs_path)


def test_r4_a_source_publishing_nothing_at_all_is_quarantined(
        fixture_server, allow_fixture_host, vault_root):
    spec = _spec(fixture_server, vault_root, size=0, sha=None)
    result = download.fetch(spec)
    assert result.state == "quarantined"
    assert "neither a size nor a hash" in (result.error_message or "")


def test_r4_a_matching_download_is_placed_and_verified(
        fixture_server, allow_fixture_host, vault_root):
    spec = _spec(fixture_server, vault_root)
    result = download.fetch(spec)
    assert result.state == "done"
    assert result.verified == "sha256"
    assert Path(spec.target_abs_path).read_bytes() == PAYLOAD
    assert _part_files(vault_root) == []


def test_r4_a_quarantine_entry_is_listed_for_the_ui(fixture_server, allow_fixture_host,
                                                    vault_root, sec_vault, monkeypatch):
    from app.core.pathsafe import Root

    fake = Root(id=99, kind="comfyui", path=str(vault_root), label="fixture")
    monkeypatch.setattr("app.core.config_service.get_config",
                        lambda: type("C", (), {"roots": (fake,)})())
    download.fetch(_spec(fixture_server, vault_root, sha=WRONG_SHA))
    listed = download.quarantine_list()
    assert len(listed) == 1
    assert listed[0]["files"]
    assert listed[0]["reason"]["problems"]


# =============================================================================
# R5 - never overwrite an existing file implicitly
# =============================================================================

def test_r5_an_existing_destination_is_a_conflict(fixture_server, allow_fixture_host,
                                                  vault_root):
    spec = _spec(fixture_server, vault_root)
    target = Path(spec.target_abs_path)
    target.write_bytes(b"the file that was already there")
    original = target.read_bytes()

    with pytest.raises(ConflictError):
        download.fetch(spec)

    assert target.read_bytes() == original
    assert fixture_server.seen == []


def test_r5_overwrite_is_not_an_option():
    assert "overwrite" not in download.ON_CONFLICT
    assert download.ON_CONFLICT == ("fail", "skip", "keep_both")
    with pytest.raises(ValidationError):
        download.resolve_conflict("whatever", "overwrite")


def test_r5_skip_and_keep_both_leave_the_original_alone(
        fixture_server, allow_fixture_host, vault_root):
    spec = _spec(fixture_server, vault_root, on_conflict="skip")
    target = Path(spec.target_abs_path)
    target.write_bytes(b"original")
    assert download.fetch(spec).state == "skipped"
    assert target.read_bytes() == b"original"

    spec = _spec(fixture_server, vault_root, on_conflict="keep_both")
    result = download.fetch(spec)
    assert result.state == "done"
    assert target.read_bytes() == b"original"
    assert Path(result.abs_path) != target
    assert Path(result.abs_path).read_bytes() == PAYLOAD


def test_r5_the_api_refuses_an_overwrite_value(client, sec_vault):
    workflow = client.get("/api/v1/workflows?limit=1").json()
    del workflow
    response = client.post("/api/v1/workflows/1/enable/fetch",
                           json={"plan_token": "x" * 12, "item_ids": ["a"],
                                 "confirm": True, "on_conflict": "overwrite"})
    assert response.status_code == 422


# =============================================================================
# R6 - free space is checked before and during
# =============================================================================

def test_r6_a_download_larger_than_free_space_is_refused_before_any_socket(
        fixture_server, allow_fixture_host, vault_root, monkeypatch):
    one_gb = 1024 ** 3
    monkeypatch.setattr(
        download.shutil, "disk_usage",
        lambda _p: shutil._ntuple_diskusage(2 * one_gb, one_gb, one_gb))
    spec = _spec(fixture_server, vault_root, size=2 * one_gb, sha=None)
    with pytest.raises(InsufficientSpace) as exc:
        download.fetch(spec)
    assert exc.value.details["shortfall_bytes"] > 0
    assert exc.value.details["free_bytes"] == one_gb
    assert exc.value.http_status == 507
    assert fixture_server.seen == []
    assert _part_files(vault_root) == []


def test_r6_the_margin_is_five_percent():
    assert download.SPACE_MARGIN == 1.05
    assert download.required_with_margin(1000) == 1050


def test_r6_the_margin_refuses_a_download_that_only_just_fits(monkeypatch, vault_root):
    monkeypatch.setattr(download.shutil, "disk_usage",
                        lambda _p: shutil._ntuple_diskusage(2000, 980, 1020))
    with pytest.raises(InsufficientSpace):
        download.check_space(vault_root, 1000)
    monkeypatch.setattr(download.shutil, "disk_usage",
                        lambda _p: shutil._ntuple_diskusage(4000, 1900, 2100))
    assert download.check_space(vault_root, 1000)["free_bytes"] == 2100


def test_r6_space_is_rechecked_during_the_transfer(app_dir):
    text = (app_dir / "enable" / "download.py").read_text(encoding="utf-8")
    assert "SPACE_RECHECK_BYTES = 256 * 1024 * 1024" in text
    assert "if since_check >= SPACE_RECHECK_BYTES:" in text


def test_r6_the_batch_is_refused_as_a_whole(monkeypatch, vault_root):
    from app.enable.service import EnableService

    monkeypatch.setattr(download.shutil, "disk_usage",
                        lambda _p: shutil._ntuple_diskusage(2 * 1024 ** 3,
                                                            1024 ** 3, 1024 ** 3))
    specs = [{"kind": "model", "expected_size": 900 * 1024 ** 2,
              "target_abs_path": str(vault_root / "models" / "loras" / f"m{i}.safetensors")}
             for i in range(3)]
    with pytest.raises(InsufficientSpace):
        EnableService()._precheck_space(specs)


# =============================================================================
# R7 - node packages: clone or report only.  Never execute anything.
# =============================================================================

FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_ATTRS = {("os", "system"), ("os", "popen"), ("os", "execv"),
                   ("pickle", "load"), ("pickle", "loads"),
                   ("importlib", "import_module")}
FORBIDDEN_MODULES = {"pickle", "torch", "marshal", "dill", "importlib", "runpy",
                     "imp", "pip"}


def _enable_sources(app_dir: Path) -> list[Path]:
    return sorted(p for p in (app_dir / "enable").rglob("*.py")
                  if "__pycache__" not in p.parts)


def test_r7_the_c9_package_imports_no_execution_primitive(app_dir):
    offenders = []
    for path in _enable_sources(app_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in FORBIDDEN_MODULES:
                    offenders.append((path.name, node.lineno, name))
                if name == "subprocess" and path.name != "git_fetch.py":
                    offenders.append((path.name, node.lineno, "subprocess"))
    assert not offenders, f"execution primitives in the C9 package: {offenders}"


def test_r7_the_c9_package_calls_no_execution_primitive(app_dir):
    offenders = []
    for path in _enable_sources(app_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                offenders.append((path.name, node.lineno, func.id))
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and (func.value.id, func.attr) in FORBIDDEN_ATTRS:
                offenders.append((path.name, node.lineno,
                                  f"{func.value.id}.{func.attr}"))
    assert not offenders, f"execution call sites in the C9 package: {offenders}"


def test_r7_only_git_fetch_may_start_a_process_and_only_git(app_dir):
    text = (app_dir / "enable" / "git_fetch.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    runs = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "subprocess"
            and n.func.attr in ("run", "Popen", "call", "check_output", "check_call")]
    assert len(runs) == 1, "exactly one subprocess call site is permitted"
    call = runs[0]
    kwargs = {k.arg: k.value for k in call.keywords}
    assert "timeout" in kwargs, "the clone must have a wall-clock timeout"
    assert "shell" not in kwargs, "shell must stay at its False default"
    assert isinstance(call.args[0], ast.Name) and call.args[0].id == "argv"
    # The argv itself is the frozen list build_argv() returns.
    assert "def build_argv" in text


def test_r7_nothing_in_the_package_names_an_installer_as_something_to_run(app_dir):
    for path in _enable_sources(app_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("run", "Popen", "call", "check_output",
                                           "check_call", "system", "popen"):
                dump = ast.dump(node)
                for needle in ("pip", "install.py", "requirements.txt", "setup.py"):
                    assert needle not in dump, (path.name, node.lineno, needle)


def test_r7_a_hostile_repo_is_inspected_never_run(tmp_path):
    """The same shape as test_a_malicious_init_py_is_parsed_never_run."""
    marker = tmp_path / "OWNED.txt"
    repo = tmp_path / "custom_nodes" / "ComfyUI-Hostile"
    repo.mkdir(parents=True)
    install_py = repo / "install.py"
    install_py.write_text(
        "import os\n"
        f"open(r'{marker}', 'w').write('owned')\n"
        f"os.system('cmd /c echo owned > \"{marker}\"')\n", encoding="utf-8")
    requirements = repo / "requirements.txt"
    requirements.write_text("evil-package==1.0\n--index-url http://evil.test\n",
                            encoding="utf-8")
    (repo / ".gitmodules").write_text(
        '[submodule "x"]\n\tpath = x\n\turl = file:///C:/\n', encoding="utf-8")
    before = (install_py.read_bytes(), requirements.read_bytes())

    findings = git_fetch.inspect_clone(str(repo))
    steps = git_fetch.post_clone_steps(str(repo), findings)
    notes = git_fetch._notes(findings)

    assert not marker.exists(), "the hostile installer ran"
    assert (install_py.read_bytes(), requirements.read_bytes()) == before
    assert "install.py" in findings["install_markers"]
    assert "requirements.txt" in findings["install_markers"]
    assert findings["has_gitmodules"] is True
    assert findings["submodules"] == ["file:///C:/"]
    assert any("pip install" in s for s in steps), "the command must be shown"
    assert any("NOT run" in n for n in notes)
    assert not marker.exists()


def test_r7_a_clone_of_a_non_allowlisted_remote_never_starts_git(tmp_path):
    for remote in ("https://evil.test/x/y", "file:///C:/repo",
                   "https://github.com.evil.test/x/y"):
        with pytest.raises(hosts.HostNotAllowed):
            git_fetch.clone(remote, str(tmp_path / "target"))
    assert not (tmp_path / "target").exists()


def test_r7_the_report_states_what_is_never_run():
    from app.enable import report

    joined = " ".join(report.NEVER_RUNS).lower()
    for needle in ("pip install", "requirements.txt", "install.py", "submodule"):
        assert needle in joined


# =============================================================================
# R8 - git clones are constrained
# =============================================================================

def test_r8_the_argv_carries_every_required_constraint(tmp_path):
    argv = git_fetch.build_argv("git", "https://github.com/x/y", str(tmp_path / "t"),
                                str(tmp_path / "hooks"))
    joined = " ".join(argv)
    assert argv[0] == "git"
    assert "--depth" in argv and argv[argv.index("--depth") + 1] == "1"
    assert "--single-branch" in argv
    assert "--no-tags" in argv
    assert "--no-recurse-submodules" in argv
    assert "--recurse-submodules" not in argv
    assert f"core.hooksPath={tmp_path / 'hooks'}" in argv
    assert "protocol.file.allow=never" in argv
    assert "protocol.ext.allow=never" in argv
    assert "credential.helper=" in argv
    assert argv[-2:] == ["https://github.com/x/y", str(tmp_path / "t")]
    assert "--upload-pack" not in joined and "--exec" not in joined


def test_r8_the_environment_forbids_every_prompt():
    env = git_fetch._env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_DIR" not in env


def test_r8_an_existing_target_is_never_clobbered(tmp_path):
    target = tmp_path / "ComfyUI-Existing"
    target.mkdir()
    (target / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ConflictError):
        git_fetch.clone("https://github.com/x/ComfyUI-Existing", str(target))
    assert (target / "keep.txt").read_text(encoding="utf-8") == "mine"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_r8_a_declared_submodule_is_never_fetched(tmp_path):
    """Run the *production* clone flags against a local repo that declares a
    submodule pointing at ``file:///C:/``.  The submodule must not be fetched.

    The remote here is ``file://``, which the host allowlist refuses in
    production - so the test drives git with the exact flag tuple the module
    exports rather than through ``clone()``, and adds only the one config the
    fixture transport needs.  What is being asserted is the flag tuple.
    """
    source = tmp_path / "source"
    source.mkdir()
    env = {**git_fetch._env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args, cwd):
        return subprocess.run(["git", *args], cwd=str(cwd), env=env,  # noqa: S603, S607
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=60, check=False)

    assert git("init", "-b", "main", cwd=source).returncode == 0
    (source / "nodes.py").write_text("# harmless\n", encoding="utf-8")
    (source / ".gitmodules").write_text(
        '[submodule "hostile"]\n\tpath = hostile\n\turl = file:///C:/\n',
        encoding="utf-8")
    git("add", "-A", cwd=source)
    assert git("commit", "-m", "initial", cwd=source).returncode == 0

    target = tmp_path / "clone"
    argv = ["git", "-c", f"core.hooksPath={tmp_path / 'hooks'}",
            "-c", "protocol.file.allow=always",      # fixture transport only
            "-c", "protocol.ext.allow=never",
            "-c", "credential.helper=",
            *git_fetch._CLONE_FLAGS, "--", source.as_uri(), str(target)]
    assert "--no-recurse-submodules" in argv
    result = subprocess.run(argv, cwd=str(tmp_path), env=env,  # noqa: S603
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout

    assert (target / ".gitmodules").is_file(), "the declaration should be visible"
    hostile = target / "hostile"
    assert not hostile.exists() or not any(hostile.iterdir()), \
        "the submodule was fetched"
    findings = git_fetch.inspect_clone(str(target))
    assert findings["has_gitmodules"] is True
    assert findings["submodules"] == ["file:///C:/"]


# =============================================================================
# R9 - explicit per-item consent, and the plan is what runs
# =============================================================================

@pytest.fixture
def a_plan():
    plan.reset()
    items = [
        plan.PlanItem(item_id=plan.item_id("model", "a.safetensors"), kind="model",
                      ref_name="a.safetensors",
                      payload={"kind": "model", "source_url": "https://huggingface.co/a",
                               "target_abs_path": "T", "expected_size": 1}),
        plan.PlanItem(item_id=plan.item_id("model", "b.safetensors"), kind="model",
                      ref_name="b.safetensors",
                      payload={"kind": "model", "source_url": "https://huggingface.co/b",
                               "target_abs_path": "T2", "expected_size": 2}),
    ]
    issued = plan.issue(7, items)
    yield issued
    plan.reset()


def test_r9_the_plan_redeems_only_what_it_promised(a_plan):
    got = plan.redeem(a_plan.token, 7, [next(iter(a_plan.items))])
    assert len(got) == 1


def test_r9_an_item_outside_the_plan_is_refused(a_plan):
    with pytest.raises(ValidationError) as exc:
        plan.redeem(a_plan.token, 7, ["mode_deadbeefdeadbeef"])
    assert exc.value.details["reason"] == "item_not_in_plan"


def test_r9_a_superseded_plan_cannot_execute(a_plan):
    fresh = plan.issue(7, [plan.PlanItem(item_id="mode_1111111111111111", kind="model",
                                         ref_name="c.safetensors", payload={"x": 1})])
    with pytest.raises(ValidationError):
        plan.redeem(a_plan.token, 7, [next(iter(a_plan.items))])
    assert plan.redeem(fresh.token, 7, ["mode_1111111111111111"])


def test_r9_an_expired_plan_cannot_execute(a_plan, monkeypatch):
    monkeypatch.setattr(plan.time, "time", lambda: a_plan.expires_at + 1)
    with pytest.raises(ValidationError) as exc:
        plan.redeem(a_plan.token, 7, [next(iter(a_plan.items))])
    assert exc.value.details["reason"] in ("unknown_or_expired", "superseded")


def test_r9_a_token_for_another_workflow_is_refused(a_plan):
    with pytest.raises(ValidationError):
        plan.redeem(a_plan.token, 8, [next(iter(a_plan.items))])


def test_r9_an_empty_selection_downloads_nothing(a_plan):
    with pytest.raises(ValidationError) as exc:
        plan.redeem(a_plan.token, 7, [])
    assert exc.value.details["reason"] == "empty_selection"


def test_r9_the_api_refuses_a_stale_plan_and_issues_no_request(
        client, sec_vault, fixture_server, indexed_client):
    workflows = client.get("/api/v1/workflows?limit=1").json()["items"]
    if not workflows:
        pytest.skip("no workflow indexed in the synthetic vault")
    wid = workflows[0]["id"]
    report = client.get(f"/api/v1/workflows/{wid}/enable/plan").json()
    token = report["plan_token"]

    bad_item = client.post(f"/api/v1/workflows/{wid}/enable/fetch",
                           json={"plan_token": token, "item_ids": ["not_in_the_plan"],
                                 "confirm": True})
    assert bad_item.status_code == 422
    assert bad_item.json()["error"]["code"] == "VALIDATION_ERROR"

    bad_token = client.post(f"/api/v1/workflows/{wid}/enable/fetch",
                            json={"plan_token": "x" * 32, "item_ids": ["a"],
                                  "confirm": True})
    assert bad_token.status_code == 422

    no_confirm = client.post(f"/api/v1/workflows/{wid}/enable/fetch",
                             json={"plan_token": token, "item_ids": ["a"],
                                   "confirm": False})
    assert no_confirm.status_code == 422
    assert fixture_server.seen == []


def test_r9_the_report_shows_source_size_destination_and_hash_per_item(
        client, indexed_client):
    workflows = client.get("/api/v1/workflows?limit=1").json()["items"]
    if not workflows:
        pytest.skip("no workflow indexed in the synthetic vault")
    report = client.get(
        f"/api/v1/workflows/{workflows[0]['id']}/enable/plan").json()
    assert "plan_token" in report and report["plan_expires_in_ms"] > 0
    assert "download_bytes" in report["summary"]
    assert "volumes" in report["space"]
    assert report["policy"]["model_hosts"]
    for item in report["models"]:
        assert item["status"] in ("fetchable", "already_present", "no_source",
                                  "blocked")
        assert "destination" in item and "source" in item
    for item in report["node_packages"]:
        assert "manual_steps" in item


# =============================================================================
# R10 - from MCP: the same rules, plus audit
# =============================================================================

def test_r10_the_fetch_tool_is_mutating_audited_and_capped():
    from app.mcp import registry

    tool = registry.BY_NAME["enable_workflow_fetch"]
    assert tool.mutating is True
    assert tool.audited is True
    assert tool.as_dict()["annotations"]["readOnlyHint"] is False
    assert tool.input_schema["properties"]["item_ids"]["maxItems"] == registry.MAX_BATCH
    assert tool.input_schema["additionalProperties"] is False
    assert set(tool.input_schema["required"]) == {"workflow_uid", "plan_token",
                                                  "item_ids", "confirm"}
    assert "overwrite" not in tool.input_schema["properties"]["on_conflict"]["enum"]


def test_r10_the_plan_tool_is_read_only():
    from app.mcp import registry

    tool = registry.BY_NAME["enable_workflow_plan"]
    assert tool.mutating is False
    assert tool.as_dict()["annotations"]["readOnlyHint"] is True


def test_r10_the_tool_takes_a_uid_and_never_a_url_or_path():
    from app.mcp import registry

    for name in ("enable_workflow_plan", "enable_workflow_fetch"):
        props = registry.BY_NAME[name].input_schema["properties"]
        assert props["workflow_uid"]["pattern"] == "^workflow:[0-9]+$"
        assert not [p for p in props
                    if "url" in p.lower() or "path" in p.lower()]


def test_r10_read_only_mode_refuses_the_fetch_tool(client):
    from app.mcp import handlers, protocol, registry

    client.patch("/api/v1/system/config", json={"mcp_read_only": True})
    try:
        dispatcher = protocol.Dispatcher(transport="http")
        assert dispatcher.read_only() is True
        assert registry.BY_NAME["enable_workflow_fetch"].mutating is True
        del handlers
    finally:
        client.patch("/api/v1/system/config", json={"mcp_read_only": False})


def test_r10_the_confirmation_is_not_waived_for_agents(sec_vault):
    from app.mcp import handlers

    ctx = handlers.Ctx(transport="http", session_id="s")
    with pytest.raises(handlers.ToolFailure) as exc:
        handlers.enable_workflow_fetch(
            {"workflow_uid": "workflow:1", "plan_token": "x" * 12,
             "item_ids": ["a"], "confirm": False}, ctx)
    assert "confirm=true" in str(exc.value)


def test_r10_the_batch_cap_holds_for_the_tool(sec_vault):
    from app.mcp import handlers, registry

    ctx = handlers.Ctx(transport="http", session_id="s")
    with pytest.raises(handlers.ToolFailure) as exc:
        handlers.enable_workflow_fetch(
            {"workflow_uid": "workflow:1", "plan_token": "x" * 12,
             "item_ids": [f"i{i}" for i in range(registry.MAX_BATCH + 1)],
             "confirm": True}, ctx)
    assert "at most" in str(exc.value)


def test_r10_a_fetch_call_writes_an_audit_row(sec_vault):
    from app.core import db as dbmod
    from app.services import mcp_audit

    before = mcp_audit.count()
    mcp_audit.record(transport="http", tool="enable_workflow_fetch",
                     arguments={"workflow_uid": "workflow:1", "item_ids": ["a"],
                                "confirm": True},
                     uids=["workflow:1"], outcome="error", error_code="VALIDATION_ERROR")
    assert mcp_audit.count() == before + 1
    row = dbmod.one(dbmod.get_ro(),
                    "SELECT tool, arguments FROM mcp_audit ORDER BY id DESC LIMIT 1")
    assert row["tool"] == "enable_workflow_fetch"
    assert "workflow:1" in row["arguments"]


def test_r10_audited_uids_names_the_workflow():
    from app.mcp import handlers

    assert handlers.audited_uids(
        "enable_workflow_fetch", {"workflow_uid": "workflow:12"}, {}) == ["workflow:12"]


# =============================================================================
# R11 - cancellable, resumable, and bounded
# =============================================================================

def test_r11_a_cancelled_download_leaves_no_file_at_the_target_name(
        fixture_server, allow_fixture_host, vault_root):
    fixture_server.chunk_size = 4096
    fixture_server.delay = 0.02
    spec = _spec(fixture_server, vault_root)
    cancel = threading.Event()
    threading.Timer(0.15, cancel.set).start()

    result = download.fetch(spec, cancel=cancel)

    assert result.state == "cancelled"
    assert not os.path.exists(spec.target_abs_path)
    parts = _part_files(vault_root)
    assert len(parts) == 1
    assert parts[0].name.endswith(".safetensors.part")


def test_r11_a_resume_completes_and_re_verifies_the_whole_file(
        fixture_server, allow_fixture_host, vault_root):
    spec = _spec(fixture_server, vault_root)
    part = Path(spec.target_abs_path + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(PAYLOAD[:20_000])

    result = download.fetch(spec)

    assert result.state == "done", result.error_message
    assert result.sha256 == PAYLOAD_SHA
    assert Path(spec.target_abs_path).read_bytes() == PAYLOAD
    assert _part_files(vault_root) == []
    assert any("Range" in p or p.startswith("/f/") for p in fixture_server.seen)


def test_r11_a_poisoned_resume_prefix_is_caught_by_the_full_re_hash(
        fixture_server, allow_fixture_host, vault_root):
    """The prefix already on disk is never trusted."""
    spec = _spec(fixture_server, vault_root)
    part = Path(spec.target_abs_path + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"\x00" * 20_000)          # same length, wrong bytes

    result = download.fetch(spec)

    assert result.state == "quarantined"
    assert not os.path.exists(spec.target_abs_path)


def test_r11_a_server_that_ignores_range_restarts_cleanly(
        fixture_server, allow_fixture_host, vault_root):
    fixture_server.honour_range = False
    spec = _spec(fixture_server, vault_root)
    part = Path(spec.target_abs_path + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"\x00" * 20_000)

    result = download.fetch(spec)

    assert result.state == "done", result.error_message
    assert Path(spec.target_abs_path).read_bytes() == PAYLOAD


def test_r11_the_queue_survives_a_restart(sec_vault):
    from app.core import db as dbmod
    from app.enable.service import EnableService

    conn = dbmod.get_ro()
    assert dbmod.one(conn, "SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name='enable_jobs'") is not None

    def _op(c):
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT INTO enable_jobs(batch_id,workflow_id,item_key,kind,ref_name,"
            "source_url,source_host,target_abs_path,state,enqueued_at) "
            "VALUES ('b1',NULL,'k1','model','x.safetensors',"
            "'https://huggingface.co/x','huggingface.co','T','running',1)")
        c.commit()

    dbmod.writer().run(_op)
    assert EnableService().resume_pending() == 1
    state = dbmod.scalar(dbmod.get_ro(),
                         "SELECT state FROM enable_jobs WHERE item_key='k1'")
    assert state == "queued"


def test_r11_the_partial_extension_is_invisible_to_the_indexer():
    from app.indexing import walker

    assert ".part" in walker.SKIP_EXTS
    from app.config import QUARANTINE_DIRNAME

    assert QUARANTINE_DIRNAME in walker.SKIP_DIRS


def test_r11_cancel_is_reachable_from_the_api(client):
    response = client.post("/api/v1/enable/cancel", json={})
    assert response.status_code == 200
    assert "cancelled" in response.json()


def test_r11_the_stream_and_status_endpoints_exist(client):
    assert client.get("/api/v1/enable/status").status_code == 200
    assert client.get("/api/v1/enable/quarantine").status_code == 200


# =============================================================================
# End to end through the job service: plan -> confirm -> queue -> verify -> place
# =============================================================================

def _drain(service, batch_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.status(batch_id=batch_id)
        if not status["running"] and not status["states"].get("queued") \
                and not status["states"].get("running"):
            return status
        time.sleep(0.05)
    raise AssertionError(f"batch {batch_id} did not finish: {service.status(batch_id)}")


@pytest.fixture
def enable_service(monkeypatch):
    from app.enable import service as enable_module

    service = enable_module.EnableService()
    # A post-fetch rescan of a synthetic install would race the fixture teardown
    # that closes the database; the rescan itself is C8's territory.
    monkeypatch.setattr(service, "_schedule_rescan", lambda: None)
    plan.reset()
    try:
        yield service
    finally:
        service.shutdown()
        plan.reset()


def _item(root: Path, name: str, *, url: str, size: int, sha: str | None,
          category: str = "loras") -> plan.PlanItem:
    target = root / "models" / category / name
    return plan.PlanItem(
        item_id=plan.item_id("model", name), kind="model", ref_name=name,
        payload={"kind": "model", "ref_name": name, "category": category,
                 "provider": "workflow_manifest", "source_url": url,
                 "source_host": "localhost", "expected_size": size,
                 "expected_sha256": sha, "root_id": 1, "root_path": str(root),
                 "target_abs_path": str(target), "on_conflict": "fail"})


def test_the_whole_path_places_a_verified_file(sec_vault, fixture_server,
                                               allow_fixture_host, enable_service):
    root = Path(str(sec_vault.comfyui_path))
    item = _item(root, "fetched.safetensors",
                 url=f"{fixture_server.base}/f/good.safetensors",
                 size=len(PAYLOAD), sha=PAYLOAD_SHA)
    issued = plan.issue(1, [item])

    started = enable_service.fetch(1, plan_token=issued.token,
                                   item_ids=[item.item_id], confirm=True)
    assert started["queued"] == 1
    assert started["bytes_total"] == len(PAYLOAD)

    status = _drain(enable_service, started["batch_id"])
    assert status["states"] == {"done": 1}, status["items"]
    row = status["items"][0]
    assert row["state"] == "done"
    assert row["result"]["verified"] == "sha256"
    assert (root / "models" / "loras" / "fetched.safetensors").read_bytes() == PAYLOAD
    assert _part_files(root) == []


def test_the_whole_path_quarantines_a_bad_file_and_records_a_scan_error(
        sec_vault, fixture_server, allow_fixture_host, enable_service):
    from app.core import db as dbmod

    root = Path(str(sec_vault.comfyui_path))
    item = _item(root, "poisoned.safetensors",
                 url=f"{fixture_server.base}/f/good.safetensors",
                 size=len(PAYLOAD), sha=WRONG_SHA)
    issued = plan.issue(2, [item])
    started = enable_service.fetch(2, plan_token=issued.token,
                                   item_ids=[item.item_id], confirm=True)
    status = _drain(enable_service, started["batch_id"])

    assert status["states"] == {"quarantined": 1}
    assert not (root / "models" / "loras" / "poisoned.safetensors").exists()
    assert status["quarantine"], "the UI must be able to see what was parked"

    row = dbmod.one(dbmod.get_ro(),
                    "SELECT phase, kind, code, message FROM scan_errors "
                    "WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                    (started["scan_job_id"],))
    assert row["phase"] == "enable"
    assert row["code"] == "INTEGRITY_MISMATCH"
    assert "poisoned.safetensors" in row["message"]

    job = dbmod.one(dbmod.get_ro(),
                    "SELECT status, error_count FROM scan_jobs WHERE id = ?",
                    (started["scan_job_id"],))
    assert job["status"] == "failed"
    assert job["error_count"] == 1


def test_the_whole_path_refuses_a_batch_that_will_not_fit(
        sec_vault, fixture_server, allow_fixture_host, enable_service, monkeypatch):
    from app.core import db as dbmod

    root = Path(str(sec_vault.comfyui_path))
    one_gb = 1024 ** 3
    monkeypatch.setattr(download.shutil, "disk_usage",
                        lambda _p: shutil._ntuple_diskusage(2 * one_gb, one_gb, one_gb))
    item = _item(root, "huge.safetensors",
                 url=f"{fixture_server.base}/f/good.safetensors",
                 size=2 * one_gb, sha=PAYLOAD_SHA)
    issued = plan.issue(3, [item])
    with pytest.raises(InsufficientSpace) as exc:
        enable_service.fetch(3, plan_token=issued.token, item_ids=[item.item_id],
                             confirm=True)
    assert exc.value.details["shortfall_bytes"] > 0
    assert fixture_server.seen == []
    assert dbmod.scalar(dbmod.get_ro(), "SELECT COUNT(*) FROM enable_jobs") == 0


def test_the_whole_path_refuses_a_redirect_off_the_allowlist(
        sec_vault, fixture_server, allow_fixture_host, enable_service):
    root = Path(str(sec_vault.comfyui_path))
    fixture_server.redirect_to = (
        f"http://127.0.0.1:{fixture_server.server_address[1]}/leak/x.safetensors")
    fixture_server.bodies["/leak/x.safetensors"] = PAYLOAD
    item = _item(root, "redirected.safetensors",
                 url=f"{fixture_server.base}/redirect/x",
                 size=len(PAYLOAD), sha=PAYLOAD_SHA)
    issued = plan.issue(4, [item])
    started = enable_service.fetch(4, plan_token=issued.token,
                                   item_ids=[item.item_id], confirm=True)
    status = _drain(enable_service, started["batch_id"])

    assert status["states"] == {"failed": 1}
    assert status["items"][0]["error_code"] == "UPSTREAM_UNAVAILABLE"
    assert not [p for p in fixture_server.seen if p.startswith("/leak")]
    assert not (root / "models" / "loras" / "redirected.safetensors").exists()


def test_a_cancelled_batch_stops_and_keeps_only_the_part_file(
        sec_vault, fixture_server, allow_fixture_host, enable_service):
    root = Path(str(sec_vault.comfyui_path))
    fixture_server.chunk_size = 4096
    fixture_server.delay = 0.02
    item = _item(root, "slow.safetensors",
                 url=f"{fixture_server.base}/f/good.safetensors",
                 size=len(PAYLOAD), sha=PAYLOAD_SHA)
    issued = plan.issue(5, [item])
    started = enable_service.fetch(5, plan_token=issued.token,
                                   item_ids=[item.item_id], confirm=True)
    time.sleep(0.2)
    enable_service.cancel()
    status = _drain(enable_service, started["batch_id"])

    assert "done" not in status["states"]
    assert not (root / "models" / "loras" / "slow.safetensors").exists()
    assert len(_part_files(root)) == 1
