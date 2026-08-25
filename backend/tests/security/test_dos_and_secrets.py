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


# S-05 fixed.  A failure here means the explicit pixel budget was dropped and a
# few hundred bytes of PNG can allocate hundreds of megabytes again.
def test_the_thumbnail_pipeline_caps_source_pixels():
    from PIL import Image

    from app.jobs import thumb_service  # noqa: F401  (import applies its settings)

    assert Image.MAX_IMAGE_PIXELS is not None
    assert Image.MAX_IMAGE_PIXELS <= 80_000_000, (
        "the source pixel budget must be set explicitly, not inherited")


def test_both_image_open_sites_pass_a_format_allowlist(app_dir):
    """Without ``formats=`` a byte sniff routes an output file into the PSD,
    FITS or raw-codec plugins - decoders the vault has no use for."""
    for rel in ("jobs/thumb_service.py", "parsers/image_meta.py"):
        text = (app_dir / rel).read_text(encoding="utf-8")
        opens = text.count("Image.open(")
        assert opens and text.count("formats=") >= opens, rel


def test_an_over_budget_header_is_refused_before_the_decode(tmp_path):
    """Executed: the header is read, the pixels never are."""
    from PIL import Image

    from app.core import imaging
    from app.parsers import image_meta

    bomb = _declared_size_png(tmp_path / "wide.png", 10_000, 9_000)
    assert bomb.stat().st_size < 2048
    assert imaging.exceeds_budget((10_000, 9_000))
    meta = image_meta.OutputMeta()
    image_meta.read_image(bomb, meta)
    assert meta.error_code, "an over-budget image must be recorded, not decoded"
    assert "budget" in (meta.error_message or "")
    assert meta.has_metadata is False
    # And a format outside the allowlist is not routed to its plugin at all.
    assert "PSD" not in imaging.open_formats()
    assert Image.MAX_IMAGE_PIXELS == imaging.MAX_IMAGE_PIXELS


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

# S-07 fixed by BodySizeLimitMiddleware.  A failure here means the cap is gone
# and a multi-megabyte body is buffered and parsed again before rejection.
def test_an_oversized_request_body_is_rejected_before_it_is_parsed(client):
    payload = json.dumps({"uid": "model:1", "new_name": "A" * (32 * 1024 * 1024)})
    response = client.post("/api/v1/fileops/rename", content=payload,
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_the_body_cap_is_not_bypassed_by_omitting_content_length(client):
    """A chunked body carries no length, so the bytes are counted as they land."""
    def chunks():
        payload = (b'{"uid":"model:1","new_name":"' + b"A" * (12 * 1024 * 1024)
                   + b'"}')
        for start in range(0, len(payload), 65536):
            yield payload[start:start + 65536]

    response = client.post("/api/v1/fileops/rename", content=chunks(),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_the_body_cap_also_covers_the_mcp_endpoint(client):
    """``mcp_post`` reads the whole body itself, outside any Pydantic model."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"pad": "A" * (32 * 1024 * 1024)}})
    response = client.post("/api/v1/mcp", content=payload,
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_the_body_cap_leaves_room_for_the_largest_legitimate_body():
    """The 3D-model poster is the biggest real body: 4 MB of PNG, base64'd."""
    from app.api.middleware import MAX_BODY_BYTES
    from app.jobs.thumb_service import ThumbService

    assert MAX_BODY_BYTES >= ThumbService.MAX_RENDERED_BYTES * 4 // 3 + 1024
    assert MAX_BODY_BYTES <= 16 * 1024 * 1024


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


# S-03 fixed.  A failure here means the SSE subscriber cap was removed and
# ``ProgressBus._subs`` can grow without bound again.
def test_the_sse_subscriber_count_is_capped():
    from app.core import progress

    assert any("MAX_SUBSCRIBERS" in name or "MAX_SUBS" in name
               for name in dir(progress)), (
        "BUILD_PLAN 7.12 requires an SSE subscriber cap")
    assert 0 < progress.MAX_SUBSCRIBERS <= 128


def test_the_bus_refuses_the_subscription_past_the_cap():
    """Executed, not read: the cap holds at the bus and at the router."""
    import asyncio

    from app.api.deps import require_stream_capacity
    from app.api.middleware import ApiError
    from app.core import progress

    async def drive() -> None:
        bus = progress.ProgressBus("cap-probe")
        # A subscriber only registers on its first step, so step each one.
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


# ---------------------------------------------------------------------------
# Outbound network surface
# ---------------------------------------------------------------------------

def test_only_seven_modules_may_reach_the_network(app_dir):
    """The fifth is the C9 fetcher, whose every URL passes ``enable/hosts.py``;
    the sixth is the node-registry catalogue, which only ever fetches metadata
    from its fixed official endpoint behind the ``online_enabled`` kill-switch;
    the seventh is the self-updater, whose repository is a module constant and
    whose every URL passes ``hosts.check(kind=KIND_RELEASE)``.
    """
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
                             "services/app_update_service.py",
                             "services/civitai_service.py",
                             "services/comfyui_service.py",
                             "services/node_registry_service.py",
                             "services/ollama_service.py"], users


# S-08 fixed by ``core/urlsafe.py``.  A failure here means the backend can be
# steered into an arbitrary outbound GET again - and /ai/describe would follow
# it with the owner's prompts.
def test_the_ollama_endpoint_only_accepts_a_loopback_url(client):
    client.patch("/api/v1/system/config", json={"ollama_enabled": True})
    response = client.post("/api/v1/system/ollama/test",
                           json={"url": "http://169.254.169.254"})
    assert response.status_code == 422


@pytest.mark.parametrize("url", [
    "http://169.254.169.254",            # the cloud metadata address
    "http://8.8.8.8:11434",              # a public address
    "https://evil.test/api",             # any name at all - DNS is not trusted
    "http://ollama.internal:11434",
    "http://user:pw@127.0.0.1:11434",    # credentials in the authority
    "http://127.0.0.1:11434/api/tags",   # a path, not a base URL
    "file:///C:/Windows/win.ini",
    "http://[fe80::1]:11434",            # IPv6 link-local
    "http://0.0.0.0:11434",
])
def test_no_endpoint_accepts_a_non_local_ollama_address(client, url):
    """Both the probe and the persisted config key are refused."""
    assert client.post("/api/v1/system/ollama/test",
                       json={"url": url}).status_code == 422, url
    assert client.patch("/api/v1/system/config",
                        json={"ollama_url": url}).status_code == 422, url
    stored = client.get("/api/v1/system/config").json().get("ollama_url")
    assert stored != url


@pytest.mark.parametrize("url", [
    "http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434",
    "http://192.168.1.10:11434", "http://10.1.2.3:11434", "http://172.16.0.9:11434",
])
def test_a_lan_ollama_is_still_accepted(client, url):
    """The owner may legitimately run Ollama on another machine on their LAN."""
    assert client.patch("/api/v1/system/config",
                        json={"ollama_url": url}).status_code == 200, url


def test_the_service_refuses_a_non_local_url_that_reached_the_database(client):
    """Second gate: a value that bypassed the schema still sends nothing."""
    import asyncio

    from app.services.ollama_service import OllamaService

    client.patch("/api/v1/system/config", json={"ollama_enabled": True})
    service = OllamaService("http://169.254.169.254")
    ok, reason = asyncio.run(service.check_connection())
    assert ok is False
    assert "not a local or private-network address" in reason
    answer = asyncio.run(service.generate("a prompt that must not leave the box"))
    assert answer["ok"] is False
    assert answer["text"] is None


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


# S-09 fixed: the floor is >=12.3.0.  A failure here means the floor was lowered
# again and a fresh install may land on a Pillow with 17 known CVEs.
def test_pillow_floor_excludes_known_cves(repo_root):
    floor = _requirements(repo_root)["pillow"]
    assert _version(floor)[:2] >= (12, 3), f"pillow floor is {floor}"


# S-09 fixed: python-multipart was removed as unused.  A failure here means it
# came back - re-check that nothing actually parses a form before pinning it.
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
