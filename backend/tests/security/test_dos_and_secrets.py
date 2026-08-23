"""Resource limits, secret handling, log hygiene, and dependency floors.

BUILD_PLAN 7 items 9-12 and 14.  The threat here is not a remote attacker - the
app is loopback-only - it is a hostile *file*: a 324-byte PNG that declares
160 megapixels, a safetensors header claiming 200 MB, a checkpoint with 60,000
zip entries.  Those arrive in ``models/``, ``input/`` and ``output/`` as a matter
of course.
"""

from __future__ import annotations

import json
import struct
import time
import zlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

SECRET = "civitai-key-must-never-appear-abc123"  # noqa: S105 - a probe value


def test_the_civitai_key_is_never_returned_by_the_config_endpoint(client):
    assert client.patch("/api/v1/system/config",
                        json={"civitai_api_key": SECRET}).status_code == 200
    body = client.get("/api/v1/system/config")
    assert SECRET not in body.text
    assert body.json()["civitai_api_key_set"] is True
    assert "civitai_api_key" not in body.json()


def test_the_civitai_key_is_never_returned_by_any_other_endpoint(client):
    client.patch("/api/v1/system/config", json={"civitai_api_key": SECRET})
    for path in ("/api/v1/system/info", "/api/v1/system/health",
                 "/api/v1/system/stats", "/api/v1/system/roots",
                 "/api/v1/comfyui/info", "/api/v1/search/status",
                 "/api/v1/embeddings/status", "/api/v1/ai/status",
                 "/openapi.json"):
        assert SECRET not in client.get(path).text, path


def test_the_civitai_key_is_not_exposed_through_mcp(client):
    session_headers = {"Content-Type": "application/json"}
    client.patch("/api/v1/system/config", json={"civitai_api_key": SECRET})
    init = client.post("/api/v1/mcp", content=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {}}}), headers=session_headers)
    sid = init.headers["mcp-session-id"]
    headers = {**session_headers, "Mcp-Session-Id": sid}
    client.post("/api/v1/mcp", content=json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}), headers=headers)
    for tool in ("vault_stats", "get_index_status"):
        response = client.post("/api/v1/mcp", content=json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": {}}}), headers=headers)
        assert SECRET not in response.text, tool


def test_the_civitai_key_is_never_logged(client, caplog):
    with caplog.at_level("DEBUG"):
        client.patch("/api/v1/system/config", json={"civitai_api_key": SECRET})
        client.get("/api/v1/system/config")
    joined = " ".join(str(r.getMessage()) for r in caplog.records)
    assert SECRET not in joined


def test_the_key_only_ever_travels_to_civitai(app_dir):
    source = (app_dir / "services" / "civitai_service.py").read_text(encoding="utf-8")
    assert 'API_BASE = "https://civitai.com/api/v1"' in source
    for line in source.splitlines():
        if "civitai_api_key" in line and "Authorization" not in line and \
                "cfg." not in line and "def " not in line:
            assert "civitai.com" in line or line.strip().startswith("#"), line
    # no other module reads the key at all
    readers = [p.name for p in app_dir.rglob("*.py")
               if "civitai_api_key" in p.read_text(encoding="utf-8")]
    assert set(readers) <= {"civitai_service.py", "config_service.py",
                            "system_router.py", "system.py"}, readers


def test_config_write_only_keys_are_not_echoed_in_the_patch_response(client):
    response = client.patch("/api/v1/system/config",
                            json={"civitai_api_key": SECRET})
    assert SECRET not in response.text


# ---------------------------------------------------------------------------
# Log hygiene
# ---------------------------------------------------------------------------

@pytest.fixture
def tolerant_client(sec_vault):
    """A client that lets a 500 come back as a response instead of re-raising."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update({"X-Vault-Request": "1"})
        yield c


def test_no_traceback_reaches_a_response_body(tolerant_client, monkeypatch):
    client = tolerant_client
    from app.services.queries import models_query

    def boom(*_args, **_kwargs):
        raise RuntimeError("internal detail: C:\\secret\\path and a stack trace")

    monkeypatch.setattr(models_query, "list_models", boom)
    response = client.get("/api/v1/models")
    assert response.status_code == 500
    body = response.json()
    assert "Traceback" not in response.text
    assert "internal detail" not in response.text
    assert "C:\\secret" not in response.text
    assert body["error"]["code"] == "INTERNAL"
    assert body["error"]["request_id"]


def test_the_request_id_in_the_body_matches_the_header(tolerant_client, monkeypatch):
    client = tolerant_client
    from app.services.queries import models_query

    monkeypatch.setattr(models_query, "list_models",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    response = client.get("/api/v1/models")
    assert response.json()["error"]["request_id"] == response.headers["X-Request-Id"]


def test_prompts_are_never_logged(app_dir):
    """Output prompts and Ollama prompts must not reach the log."""
    offenders = []
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "log." not in line:
                continue
            if "prompt" in line.lower() and "%s" in line:
                offenders.append((path.name, lineno, line.strip()[:80]))
    assert not offenders, f"prompt text reaching the log: {offenders}"


# ---------------------------------------------------------------------------
# Parser resource caps
# ---------------------------------------------------------------------------

def test_the_safetensors_header_cap_is_enforced(tmp_path):
    from app.parsers import safetensors_header

    path = tmp_path / "huge-header.safetensors"
    with open(path, "wb") as handle:
        handle.write(struct.pack("<Q", safetensors_header.MAX_HEADER + 1))
        handle.write(b"\0" * 4096)
    started = time.perf_counter()
    result = safetensors_header.read_header(path)
    assert (time.perf_counter() - started) < 5.0
    assert not result.ok
    assert result.integrity in ("invalid_header", "truncated", "unreadable")


def test_the_gguf_parser_caps_its_counts():
    from app.parsers import gguf_header

    assert gguf_header.MAX_KV <= 65_536
    assert gguf_header.MAX_TENSORS <= 1_000_000
    assert gguf_header.MAX_STRING <= 16 * 1024 * 1024


def test_the_embedded_graph_cap_is_enforced():
    from app.parsers import image_meta

    assert image_meta.GRAPH_CAP <= 16 * 1024 * 1024


def test_a_zip_with_a_huge_entry_count_still_completes(tmp_path):
    import zipfile

    from app.parsers import torch_zip

    checkpoint = tmp_path / "many.ckpt"
    with zipfile.ZipFile(checkpoint, "w") as archive:
        for index in range(20_000):
            archive.writestr(f"archive/data/{index}", b"")
        archive.writestr("archive/data.pkl", b"\x80\x02}q\x00.")
    started = time.perf_counter()
    torch_zip.read_keys(checkpoint)
    assert (time.perf_counter() - started) < 20.0


# ---------------------------------------------------------------------------
# Image decompression bombs - SECURITY_REVIEW S-05
# ---------------------------------------------------------------------------

def _declared_size_png(path: Path, width: int, height: int) -> Path:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\xff" * (width * 3) for _ in range(min(height, 4)))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return path


def test_a_png_declaring_900_megapixels_is_refused_by_pillow(tmp_path):
    from PIL import Image

    bomb = _declared_size_png(tmp_path / "huge.png", 30_000, 30_000)
    assert bomb.stat().st_size < 2048
    with pytest.raises(Image.DecompressionBombError), Image.open(bomb) as image:
        image.load()


@pytest.mark.xfail(reason="SECURITY_REVIEW S-05: Image.MAX_IMAGE_PIXELS is left "
                          "at Pillow's default, so a 324-byte PNG declaring "
                          "160 Mpx decodes into ~480 MB before the thumbnail is "
                          "made",
                   strict=False)
def test_the_thumbnail_pipeline_caps_source_pixels():
    from PIL import Image

    from app.jobs import thumb_service  # noqa: F401  (import applies its settings)

    assert Image.MAX_IMAGE_PIXELS is not None
    assert Image.MAX_IMAGE_PIXELS <= 80_000_000, (
        "the source pixel budget must be set explicitly, not inherited")


def test_a_bomb_never_turns_a_thumbnail_request_into_a_5xx(indexed_client,
                                                           sec_comfyui):
    """Whatever the pixel budget, the endpoint degrades to a placeholder."""
    client = indexed_client
    _declared_size_png(sec_comfyui / "output" / "bomb.png", 30_000, 30_000)
    from app.indexing.service import get_indexer

    indexer = get_indexer()
    indexer.start(mode="full", force=True, enrich_online=False, trigger="test")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and indexer.status().get("running"):
        time.sleep(0.05)
    outputs = client.get("/api/v1/outputs", params={"limit": 50}).json()["items"]
    bombs = [o for o in outputs if o["filename"] == "bomb.png"]
    if not bombs:
        pytest.skip("the bomb was not indexed")
    response = client.get("/api/v1/files/thumbnail",
                          params={"uid": bombs[0]["uid"], "size": 320})
    assert response.status_code == 200
    assert response.headers.get("X-Thumb-Source") in ("placeholder", "generated")


def test_the_thumbnail_size_parameter_is_clamped(indexed_client):
    from app.jobs.thumb_service import SIZES, pick_size

    for requested in (-1, 0, 1, 5000, 10 ** 9):
        assert pick_size(requested) in SIZES


# ---------------------------------------------------------------------------
# Request and stream limits
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="SECURITY_REVIEW S-07: no Content-Length cap exists, so "
                          "a multi-megabyte body is fully buffered and parsed "
                          "before Pydantic rejects it",
                   strict=False)
def test_an_oversized_request_body_is_rejected_before_it_is_parsed(client):
    payload = json.dumps({"uid": "model:1", "new_name": "A" * (32 * 1024 * 1024)})
    response = client.post("/api/v1/fileops/rename", content=payload,
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_an_oversized_body_at_least_fails_closed(client):
    payload = json.dumps({"uid": "model:1", "new_name": "A" * (4 * 1024 * 1024)})
    response = client.post("/api/v1/fileops/rename", content=payload,
                           headers={"Content-Type": "application/json"})
    assert response.status_code in (413, 422)


def test_bulk_endpoints_cap_their_uid_lists(client):
    for endpoint in ("/api/v1/models/bulk", "/api/v1/outputs/bulk",
                     "/api/v1/storage/cleanup", "/api/v1/tags/assign"):
        response = client.post(endpoint,
                               json={"uids": [f"model:{i}" for i in range(500)],
                                     "patch": {}, "add": ["x"]})
        assert response.status_code in (413, 422), endpoint


def test_fileops_cap_their_uid_lists(client):
    from app.services.file_ops import MAX_BATCH

    assert MAX_BATCH == 200
    response = client.post("/api/v1/fileops/delete",
                           json={"uids": [f"model:{i}" for i in range(MAX_BATCH + 1)]})
    assert response.status_code in (413, 422)


def test_the_progress_bus_drops_a_slow_subscriber_rather_than_growing(app_dir):
    from app.core.progress import MAX_QUEUE

    assert MAX_QUEUE <= 10_000
    source = (app_dir / "core" / "progress.py").read_text(encoding="utf-8")
    assert "overflow" in source, "a saturated subscriber must be dropped"


@pytest.mark.xfail(reason="SECURITY_REVIEW S-03: ProgressBus._subs is unbounded, "
                          "so nothing caps how many SSE streams may be opened",
                   strict=False)
def test_the_sse_subscriber_count_is_capped():
    from app.core import progress

    assert any("MAX_SUBSCRIBERS" in name or "MAX_SUBS" in name
               for name in dir(progress)), (
        "BUILD_PLAN 7.12 requires an SSE subscriber cap")


# ---------------------------------------------------------------------------
# Outbound network surface
# ---------------------------------------------------------------------------

def test_only_five_modules_may_reach_the_network(app_dir):
    """The fifth is the C9 fetcher, whose every URL passes ``enable/hosts.py``."""
    users = []
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "import httpx" in text or "import requests" in text or \
                "urllib.request" in text:
            users.append(str(path.relative_to(app_dir)).replace("\\", "/"))
    assert sorted(users) == ["enable/download.py",
                             "jobs/embed_service.py",
                             "services/civitai_service.py",
                             "services/comfyui_service.py",
                             "services/ollama_service.py"], users


@pytest.mark.xfail(reason="SECURITY_REVIEW S-08: /system/ollama/test and the "
                          "ollama_url config key accept any absolute URL, so the "
                          "backend can be made to issue an arbitrary GET",
                   strict=False)
def test_the_ollama_endpoint_only_accepts_a_loopback_url(client):
    client.patch("/api/v1/system/config", json={"ollama_enabled": True})
    response = client.post("/api/v1/system/ollama/test",
                           json={"url": "http://169.254.169.254"})
    assert response.status_code == 422


def test_the_embedding_model_url_is_not_settable_through_the_api(client):
    """C9 precedent: the only download the app performs today is not
    caller-steerable, and the C9 downloader must keep that property."""
    response = client.patch("/api/v1/system/config",
                            json={"embedding_model_url": "http://evil.test/m"})
    assert response.status_code == 422
    assert "embedding_model_url" in response.text


# ---------------------------------------------------------------------------
# Dependency floors (BUILD_PLAN 7.14)
# ---------------------------------------------------------------------------

def _requirements(repo_root: Path) -> dict[str, str]:
    text = (repo_root / "backend" / "requirements.txt").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        for separator in (">=", "==", "~="):
            if separator in line:
                name, _sep, rest = line.partition(separator)
                out[name.strip().lower().split("[")[0]] = rest.split(",")[0].strip()
                break
    return out


def _version(text: str) -> tuple:
    parts = []
    for chunk in text.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def test_pyyaml_floor_is_at_or_above_6_0_1(repo_root):
    assert _version(_requirements(repo_root)["pyyaml"]) >= (6, 0, 1)


@pytest.mark.xfail(reason="SECURITY_REVIEW S-09: the pillow floor >=10.3 admits "
                          "builds with 17 later CVEs reachable through "
                          "Image.open on files the vault does not control",
                   strict=False)
def test_pillow_floor_excludes_known_cves(repo_root):
    floor = _requirements(repo_root)["pillow"]
    assert _version(floor)[:2] >= (12, 3), f"pillow floor is {floor}"


@pytest.mark.xfail(reason="SECURITY_REVIEW S-09: python-multipart is unused and "
                          "its >=0.0.6 floor admits nine CVEs",
                   strict=False)
def test_python_multipart_is_not_pinned_at_a_vulnerable_floor(repo_root):
    requirements = _requirements(repo_root)
    assert ("python-multipart" not in requirements
            or _version(requirements["python-multipart"]) >= (0, 0, 31))


def test_installed_versions_are_current(repo_root):
    """The environment actually in use must be clean even where floors are not."""
    import PIL
    import starlette
    import yaml

    assert tuple(int(p) for p in PIL.__version__.split(".")[:2]) >= (12, 3)
    assert tuple(int(p) for p in starlette.__version__.split(".")[:2]) >= (1, 3)
    assert _version(yaml.__version__) >= (6, 0, 1)


def test_no_torch_or_pickle_dependency_is_declared(repo_root):
    requirements = _requirements(repo_root)
    for forbidden in ("torch", "torchvision", "dill", "safetensors"):
        assert forbidden not in requirements, forbidden
