"""The System Advisor end to end: router -> service -> fit/security -> catalog.

Hermetic by construction.  The hardware profile is a fixed fake (a 24 GB CUDA
card beside 64 GB of RAM), no probe is allowed to start a process, the catalog
is a handful of entries written into ``tmp_path``, and the updater's only way to
the network - ``sources.fetch_json`` - is replaced by a function that answers
from canned documents and fails the test if it is asked for a URL that is off
the catalog allowlist.  Nothing here reads or writes ``backend/data``.
"""

from __future__ import annotations

import json
import threading

import pytest

from app.advisor import catalog, fit, hardware, probe, service, sources, updater
from app.core import config_service
from app.core import db as dbmod
from app.core.errors import ConflictError, FeatureUnavailable, UpstreamUnavailable
from app.enable import hosts

GIB = fit.GIB
MIB = fit.MIB
API = "/api/v1/advisor"
CSRF = {"X-Vault-Request": "1"}
SHA = "ab" * 32


# ---------------------------------------------------------------------------
# A tiny catalog
# ---------------------------------------------------------------------------

def _variant(variant_id: str, size: int, **extra) -> dict:
    row = {"id": variant_id, "label": variant_id.rsplit("/", 1)[-1].rsplit(".", 1)[0],
           "format": "safetensors", "bytes": int(size), "parts": 1,
           "file": variant_id, "direct": True,
           "download_url": f"https://huggingface.co/demo-labs/x/resolve/main/{variant_id}"}
    row.update(extra)
    return row


def _entry(entry_id: str, name: str, kind: str, variants: list[dict], **extra) -> dict:
    row = {"id": entry_id, "source": "huggingface", "name": name,
           "publisher": "demo-labs", "kind": kind, "license": "apache-2.0",
           "url": "https://huggingface.co/demo-labs/x", "downloads": 50_000,
           "created_at": "2025-01-01T00:00:00.000Z",
           "updated_at": "2026-06-01T00:00:00.000Z", "runtime": "comfyui",
           "variants": variants,
           "safety": {"scan": "clean", "publisher_trust": "official",
                      "hash_published": True}}
    row.update(extra)
    return row


def tiny_entries() -> list[dict]:
    return [
        # optimal lead (fp8), with a larger variant that only offloads
        _entry("hf:demo-labs/Demo-Image", "Demo Image", "image", [
            _variant("split_files/diffusion_models/demo_fp16.safetensors",
                     22.2 * GIB, precision="fp16", sha256=SHA),
            _variant("split_files/diffusion_models/demo_fp8.safetensors",
                     12 * GIB, precision="fp8", sha256=SHA)],
            companions=[{"role": "text_encoder", "label": "t5", "bytes": 5 * GIB,
                         "format": "safetensors"},
                        {"role": "vae", "label": "vae", "bytes": 300 * MIB,
                         "format": "safetensors"}],
            curated=True, quality=95, family="Demo"),
        # fits only by spending the headroom
        _entry("hf:demo-labs/Demo-Video", "Demo Video", "video", [
            _variant("demo_video_fp16.safetensors", 17 * GIB, precision="fp16")],
            downloads=9_000, updated_at="2026-09-01T00:00:00.000Z"),
        # a comfortable language model
        _entry("hf:bartowski/Demo-7B-GGUF", "Demo 7B", "llm", [
            _variant("Demo-7B-Q8_0.gguf", 7.5 * GIB, precision="Q8_0", format="gguf"),
            _variant("Demo-7B-Q4_K_M.gguf", 4.4 * GIB, precision="Q4_K_M",
                     format="gguf")],
            publisher="bartowski", runtime="gguf", license="mit", downloads=800_000,
            safety={"scan": "clean", "publisher_trust": "trusted"}),
        # larger than the GPU: split with the processor
        _entry("ollama:demo:70b", "demo:70b", "llm", [
            _variant("demo:70b", 40 * GIB, precision="Q4_K_M", format="gguf",
                     direct=False, download_url="https://ollama.com/library/demo:70b")],
            source="ollama", publisher="ollama library", runtime="ollama",
            license=None, downloads=None, url="https://ollama.com/library/demo",
            safety={"scan": "unknown", "publisher_trust": "trusted"}),
        # does not fit at all
        _entry("hf:demo-labs/Demo-Giant", "Demo Giant", "image", [
            _variant("giant_fp32.safetensors", 80 * GIB, precision="fp32")],
            downloads=120),
        # an add-on
        _entry("civitai:4242", "Demo Style", "lora", [
            _variant("v9001", 200 * MIB, file="demo_style.safetensors",
                     download_url="https://civitai.com/api/download/models/9001")],
            source="civitai", publisher="demo_artist", downloads=52_000,
            license="civitai: commercial use allowed",
            url="https://civitai.com/models/4242",
            safety={"scan": "clean", "publisher_trust": "unknown"}),
        # a pickle from a curated list: runs fine, scores low
        _entry("comfy:models_upscale_models_4x_demo.pth", "4x Demo", "upscale", [
            _variant("4x_demo.pth", 66 * MIB, format="pth",
                     download_url="https://example.test/4x_demo.pth")],
            source="comfy_manager", publisher="unknown", license=None, downloads=None,
            created_at=None, updated_at=None, url="https://example.test/4x",
            safety={"scan": "unknown", "publisher_trust": "curated"}),
    ]


def adult_entry() -> dict:
    """Flagged adult by its host: the catalog neither stores nor serves it."""
    return _entry("hf:someone/Adult-Thing", "Adult Thing", "image", [
        _variant("adult.safetensors", 6 * GIB)], nsfw=True, publisher="someone")


EXPECTED_VERDICTS = {
    "hf:demo-labs/Demo-Image": "optimal",
    "hf:demo-labs/Demo-Video": "max",
    "hf:bartowski/Demo-7B-GGUF": "optimal",
    "ollama:demo:70b": "offload",
    "hf:demo-labs/Demo-Giant": "no",
    "civitai:4242": "base",
    "comfy:models_upscale_models_4x_demo.pth": "optimal",
}
LISTED = len(EXPECTED_VERDICTS)

TINY_SOURCES = {
    "revision": 1,
    "sources": [
        {"id": "huggingface", "name": "Hugging Face Hub", "url": "https://huggingface.co",
         "provides": "model facts", "kinds": ["image", "video", "llm"], "trust": "high"},
        {"id": "civitai", "name": "Civitai", "url": "https://civitai.com",
         "provides": "community models", "kinds": ["lora"], "trust": "community"},
        {"id": "comfy_manager", "name": "ComfyUI-Manager model list",
         "url": "https://github.com/Comfy-Org/ComfyUI-Manager",
         "provides": "curated files", "kinds": ["upscale"], "trust": "curated"},
        {"id": "openmodeldb", "name": "OpenModelDB", "url": "https://openmodeldb.info",
         "provides": "upscalers", "kinds": ["upscale"], "trust": "curated"},
        {"id": "ollama", "name": "Ollama library", "url": "https://ollama.com",
         "provides": "language models", "kinds": ["llm"], "trust": "high"},
        {"id": "github", "name": "GitHub", "url": "https://github.com",
         "provides": "leads", "kinds": [], "trust": "leads"},
    ],
    "pins": {"huggingface": [{"repo": "demo-labs/Demo-Image", "kind": "image",
                              "curated": True, "quality": 95}]},
    "discovery": {
        "huggingface": [{"kind": "llm", "pipeline_tag": "text-generation",
                         "sort": "downloads", "limit": 5}],
        "civitai": [{"types": "LORA", "sort": "Most Downloaded", "period": "Month",
                     "limit": 5}],
        "github": [{"q": "topic:comfyui stars:>200", "limit": 5}],
        "openmodeldb": {"limit": 10},
        "follow_base_models": {"enabled": True, "limit": 5},
    },
    "publishers": {"official": ["demo-labs"], "trusted": ["bartowski"]},
}


def fake_profile(mount: str) -> dict:
    accelerator = {"backend": "cuda", "name": "NVIDIA GeForce RTX 4090",
                   "vendor": "nvidia", "vram_total_bytes": 24 * GIB,
                   "pooled_vram_bytes": 24 * GIB, "unified": False,
                   "vram_is_estimate": False, "gpu_index": 0, "gpu_count": 1}
    return {
        "collected_at": 1_795_000_000_000, "elapsed_ms": 3,
        "os": {"platform": "win32", "name": "Windows", "version": "11",
               "arch": "AMD64", "python": "3.12.0"},
        "cpu": {"model": "AMD Ryzen 9 7950X", "vendor": "amd", "arch": "AMD64",
                "physical_cores": 16, "logical_cores": 32, "performance_cores": None,
                "efficiency_cores": None, "max_mhz": 4500.0, "features": ["AVX2"]},
        "memory": {"total_bytes": 64 * GIB, "available_bytes": 48 * GIB,
                   "swap_total_bytes": 8 * GIB},
        "gpus": [{"index": 0, "name": "NVIDIA GeForce RTX 4090", "vendor": "nvidia",
                  "backend": "cuda", "vram_total_bytes": 24 * GIB,
                  "vram_is_estimate": False, "unified": False, "integrated": False,
                  "driver": "566.36", "source": "nvidia-smi"}],
        "accelerator": accelerator,
        "volumes": [{"mount": mount, "device": None, "fstype": None,
                     "total_bytes": 2000 * GIB, "free_bytes": 500 * GIB,
                     "used_bytes": 1500 * GIB, "kind": "nvme", "roles": ["Models"]}],
        "tier": hardware.tier_for(accelerator, 64 * GIB),
        "notes": [], "probes": [{"name": "nvidia_static", "ok": True, "ms": 1}],
        "psutil": True,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _reset_updater() -> None:
    updater._cancel.clear()
    updater._status.update({"running": False, "phase": None, "phase_label": None,
                            "done": 0, "total": 0, "started_at": None,
                            "finished_at": None, "trigger": None, "error": None,
                            "result": None})


@pytest.fixture
def advisor(tmp_path, monkeypatch, temp_vault):
    """An isolated advisor: fake machine, tmp catalog, no process, no network."""
    data = tmp_path / "advisor-data"
    seed_dir = tmp_path / "advisor-seed"
    seed_dir.mkdir()
    (seed_dir / "sources.json").write_text(json.dumps(TINY_SOURCES), encoding="utf-8")
    monkeypatch.setattr(catalog, "USER_DIR", data)
    monkeypatch.setattr(catalog, "USER_CATALOG", data / "catalog.json")
    monkeypatch.setattr(catalog, "USER_STATE", data / "state.json")
    monkeypatch.setattr(catalog, "SEED_CATALOG", seed_dir / "model_catalog.json")
    monkeypatch.setattr(catalog, "SEED_SOURCES", seed_dir / "sources.json")

    profile = fake_profile(str(tmp_path))
    monkeypatch.setattr(hardware, "profile",
                        lambda *, refresh=False, marked_paths=None: profile)
    # The live sample must not start nvidia-smi / ioreg from a test.
    monkeypatch.setattr(probe, "run", lambda name: None)

    def forbidden(*args, **kwargs):
        raise AssertionError(f"network reached from a hermetic test: {args}")

    monkeypatch.setattr(sources, "fetch_json", forbidden)
    monkeypatch.setattr(sources, "head_status", forbidden)
    monkeypatch.setattr(updater.time, "sleep", lambda seconds: None)

    hardware.reset()
    catalog.invalidate()
    service._invalidate()
    _reset_updater()
    catalog.save([*tiny_entries(), adult_entry()], sources={}, leads=[
        {"name": "demo-labs/demo-video", "url": "https://github.com/demo-labs/demo-video",
         "stars": 4200}])
    try:
        yield data
    finally:
        thread = updater._thread
        if thread is not None and thread.is_alive():
            updater.cancel()
            thread.join(timeout=10)
        _reset_updater()
        hardware.reset()
        catalog.invalidate()
        service._invalidate()


@pytest.fixture
def client(advisor):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.headers.update(CSRF)
        yield c


def go_online() -> None:
    config_service.set_config({"online_enabled": True})


def assert_envelope(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["message"]
    assert "detail" not in response.json(), "never FastAPI's raw error shape"
    return error


# ---------------------------------------------------------------------------
# Every read route answers with its documented shape
# ---------------------------------------------------------------------------

BUDGET_KEYS = {"backend", "unified", "vram_total", "vram_pooled", "vram_reserve",
               "vram_safe", "vram_max", "pooled_safe", "pooled_max", "ram_total",
               "ram_reserve", "ram_safe", "ram_max", "vram_headroom_pct",
               "ram_headroom_pct", "disk_free", "inferred"}
UPDATE_KEYS = {"running", "phase", "phase_label", "done", "total", "started_at",
               "finished_at", "trigger", "error", "result", "online_enabled",
               "auto_update", "catalog_origin", "catalog_generated_at",
               "catalog_age_ms", "stale", "stale_after_ms", "entries", "last_run"}

READ_ROUTES = {
    "/overview": {"tier", "accelerator", "cpu", "memory", "os", "notes", "budget",
                  "settings", "totals", "catalog_total", "new", "update"},
    "/hardware": {"collected_at", "elapsed_ms", "os", "cpu", "memory", "gpus",
                  "accelerator", "volumes", "tier", "notes", "probes", "psutil",
                  "budget"},
    "/live": {"t", "cpu", "memory", "gpus", "disk_io", "net_io", "volumes",
              "top_processes", "budget", "accelerator"},
    "/catalog": {"items", "page", "facets", "meta"},
    "/catalog/entry?id=hf:demo-labs/Demo-Image": {
        "id", "name", "kind", "kind_label", "variants", "fit", "security", "budget",
        "first_seen_at", "is_new", "installed"},
    "/recommendations": {"tier", "accelerator", "budget", "scope", "totals",
                         "runnable", "catalog_total", "groups", "inferred"},
    "/installed": {"items", "page", "facets", "meta"},
    "/safe-config": {"summary", "budget", "comfyui", "ollama", "llama_cpp", "files",
                     "rules", "inferred"},
    "/settings": {"vram_headroom_pct", "ram_headroom_pct", "auto_update", "kinds",
                  "online_enabled", "limits", "kind_options"},
    "/sources": {"revision", "origin", "sources", "pins", "user_pins", "learned",
                 "learned_total", "queries", "publishers", "allowed_hosts", "leads",
                 "counts", "update"},
    "/update/status": UPDATE_KEYS,
}


@pytest.mark.parametrize("path", sorted(READ_ROUTES))
def test_every_read_route_returns_200_with_its_documented_keys(client, path):
    response = client.get(API + path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert READ_ROUTES[path] <= set(body), READ_ROUTES[path] - set(body)
    assert response.headers.get("X-Request-Id")


def test_read_routes_need_no_csrf_header(advisor):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as naked:
        for path in ("/overview", "/hardware", "/catalog", "/settings", "/sources"):
            assert naked.get(API + path).status_code == 200, path


def test_the_route_table_is_exactly_the_eighteen_documented_routes(client):
    spec = client.get("/openapi.json").json()["paths"]
    found = sorted((method.upper(), path) for path, methods in spec.items()
                   if path.startswith(API) for method in methods)
    assert found == sorted([
        ("GET", API + "/overview"), ("GET", API + "/hardware"), ("GET", API + "/live"),
        ("GET", API + "/catalog"), ("GET", API + "/catalog/entry"),
        ("POST", API + "/catalog/verify-link"), ("GET", API + "/recommendations"),
        ("GET", API + "/installed"), ("GET", API + "/installed/{model_id}"),
        ("GET", API + "/safe-config"), ("GET", API + "/settings"),
        ("PATCH", API + "/settings"), ("GET", API + "/sources"),
        ("POST", API + "/sources/pins"), ("DELETE", API + "/sources/pins"),
        ("GET", API + "/update/status"), ("POST", API + "/update"),
        ("POST", API + "/update/cancel")])
    # Eighteen operations over sixteen paths: /settings and /sources/pins each
    # carry two verbs.
    assert len(found) == 18
    assert len({path for _m, path in found}) == 16


def test_no_advisor_route_accepts_a_url_parameter_or_body_field(client):
    """The module's own rule: a model is named by id, a pin by owner/name."""
    spec = client.get("/openapi.json").json()
    suspicious = ("url", "uri", "href", "link", "host", "endpoint", "path", "file")
    offenders = []
    for path, methods in spec["paths"].items():
        if not path.startswith(API):
            continue
        for method, operation in methods.items():
            names = [param["name"] for param in operation.get("parameters") or []]
            body = (operation.get("requestBody") or {}).get("content", {}) \
                .get("application/json", {}).get("schema", {})
            for option in [body, *body.get("anyOf", [])]:
                ref = option.get("$ref", "").rsplit("/", 1)[-1]
                names += list(spec["components"]["schemas"].get(ref, {})
                              .get("properties", {}))
            offenders += [(method, path, name) for name in names
                          if name.lower() in suspicious]
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# Overview, hardware, live
# ---------------------------------------------------------------------------

def test_overview_joins_the_machine_the_budget_and_the_verdict_totals(client):
    body = client.get(API + "/overview").json()
    assert body["tier"]["id"] == "enthusiast"
    assert body["accelerator"]["backend"] == "cuda"
    assert set(body["budget"]) == BUDGET_KEYS
    assert body["budget"]["inferred"] is True
    assert body["budget"]["disk_free"] == 500 * GIB, "free space on the Models volume"
    assert body["catalog_total"] == LISTED, "the adult-flagged entry is not counted"
    assert body["totals"] == {"optimal": 3, "max": 1, "offload": 1, "no": 1, "base": 1}
    assert body["new"] == 0
    assert body["settings"]["vram_headroom_pct"] == 15
    assert body["update"]["running"] is False
    assert body["update"]["catalog_origin"] == "refreshed"
    assert body["update"]["entries"] == LISTED


def test_hardware_reports_measured_facts_and_an_inferred_budget(client):
    body = client.get(API + "/hardware").json()
    assert body["gpus"][0]["vram_total_bytes"] == 24 * GIB
    assert body["memory"]["total_bytes"] == 64 * GIB
    budget = body["budget"]
    assert budget["vram_total"] == 24 * GIB
    assert 0 < budget["vram_safe"] < budget["vram_max"] < budget["vram_total"]
    assert 0 < budget["ram_safe"] < budget["ram_max"] < budget["ram_total"]
    assert budget["inferred"] is True
    assert body["tier"]["inferred"] is True
    assert client.get(API + "/hardware?refresh=true").status_code == 200
    assert_envelope(client.get(API + "/hardware?refresh=maybe"), 422, "VALIDATION_ERROR")


def test_live_returns_a_reading_the_budget_lines_and_optional_history(client):
    body = client.get(API + "/live").json()
    assert "history" not in body or body["history"] is None
    assert body["budget"] == {
        "vram_safe": body["budget"]["vram_safe"], "vram_max": 24 * GIB - fit.DISCRETE_SYSTEM_RESERVE,
        "vram_total": 24 * GIB, "ram_safe": body["budget"]["ram_safe"],
        "ram_total": 64 * GIB, "vram_headroom_pct": 15, "ram_headroom_pct": 25,
        "unified": False, "backend": "cuda"}
    assert body["accelerator"]["name"] == "NVIDIA GeForce RTX 4090"
    assert [g["vram_total_bytes"] for g in body["gpus"]] == [24 * GIB]
    assert body["gpus"][0]["util_percent"] is None, "no probe answered, so no reading"
    assert [v["mount"] for v in body["volumes"]]
    hardware.reset()
    with_history = client.get(API + "/live?history=true").json()
    assert isinstance(with_history["history"], list)
    assert len(with_history["history"]) >= 1
    assert {"t", "cpu", "mem", "gpu", "vram"} <= set(with_history["history"][0])


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def _page(body: dict) -> dict:
    """The paging facts this route sets (the shared model adds optional keys)."""
    return {k: body["page"][k] for k in ("limit", "offset", "total", "returned",
                                         "has_more")}


def _ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def test_the_catalog_lists_every_entry_with_its_verdict_and_never_the_adult_one(client):
    body = client.get(API + "/catalog").json()
    verdicts = {item["id"]: item["fit"]["verdict"] for item in body["items"]}
    assert verdicts == EXPECTED_VERDICTS
    assert "hf:someone/Adult-Thing" not in verdicts
    assert _page(body) == {"limit": 100, "offset": 0, "total": LISTED,
                           "returned": LISTED, "has_more": False}
    assert body["meta"] == {"sort": "recommended", "scope": None,
                            "catalog_total": LISTED, "inferred": True}
    assert set(body["facets"]) == {"kind", "verdict", "source"}
    assert {f["value"]: f["count"] for f in body["facets"]["kind"]} == {
        "image": 2, "video": 1, "llm": 2, "lora": 1, "upscale": 1}
    assert {f["value"]: f["count"] for f in body["facets"]["source"]} == {
        "huggingface": 4, "civitai": 1, "comfy_manager": 1, "ollama": 1}
    assert [f["value"] for f in body["facets"]["verdict"]] == [
        "optimal", "max", "offload", "base", "no"], "in the published verdict order"
    assert all("label" in f for f in body["facets"]["verdict"] + body["facets"]["kind"])


def test_a_catalog_row_carries_the_lead_variant_fit_and_score(client):
    rows = {r["id"]: r for r in client.get(API + "/catalog").json()["items"]}
    image = rows["hf:demo-labs/Demo-Image"]
    assert {"id", "name", "publisher", "kind", "kind_label", "source", "license",
            "downloads", "url", "curated", "is_new", "variant_count", "lead", "fit",
            "fit_reason", "security", "rank", "maximum", "stretch"} <= set(image)
    assert image["kind_label"] == "Image"
    assert image["variant_count"] == 2
    assert image["lead"]["precision"] == "fp8", "the largest comfortable variant leads"
    assert image["fit"]["optimum_variant"].endswith("demo_fp8.safetensors")
    assert image["fit"]["maximum_variant"].endswith("demo_fp8.safetensors")
    assert image["fit"]["stretch_variant"].endswith("demo_fp16.safetensors")
    assert image["stretch"]["verdict"] == "offload"
    assert image["maximum"]["verdict"] == "optimal"
    assert image["fit"]["inferred"] is True
    assert image["fit"]["runs"] is True
    assert image["fit_reason"]
    assert set(image["security"]) == {"score", "grade", "label", "tone", "inferred"}
    assert image["security"]["inferred"] is True
    assert image["security"]["grade"] == "A"
    assert image["security"]["score"] <= 99
    # A pickle scores well below the data-only files beside it.
    assert rows["comfy:models_upscale_models_4x_demo.pth"]["security"]["score"] < 50
    assert rows["hf:demo-labs/Demo-Giant"]["fit"]["runs"] is False
    assert rows["hf:demo-labs/Demo-Giant"]["maximum"] is None
    assert rows["civitai:4242"]["fit"]["verdict"] == "base"


def test_catalog_filter_by_kind_csv_and_repeated(client):
    assert set(_ids(client.get(API + "/catalog?kind=llm"))) == {
        "hf:bartowski/Demo-7B-GGUF", "ollama:demo:70b"}
    csv = _ids(client.get(API + "/catalog?kind=llm,lora"))
    repeated = _ids(client.get(API + "/catalog?kind=llm&kind=lora"))
    assert csv == repeated
    assert set(csv) == {"hf:bartowski/Demo-7B-GGUF", "ollama:demo:70b", "civitai:4242"}
    body = client.get(API + "/catalog?kind=llm").json()
    assert body["meta"]["scope"] == ["llm"]
    # The kind facet still counts the other kinds, so the filter can be widened.
    assert {f["value"] for f in body["facets"]["kind"]} == {
        "image", "video", "llm", "lora", "upscale"}
    assert {f["value"]: f["count"] for f in body["facets"]["verdict"]} == {
        "optimal": 1, "offload": 1}


def test_catalog_filter_by_verdict_source_and_runs_only(client):
    assert set(_ids(client.get(API + "/catalog?verdict=optimal"))) == {
        k for k, v in EXPECTED_VERDICTS.items() if v == "optimal"}
    assert _ids(client.get(API + "/catalog?verdict=max,no&sort=name")) == [
        "hf:demo-labs/Demo-Giant", "hf:demo-labs/Demo-Video"]
    assert _ids(client.get(API + "/catalog?source=civitai")) == ["civitai:4242"]
    runnable = set(_ids(client.get(API + "/catalog?runs_only=true")))
    assert runnable == set(EXPECTED_VERDICTS) - {"hf:demo-labs/Demo-Giant"}
    assert "civitai:4242" in runnable, "an add-on counts as usable"
    assert _ids(client.get(API + "/catalog?new_only=true")) == []


def test_catalog_text_search_is_case_insensitive_over_name_publisher_and_id(client):
    assert _ids(client.get(API + "/catalog?q=GIANT")) == ["hf:demo-labs/Demo-Giant"]
    assert _ids(client.get(API + "/catalog?q=bartowski")) == ["hf:bartowski/Demo-7B-GGUF"]
    assert _ids(client.get(API + "/catalog?q=ollama:demo")) == ["ollama:demo:70b"]
    assert _ids(client.get(API + "/catalog?q=%20%20")) != []
    assert _ids(client.get(API + "/catalog?q=no-such-model")) == []
    assert _ids(client.get(API + "/catalog?q=adult")) == []
    assert_envelope(client.get(API + "/catalog?q=" + "x" * 121), 422, "VALIDATION_ERROR")


def test_catalog_min_security_filter(client):
    rows = client.get(API + "/catalog").json()["items"]
    threshold = 70
    expected = {r["id"] for r in rows if r["security"]["score"] >= threshold}
    assert 0 < len(expected) < LISTED
    assert set(_ids(client.get(API + f"/catalog?min_security={threshold}"))) == expected
    assert_envelope(client.get(API + "/catalog?min_security=101"), 422,
                    "VALIDATION_ERROR")


@pytest.mark.parametrize("sort", service.SORTS)
def test_every_documented_sort_key_is_accepted_and_orders_the_rows(client, sort):
    body = client.get(API + f"/catalog?sort={sort}").json()
    assert body["meta"]["sort"] == sort
    rows = body["items"]
    assert len(rows) == LISTED
    keys = {
        "recommended": lambda r: -r["rank"],
        "name": lambda r: r["name"].lower(),
        "-downloads": lambda r: -(r["downloads"] or 0),
        "-updated": lambda r: r["updated_at"] or "",
        "need": lambda r: r["fit"]["need_bytes"] or 0,
        "-need": lambda r: -(r["fit"]["need_bytes"] or 0),
        "security": lambda r: r["security"]["score"],
        "-security": lambda r: -r["security"]["score"],
        "size": lambda r: r["lead"]["bytes"] or 0,
        "-size": lambda r: -(r["lead"]["bytes"] or 0),
    }
    values = [keys[sort](r) for r in rows]
    assert values == sorted(values, reverse=(sort == "-updated"))


def test_the_default_sort_puts_what_runs_well_first_and_what_cannot_run_last(client):
    ids = _ids(client.get(API + "/catalog"))
    assert ids[0] == "hf:demo-labs/Demo-Image"
    assert ids[-1] == "hf:demo-labs/Demo-Giant"


@pytest.mark.parametrize("query", [
    "sort=bogus", "sort=-name", "sort=", "sort=rank;drop", "kind=hologram",
    "verdict=perfect", "source=evil", "limit=0", "limit=501", "limit=abc",
    "offset=-1", "runs_only=perhaps",
])
def test_a_bad_catalog_query_is_a_validation_envelope(client, query):
    assert_envelope(client.get(API + f"/catalog?{query}"), 422, "VALIDATION_ERROR")


def test_catalog_paging(client):
    everything = _ids(client.get(API + "/catalog?sort=name"))
    first = client.get(API + "/catalog?sort=name&limit=3").json()
    assert [r["id"] for r in first["items"]] == everything[:3]
    assert _page(first) == {"limit": 3, "offset": 0, "total": LISTED, "returned": 3,
                            "has_more": True}
    second = client.get(API + "/catalog?sort=name&limit=3&offset=3").json()
    assert [r["id"] for r in second["items"]] == everything[3:6]
    last = client.get(API + "/catalog?sort=name&limit=3&offset=6").json()
    assert [r["id"] for r in last["items"]] == everything[6:]
    assert last["page"]["has_more"] is False
    assert last["page"]["returned"] == LISTED - 6
    beyond = client.get(API + "/catalog?limit=3&offset=600").json()
    assert beyond["items"] == []
    assert beyond["page"]["total"] == LISTED


# ---------------------------------------------------------------------------
# One entry
# ---------------------------------------------------------------------------

def test_entry_detail_assesses_every_variant_and_explains_the_score(client):
    body = client.get(API + "/catalog/entry",
                      params={"id": "hf:demo-labs/Demo-Image"}).json()
    assert body["name"] == "Demo Image"
    assert body["kind_label"] == "Image"
    assert [v["precision"] for v in body["variants"]] == ["fp16", "fp8"]
    fp16, fp8 = body["variants"]
    assert fp16["fit"]["verdict"] == "offload"
    assert fp8["fit"]["verdict"] == "optimal"
    for variant in body["variants"]:
        assert variant["fit"]["inferred"] is True
        assert variant["fit"]["requirement"]["inferred"] is True
        assert variant["fit"]["requirement"]["companion_bytes"] == 5 * GIB + 300 * MIB
        assert "factors" not in variant["security"]
        assert variant["security"]["inferred"] is True
        assert variant["fit"]["disk_ok"] is True
    assert body["fit"]["lead_variant"] == fp8["id"]
    assert "variants" not in body["fit"]
    assert set(body["budget"]) == BUDGET_KEYS
    security = body["security"]
    assert security["inferred"] is True
    assert "not a malware scan" in security["disclaimer"]
    assert {f["id"] for f in security["factors"]} >= {"format", "scan", "publisher"}
    assert security["score"] == min(99, 40 + sum(f["points"] for f in security["factors"]))
    assert body["installed"] == [], "nothing is indexed in this vault"
    assert body["is_new"] is False
    assert body["first_seen_at"] is None


@pytest.mark.parametrize("entry_id", ["hf:nobody/Nothing", "civitai:0", "xyz",
                                      "hf:../../etc/passwd",
                                      "hf:someone/Adult-Thing",
                                      "https://evil.test/model"])
def test_an_unknown_entry_id_is_a_404_envelope(client, entry_id):
    error = assert_envelope(client.get(API + "/catalog/entry", params={"id": entry_id}),
                            404, "NOT_FOUND")
    assert error["details"]["id"] == entry_id


@pytest.mark.parametrize("query", ["", "?id=", "?id=ab", "?id=" + "x" * 241])
def test_a_missing_or_malformed_entry_id_is_a_validation_envelope(client, query):
    assert_envelope(client.get(API + "/catalog/entry" + query), 422, "VALIDATION_ERROR")


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def test_recommendations_group_by_kind_in_interface_order(client):
    body = client.get(API + "/recommendations").json()
    assert body["inferred"] is True
    assert body["catalog_total"] == LISTED
    assert [g["kind"] for g in body["groups"]] == ["image", "video", "llm", "lora",
                                                  "upscale"]
    assert body["scope"] == list(service.KIND_ORDER)
    assert body["totals"] == {"optimal": 3, "good": 0, "max": 1, "offload": 1,
                              "cpu": 0, "base": 1, "no": 1, "unknown": 0}
    assert body["runnable"] == 5, "optimal + max + offload; the add-on is not a verdict that runs"
    groups = {g["kind"]: g for g in body["groups"]}
    for group in groups.values():
        assert {"kind", "label", "addon", "total", "counts", "optimum", "maximum",
                "stretch"} <= set(group)

    image = groups["image"]
    assert image["total"] == 2
    assert image["counts"] == {"optimal": 1, "no": 1}
    assert [r["id"] for r in image["optimum"]] == ["hf:demo-labs/Demo-Image"]
    assert [r["id"] for r in image["maximum"]] == ["hf:demo-labs/Demo-Image"]
    assert [r["id"] for r in image["stretch"]] == ["hf:demo-labs/Demo-Image"]

    video = groups["video"]
    assert video["optimum"] == [], "nothing comfortable"
    assert [r["id"] for r in video["maximum"]] == ["hf:demo-labs/Demo-Video"]

    llm = groups["llm"]
    assert [r["id"] for r in llm["optimum"]] == ["hf:bartowski/Demo-7B-GGUF"]
    assert [r["id"] for r in llm["maximum"]] == ["hf:bartowski/Demo-7B-GGUF"], \
        "the offload-only model is not a GPU maximum"

    lora = groups["lora"]
    assert lora["addon"] is True
    assert [r["id"] for r in lora["optimum"]] == ["civitai:4242"]
    assert lora["maximum"] == [] and lora["stretch"] == []


def test_recommendations_can_be_limited_to_some_kinds_and_a_group_size(client):
    body = client.get(API + "/recommendations?kind=llm,video&per_kind=1").json()
    assert [g["kind"] for g in body["groups"]] == ["video", "llm"]
    assert body["scope"] == ["video", "llm"]
    assert all(len(g["optimum"]) <= 1 for g in body["groups"])
    for query in ("kind=hologram", "per_kind=0", "per_kind=21"):
        assert_envelope(client.get(API + f"/recommendations?{query}"), 422,
                        "VALIDATION_ERROR")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_defaults(client):
    body = client.get(API + "/settings").json()
    assert body["vram_headroom_pct"] == 15
    assert body["ram_headroom_pct"] == 25
    assert body["auto_update"] is True
    assert body["kinds"] == []
    assert body["online_enabled"] is False
    assert body["limits"] == {"min_headroom_pct": 5, "max_headroom_pct": 50}
    assert [o["id"] for o in body["kind_options"]] == list(service.KIND_ORDER)


def test_patch_settings_round_trips_and_changes_the_budget(client):
    before = client.get(API + "/hardware").json()["budget"]
    response = client.patch(API + "/settings", json={"vram_headroom_pct": 40,
                                                     "ram_headroom_pct": 10})
    assert response.status_code == 200, response.text
    assert response.json()["vram_headroom_pct"] == 40
    assert response.json()["ram_headroom_pct"] == 10
    assert client.get(API + "/settings").json()["vram_headroom_pct"] == 40

    after = client.get(API + "/hardware").json()["budget"]
    assert after["vram_headroom_pct"] == 40
    assert after["vram_safe"] == int(24 * GIB * 0.60) - fit.DISCRETE_SYSTEM_RESERVE
    assert after["vram_safe"] < before["vram_safe"]
    assert after["vram_max"] == before["vram_max"], "the ceiling does not move"
    assert after["ram_safe"] > before["ram_safe"]

    # The verdicts follow at once: more headroom, same or worse - never better.
    rows = {r["id"]: r["fit"]["verdict"]
            for r in client.get(API + "/catalog").json()["items"]}
    assert rows["hf:demo-labs/Demo-Image"] == "max"
    for entry_id, verdict in rows.items():
        assert fit.verdict_rank(verdict) >= fit.verdict_rank(EXPECTED_VERDICTS[entry_id])
    assert client.get(API + "/safe-config").json()["budget"]["vram_headroom_pct"] == 40
    assert client.get(API + "/live").json()["budget"]["vram_headroom_pct"] == 40


def test_patch_settings_changes_only_the_fields_it_is_given(client):
    client.patch(API + "/settings", json={"vram_headroom_pct": 20})
    body = client.patch(API + "/settings", json={"auto_update": False}).json()
    assert body["vram_headroom_pct"] == 20
    assert body["ram_headroom_pct"] == 25
    assert body["auto_update"] is False
    assert client.get(API + "/update/status").json()["auto_update"] is False
    assert client.patch(API + "/settings", json={}).json() == body


def test_the_selected_kinds_scope_the_catalog_the_overview_and_the_picks(client):
    body = client.patch(API + "/settings", json={"kinds": ["lora", "llm", "llm"]}).json()
    assert body["kinds"] == ["llm", "lora"], "deduplicated, in interface order"
    assert set(_ids(client.get(API + "/catalog"))) == {
        "hf:bartowski/Demo-7B-GGUF", "ollama:demo:70b", "civitai:4242"}
    assert client.get(API + "/overview").json()["catalog_total"] == 3
    assert [g["kind"] for g in client.get(API + "/recommendations").json()["groups"]] \
        == ["llm", "lora"]
    # An explicit filter still reaches outside the saved scope.
    assert _ids(client.get(API + "/catalog?kind=video")) == ["hf:demo-labs/Demo-Video"]
    assert client.patch(API + "/settings", json={"kinds": []}).json()["kinds"] == []
    assert len(_ids(client.get(API + "/catalog"))) == LISTED


def test_patch_settings_requires_the_csrf_header(advisor):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as naked:
        response = naked.patch(API + "/settings", json={"vram_headroom_pct": 40})
        assert_envelope(response, 400, "CSRF_HEADER_MISSING")
        assert naked.get(API + "/settings").json()["vram_headroom_pct"] == 15


@pytest.mark.parametrize("method,path,body", [
    ("PATCH", "/settings", {"vram_headroom_pct": 40}),
    ("POST", "/update", {}),
    ("POST", "/update/cancel", None),
    ("POST", "/catalog/verify-link", {"id": "hf:demo-labs/Demo-Image"}),
    ("POST", "/sources/pins", {"repo": "owner/name"}),
    ("DELETE", "/sources/pins?repo=owner/name", None),
])
def test_every_mutating_advisor_route_requires_the_csrf_header(advisor, method, path,
                                                               body):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as naked:
        response = naked.request(method, API + path, json=body)
        assert_envelope(response, 400, "CSRF_HEADER_MISSING")


@pytest.mark.parametrize("body", [
    {"vram_headroom_pct": 4}, {"vram_headroom_pct": 0}, {"vram_headroom_pct": 51},
    {"vram_headroom_pct": 100}, {"vram_headroom_pct": -15},
    {"ram_headroom_pct": 4}, {"ram_headroom_pct": 51}, {"ram_headroom_pct": 100},
    {"vram_headroom_pct": "lots"}, {"vram_headroom_pct": 12.5},
    {"kinds": ["hologram"]}, {"kinds": "llm"}, {"auto_update": "sometimes"},
    {"online_enabled": True},            # not this route's to change
    {"advisor_vram_headroom_pct": 30},   # the raw config key is not accepted
    {"unknown": 1},
])
def test_an_out_of_range_or_unknown_setting_is_refused_and_nothing_changes(client, body):
    error = assert_envelope(client.patch(API + "/settings", json=body), 422,
                            "VALIDATION_ERROR")
    assert error.get("field_errors") or error.get("details")
    after = client.get(API + "/settings").json()
    assert (after["vram_headroom_pct"], after["ram_headroom_pct"]) == (15, 25)
    assert after["online_enabled"] is False


def test_headroom_can_never_be_set_to_plan_the_whole_card(client):
    assert client.patch(API + "/settings", json={"vram_headroom_pct": 5}).status_code == 200
    budget = client.get(API + "/hardware").json()["budget"]
    assert budget["vram_safe"] < budget["vram_max"] < budget["vram_total"]
    assert budget["vram_safe"] <= budget["vram_total"] * 0.95
    # A value written past the route (straight into config) is still clamped.
    config_service.set_config({"advisor_vram_headroom_pct": 0,
                               "advisor_ram_headroom_pct": 100})
    service._invalidate()
    body = client.get(API + "/settings").json()
    assert (body["vram_headroom_pct"], body["ram_headroom_pct"]) == (5, 50)
    budget = client.get(API + "/hardware").json()["budget"]
    assert budget["vram_safe"] < budget["vram_max"] < budget["vram_total"]


# ---------------------------------------------------------------------------
# Safe configuration
# ---------------------------------------------------------------------------

def _reserve_vram(body: dict) -> float:
    flag = next(f for f in body["comfyui"]["flags"] if f["flag"] == "--reserve-vram")
    return float(flag["value"])


@pytest.mark.parametrize("headroom", [5, 15, 30, 50])
def test_safe_config_never_suggests_reserving_zero_on_a_discrete_gpu(client, headroom):
    client.patch(API + "/settings", json={"vram_headroom_pct": headroom})
    body = client.get(API + "/safe-config").json()
    reserve = _reserve_vram(body)
    assert reserve > 0
    budget = body["budget"]
    assert reserve == round((budget["vram_total"] - budget["vram_safe"]) / GIB, 1)
    # At least the system share plus the asked-for headroom of a 24 GiB card.
    assert reserve >= round(0.8 + 24 * headroom / 100, 1) - 0.05
    assert f"--reserve-vram {reserve:.1f}" in body["comfyui"]["command_line"]
    overhead = next(e for e in body["ollama"]["env"]
                    if e["name"] == "OLLAMA_GPU_OVERHEAD")
    assert int(overhead["value"]) == budget["vram_total"] - budget["vram_safe"] > 0


def test_safe_config_is_text_to_copy_and_says_it_is_an_estimate(client):
    body = client.get(API + "/safe-config").json()
    assert body["inferred"] is True
    assert "24 GB GPU" in body["summary"]
    assert "15%" in body["summary"]
    assert [f["name"] for f in body["files"]] == ["comfyui_safe_launch.txt",
                                                  "advisor_safe.env"]
    launch, env = (f["content"] for f in body["files"])
    assert "--reserve-vram" in launch
    assert "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" in env
    assert "OLLAMA_MAX_LOADED_MODELS=1" in env
    for row in body["comfyui"]["flags"] + body["comfyui"]["env"] + body["ollama"]["env"] \
            + body["llama_cpp"]["args"]:
        assert row["why"], "every suggestion explains itself"
    assert "--lowvram" not in body["comfyui"]["command_line"], "24 GB needs no VRAM mode"
    assert "--threads 16" in body["llama_cpp"]["command_line"]
    assert len(body["rules"]) >= 3


def test_safe_config_for_other_machines_keeps_headroom_too():
    """Straight through the builder, for the shapes the fake profile is not."""
    from app.advisor import safe_config

    def build(backend, vram, ram, unified=False):
        profile = {"accelerator": {"backend": backend, "vram_total_bytes": vram,
                                   "unified": unified},
                   "memory": {"total_bytes": ram}, "cpu": {"physical_cores": 8}}
        return safe_config.build(profile, fit.make_budget(profile))

    for vram_gib in (2, 4, 6, 8, 12, 16, 48):
        body = build("cuda", vram_gib * GIB, 32 * GIB)
        assert _reserve_vram(body) > 0, vram_gib
    assert "--novram" in build("cuda", 4 * GIB, 16 * GIB)["comfyui"]["command_line"]
    assert "--lowvram" in build("cuda", 8 * GIB, 16 * GIB)["comfyui"]["command_line"]
    assert "--directml" in build("directml", 16 * GIB, 32 * GIB)["comfyui"]["command_line"]

    mac = build("mps", int(32 * GIB * 0.75), 32 * GIB, unified=True)
    names = {e["name"]: float(e["value"]) for e in mac["comfyui"]["env"]}
    assert names["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] == 0.85
    assert names["PYTORCH_MPS_LOW_WATERMARK_RATIO"] < names["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] < 1
    assert not any(f["flag"] == "--reserve-vram" for f in mac["comfyui"]["flags"])
    assert not any(e["name"] == "OLLAMA_GPU_OVERHEAD" for e in mac["ollama"]["env"])

    cpu = build("cpu", 0, 16 * GIB)
    assert cpu["comfyui"]["command_line"] == "--cpu"
    assert "--n-gpu-layers 0" in cpu["llama_cpp"]["command_line"]


# ---------------------------------------------------------------------------
# Installed models
# ---------------------------------------------------------------------------

def test_installed_is_an_empty_list_on_an_unindexed_vault(client):
    body = client.get(API + "/installed").json()
    assert body["items"] == []
    assert _page(body) == {"limit": 100, "offset": 0, "total": 0, "returned": 0,
                           "has_more": False}
    assert body["facets"] == {"verdict": [], "kind": []}
    assert body["meta"] == {"sort": "-need", "indexed_total": 0, "inferred": True}
    for sort in ("need", "-need", "size", "-size", "security", "-security", "name"):
        assert client.get(API + f"/installed?sort={sort}").status_code == 200, sort
    assert_envelope(client.get(API + "/installed?sort=recommended"), 422,
                    "VALIDATION_ERROR")
    assert_envelope(client.get(API + "/installed?kind=hologram"), 422,
                    "VALIDATION_ERROR")


def test_an_unknown_installed_model_is_a_404_and_a_bad_id_a_422(client):
    error = assert_envelope(client.get(API + "/installed/999999"), 404, "NOT_FOUND")
    assert error["details"]["id"] == 999999
    assert_envelope(client.get(API + "/installed/not-a-number"), 422, "VALIDATION_ERROR")


def test_installed_assesses_the_models_the_index_holds(client):
    """Scan the synthetic install, then read it back through the advisor."""
    import time

    from app.indexing.service import get_indexer

    indexer = get_indexer()
    indexer.start(mode="full", force=True, enrich_online=False, trigger="test")
    deadline = time.monotonic() + 60
    while indexer.status().get("running") and time.monotonic() < deadline:
        time.sleep(0.05)
    body = client.get(API + "/installed?sort=name").json()
    names = [item["filename"] for item in body["items"]]
    assert "sd15-probe.safetensors" in names
    assert "probe-lora.safetensors" in names
    assert body["meta"]["indexed_total"] == len(body["items"]) >= 3
    by_name = {item["filename"]: item for item in body["items"]}
    lora = by_name["probe-lora.safetensors"]
    assert lora["kind"] == "lora"
    assert lora["fit"]["verdict"] == "base"
    checkpoint = by_name["sd15-probe.safetensors"]
    assert checkpoint["kind"] == "image"
    assert checkpoint["fit"]["verdict"] == "optimal"
    for item in body["items"]:
        assert item["uid"] == f"model:{item['id']}"
        assert item["fit"]["inferred"] is True
        assert item["requirement"]["inferred"] is True
        assert item["security"]["inferred"] is True
        assert 0 <= item["security"]["score"] <= 99
        assert item["security"]["factors"]
    assert set(_ids(client.get(API + "/installed?kind=lora"))) == {lora["id"]}

    detail = client.get(API + f"/installed/{checkpoint['id']}").json()
    assert detail["uid"] == checkpoint["uid"]
    assert set(detail["budget"]) == BUDGET_KEYS
    assert detail["abs_path"].endswith("sd15-probe.safetensors")
    assert {"hash_state", "integrity"} <= set(detail)


# ---------------------------------------------------------------------------
# Sources and pins
# ---------------------------------------------------------------------------

def test_sources_reports_where_information_comes_from(client):
    body = client.get(API + "/sources").json()
    assert body["revision"] == 1
    assert body["origin"] == "seed"
    assert [s["id"] for s in body["sources"]] == [
        "huggingface", "civitai", "comfy_manager", "openmodeldb", "ollama", "github"]
    by_id = {s["id"]: s for s in body["sources"]}
    assert by_id["huggingface"]["entries"] == 4
    assert by_id["civitai"]["entries"] == 1
    assert by_id["openmodeldb"]["entries"] == 0
    assert by_id["github"]["entries"] == 1, "for GitHub the count is the leads"
    assert by_id["huggingface"]["last_ok"] is None, "never refreshed on this machine"
    assert body["pins"] == 1
    assert body["user_pins"] == []
    assert body["learned"] == [] and body["learned_total"] == 0
    assert body["queries"] == {"huggingface": 1, "civitai": 1, "github": 1,
                               "openmodeldb": 1, "follow_base_models": 1}
    assert body["publishers"] == {"official": 1, "trusted": 1}
    assert body["allowed_hosts"] == sorted(hosts.CATALOG_HOSTS_EXACT)
    assert body["leads"][0]["name"] == "demo-labs/demo-video"
    assert body["counts"]["total"] == LISTED
    assert set(body["update"]) >= UPDATE_KEYS


def test_a_pin_round_trip(client, advisor):
    created = client.post(API + "/sources/pins", json={"repo": "owner/name",
                                                       "kind": "video"})
    assert created.status_code == 200, created.text
    pin = created.json()
    assert pin["repo"] == "owner/name"
    assert pin["kind"] == "video"
    assert isinstance(pin["added_at"], int)
    assert [p["repo"] for p in client.get(API + "/sources").json()["user_pins"]] == [
        "owner/name"]
    state = json.loads((advisor / "state.json").read_text(encoding="utf-8"))
    assert [p["repo"] for p in state["user_pins"]] == ["owner/name"]

    # Pinning the same repository again replaces it rather than duplicating it.
    client.post(API + "/sources/pins", json={"repo": "owner/name"})
    client.post(API + "/sources/pins", json={"repo": "other/model"})
    pins = client.get(API + "/sources").json()["user_pins"]
    assert [p["repo"] for p in pins] == ["owner/name", "other/model"]
    assert "kind" not in pins[0]

    removed = client.delete(API + "/sources/pins", params={"repo": "owner/name"})
    assert removed.status_code == 200
    assert removed.json() == {"removed": True, "repo": "owner/name"}
    again = client.delete(API + "/sources/pins", params={"repo": "owner/name"})
    assert again.json() == {"removed": False, "repo": "owner/name"}
    assert [p["repo"] for p in client.get(API + "/sources").json()["user_pins"]] == [
        "other/model"]


def test_a_pasted_hub_page_address_is_reduced_to_the_repository_id(client):
    pin = client.post(API + "/sources/pins",
                      json={"repo": "https://huggingface.co/owner/name/"}).json()
    assert pin["repo"] == "owner/name"


@pytest.mark.parametrize("repo", [
    "../x", "a/b/c", "a b/c", "owner", "owner/", "/name", "a/b?x=1", "a/b#frag",
    "a@evil.test/b", "https://evil.test/owner/name", "http://huggingface.co/owner/name",
    "https://huggingface.co/owner/name/tree/main", "https://huggingface.co/",
    "a/..", "..", "a\\b/c", "a/b&c",
])
def test_an_invalid_repository_id_is_refused_and_not_stored(client, advisor, repo):
    error = assert_envelope(client.post(API + "/sources/pins", json={"repo": repo}),
                            422, "VALIDATION_ERROR")
    assert error
    assert client.get(API + "/sources").json()["user_pins"] == []
    assert not (advisor / "state.json").exists()


@pytest.mark.parametrize("body", [
    {}, {"repo": ""}, {"repo": "ab"}, {"repo": "x" * 241}, {"repo": 5},
    {"repo": "owner/name", "kind": "hologram"},
    {"repo": "owner/name", "url": "https://evil.test/x"},
    {"url": "https://huggingface.co/owner/name"},
])
def test_a_malformed_pin_body_is_a_validation_envelope(client, body):
    assert_envelope(client.post(API + "/sources/pins", json=body), 422,
                    "VALIDATION_ERROR")
    assert client.get(API + "/sources").json()["user_pins"] == []


def test_removing_a_pin_needs_a_repo_parameter(client):
    assert_envelope(client.delete(API + "/sources/pins"), 422, "VALIDATION_ERROR")
    assert client.delete(API + "/sources/pins",
                         params={"repo": "never/pinned"}).json()["removed"] is False


# ---------------------------------------------------------------------------
# The kill-switch: nothing leaves while outbound lookups are off
# ---------------------------------------------------------------------------

class _NoThread:
    """Replaces ``threading.Thread`` inside the updater; starting one fails."""

    started = 0

    def __init__(self, *args, **kwargs) -> None:
        type(self).started += 1
        raise AssertionError("a refresh thread was created while offline")


def test_update_while_offline_is_feature_unavailable_and_starts_no_thread(
        client, monkeypatch):
    _NoThread.started = 0
    monkeypatch.setattr(updater.threading, "Thread", _NoThread)
    for body in ({}, None, {"kinds": ["llm"]}, {"sources": ["huggingface"]}):
        error = assert_envelope(client.post(API + "/update", json=body), 503,
                                "FEATURE_UNAVAILABLE")
        assert error["details"]["reason"] == "online_disabled"
        assert "hint" in error["details"]
    assert _NoThread.started == 0
    status = client.get(API + "/update/status").json()
    assert status["running"] is False
    assert status["online_enabled"] is False
    assert status["started_at"] is None
    assert status["last_run"] is None


def test_verify_link_while_offline_is_feature_unavailable(client):
    # ``sources.head_status`` is the fixture's tripwire: reaching it fails the test.
    error = assert_envelope(
        client.post(API + "/catalog/verify-link",
                    json={"id": "hf:demo-labs/Demo-Image"}), 503, "FEATURE_UNAVAILABLE")
    assert error["details"]["reason"] == "online_disabled"
    # Offline is checked before the id, so an unknown id is not probed either.
    assert_envelope(client.post(API + "/catalog/verify-link",
                                json={"id": "hf:nobody/Nothing"}), 503,
                    "FEATURE_UNAVAILABLE")


def test_cancel_while_idle_is_harmless(client):
    response = client.post(API + "/update/cancel")
    assert response.status_code == 200
    assert response.json()["running"] is False


@pytest.mark.parametrize("body", [
    {"id": "hf:demo-labs/Demo-Image", "url": "https://evil.test/x"},
    {"url": "https://evil.test/x"},
    {"id": "ab"}, {"id": ""}, {}, {"id": "hf:a/b", "variant": "x" * 401},
])
def test_verify_link_accepts_an_id_never_a_url(client, body):
    go_online()
    assert_envelope(client.post(API + "/catalog/verify-link", json=body), 422,
                    "VALIDATION_ERROR")


def test_verify_link_online_checks_only_the_stored_link(client, monkeypatch):
    go_online()
    asked: list[str] = []

    def head(url, *, client=None):
        asked.append(url)
        return {"state": "ok", "status": 302, "bytes": 12 * GIB,
                "message": "The link is live."}

    monkeypatch.setattr(sources, "head_status", head)
    fp8 = "split_files/diffusion_models/demo_fp8.safetensors"
    body = client.post(API + "/catalog/verify-link",
                       json={"id": "hf:demo-labs/Demo-Image", "variant": fp8}).json()
    assert body["state"] == "ok"
    assert body["size_matches"] is True
    assert isinstance(body["checked_at"], int)
    assert asked == [body["url"]]
    assert body["url"] == f"https://huggingface.co/demo-labs/x/resolve/main/{fp8}"

    # Without a variant, the entry's first variant is checked.
    client.post(API + "/catalog/verify-link", json={"id": "hf:demo-labs/Demo-Image"})
    assert asked[-1].endswith("demo_fp16.safetensors")
    assert_envelope(client.post(API + "/catalog/verify-link",
                                json={"id": "hf:nobody/Nothing"}), 404, "NOT_FOUND")
    assert_envelope(client.post(API + "/catalog/verify-link",
                                json={"id": "hf:demo-labs/Demo-Image",
                                      "variant": "no-such-file"}), 404, "NOT_FOUND")
    assert len(asked) == 2, "nothing was requested for the two 404s"


# ---------------------------------------------------------------------------
# The updater, fed canned documents
# ---------------------------------------------------------------------------

def _sibling(path: str, size: float, sha: str | None = SHA) -> dict:
    return {"rfilename": path, "size": int(size),
            "lfs": {"size": int(size), "sha256": sha}}


HUB_DETAILS = {
    "demo-labs/Demo-Image": {
        "id": "demo-labs/Demo-Image", "pipeline_tag": "text-to-image", "sha": "rev-2",
        "tags": ["license:apache-2.0"], "downloads": 60_000,
        "securityRepoStatus": {"scansDone": True, "filesWithIssues": []},
        "siblings": [
            _sibling("split_files/diffusion_models/demo_fp8.safetensors", 12 * GIB),
            _sibling("split_files/text_encoders/t5xxl_fp8.safetensors", 5 * GIB)]},
    "bartowski/Fresh-9B-GGUF": {
        "id": "bartowski/Fresh-9B-GGUF", "pipeline_tag": "text-generation",
        "sha": "rev-1", "tags": ["gguf", "base_model:demo-labs/Fresh-9B", "license:mit"],
        "siblings": [_sibling("Fresh-9B-Q4_K_M.gguf", 5.5 * GIB)]},
    # reached only by following the base_model link above
    "demo-labs/Fresh-9B": {
        "id": "demo-labs/Fresh-9B", "pipeline_tag": "text-generation", "sha": "rev-1",
        "tags": ["license:mit"],
        "siblings": [_sibling("model.safetensors", 18 * GIB)]},
}
HUB_LISTING = [{"id": "bartowski/Fresh-9B-GGUF", "sha": "rev-1"},
               {"id": "../../not-a-repo", "sha": "x"}]
CIVITAI_PAGE = {"items": [{
    "id": 777, "name": "Fresh Style", "type": "LORA", "nsfw": False,
    "creator": {"username": "artist"}, "stats": {"downloadCount": 10},
    "modelVersions": [{"id": 9100, "name": "v1", "baseModel": "SDXL 1.0",
                       "publishedAt": "2026-10-01T00:00:00.000Z",
                       "files": [{"name": "fresh_style.safetensors", "type": "Model",
                                  "primary": True, "sizeKB": 150_000.0,
                                  "metadata": {"format": "SafeTensor"},
                                  "pickleScanResult": "Success",
                                  "virusScanResult": "Success"}]}]},
    {"id": 778, "name": "Adult Style", "type": "LORA", "nsfw": True,
     "modelVersions": []}]}
MANAGER_LIST = {"models": [{
    "name": "Fresh VAE", "type": "VAE", "base": "SDXL", "save_path": "default",
    "filename": "fresh_vae.safetensors", "size": "319.14MB",
    "reference": "https://huggingface.co/demo-labs/fresh-vae",
    "url": "https://huggingface.co/demo-labs/fresh-vae/resolve/main/fresh_vae.safetensors"}]}
OPENMODELDB = {"4x-Fresh": {
    "name": "4x Fresh", "author": "alice", "scale": 4, "date": "2026-08-01",
    "resources": [{"platform": "pytorch", "type": "safetensors", "size": 66_000_000,
                   "urls": ["https://example.test/4x-fresh.safetensors"]}]}}
OLLAMA_TAGS = {"models": [
    {"name": "fresh:9b", "size": 5_500_000_000, "digest": "f" * 64,
     "details": {"family": "fresh", "quantization_level": "Q4_K_M"}},
    {"name": "fresh:cloud", "size": 0}]}
GITHUB_PAGE = {"items": [
    {"full_name": "demo-labs/fresh-video", "stargazers_count": 900, "fork": False,
     "html_url": "https://github.com/demo-labs/fresh-video", "archived": False},
    {"full_name": "someone/fork", "stargazers_count": 9000, "fork": True,
     "html_url": "https://github.com/someone/fork"}]}


class FakeUpstream:
    """Stands in for ``sources.fetch_json``.  Records and allowlist-checks every URL."""

    def __init__(self, *, failing: tuple[str, ...] = (), hub_details: dict | None = None,
                 hub_listing: list | None = None) -> None:
        self.failing = set(failing)
        self.hub_details = dict(HUB_DETAILS if hub_details is None else hub_details)
        self.hub_listing = list(HUB_LISTING if hub_listing is None else hub_listing)
        self.urls: list[str] = []
        self.clients: list[object] = []
        self.lock = threading.Lock()
        self.on_request = None

    def _fail(self, source: str) -> None:
        if source in self.failing or "all" in self.failing:
            raise UpstreamUnavailable(f"{source} is down (canned).",
                                      details={"host": source})

    def __call__(self, url, *, client=None, max_bytes=None, accept_statuses=()):
        checked = hosts.check(url, kind=hosts.KIND_CATALOG)   # never off the allowlist
        with self.lock:
            self.urls.append(url)
            self.clients.append(client)
        if self.on_request is not None:
            self.on_request(url)
        if url == sources.VAULT_SOURCES:
            self._fail("vault")
            return 404, None
        if url == sources.COMFY_MANAGER_LIST:
            self._fail("comfy_manager")
            return 200, MANAGER_LIST
        if url == sources.OPENMODELDB_LIST:
            self._fail("openmodeldb")
            return 200, OPENMODELDB
        if url == sources.OLLAMA_TAGS:
            self._fail("ollama")
            return 200, OLLAMA_TAGS
        if checked.host == "api.github.com":
            self._fail("github")
            return 200, GITHUB_PAGE
        if checked.host == "civitai.com":
            self._fail("civitai")
            return 200, CIVITAI_PAGE
        assert checked.host == "huggingface.co", url
        self._fail("huggingface")
        if url.startswith(sources.HF_BASE + "/api/models?"):
            return 200, self.hub_listing
        repo = url[len(sources.HF_BASE + "/api/models/"):].split("?", 1)[0]
        if repo not in self.hub_details:
            assert 404 in accept_statuses
            return 404, None
        return 200, self.hub_details[repo]


@pytest.fixture
def upstream(advisor, monkeypatch):
    fake = FakeUpstream()
    monkeypatch.setattr(sources, "fetch_json", fake)
    return fake


def _stored_ids() -> set[str]:
    catalog.invalidate()
    return {e["id"] for e in catalog.load()["entries"]}


def test_a_refresh_writes_a_new_catalog_from_every_source(advisor, upstream):
    before = _stored_ids()
    updater._run("manual", None, None)

    assert (advisor / "catalog.json").is_file()
    assert (advisor / "state.json").is_file()
    doc = json.loads((advisor / "catalog.json").read_text(encoding="utf-8"))
    assert doc["schema"] == 1
    assert doc["generator"].startswith("Geekatplay ComfyUI Asset Vault")
    loaded = catalog.load()
    assert loaded["origin"] == "refreshed"
    ids = {e["id"] for e in loaded["entries"]}
    assert ids == {
        "hf:demo-labs/Demo-Image",          # pinned
        "hf:bartowski/Fresh-9B-GGUF",       # discovered by listing
        "hf:demo-labs/Fresh-9B",            # found by following base_model one hop
        "civitai:777",
        "comfy:models_vae_fresh_vae.safetensors",
        "omdb:4x-Fresh",
        "ollama:fresh:9b",
    }
    assert ids != before
    by_id = loaded["by_id"]
    assert by_id["hf:demo-labs/Demo-Image"]["revision"] == "rev-2"
    assert by_id["hf:demo-labs/Demo-Image"]["curated"] is True, "from the pin's hint"
    assert by_id["hf:demo-labs/Demo-Image"]["safety"]["publisher_trust"] == "official"
    assert by_id["hf:bartowski/Fresh-9B-GGUF"]["safety"]["publisher_trust"] == "trusted"
    assert by_id["hf:bartowski/Fresh-9B-GGUF"]["kind"] == "llm"
    assert "civitai:778" not in ids, "adult-flagged rows are dropped at the source"
    assert "ollama:fresh:cloud" not in ids
    assert [lead["name"] for lead in loaded["leads"]] == ["demo-labs/fresh-video"]

    state = catalog.load_state()
    assert set(state["sources"]) == set(updater.SOURCE_IDS)
    assert all(row["ok"] for row in state["sources"].values())
    assert state["sources"]["huggingface"]["entries"] == 3
    assert state["learned"] == ["demo-labs/Fresh-9B"]
    run = state["last_run"]
    assert run["trigger"] == "manual"
    assert run["error"] is None
    assert run["result"]["entries"] == 7
    assert run["result"]["failed_sources"] == []
    assert run["result"]["counts"]["by_source"]["huggingface"] == 3
    status = updater.status()
    assert status["running"] is False
    assert status["result"]["entries"] == 7
    assert status["stale"] is False


def test_a_refresh_only_ever_asks_allowlisted_urls_with_one_shared_client(
        advisor, upstream):
    updater._run("manual", None, None)
    assert len(upstream.urls) >= 9
    for url in upstream.urls:
        assert hosts.check(url, kind=hosts.KIND_CATALOG).url == url
        assert url.startswith("https://")
    assert {hosts.check(u, kind=hosts.KIND_CATALOG).host for u in upstream.urls} <= \
        set(hosts.CATALOG_HOSTS_EXACT)
    assert all(c is not None for c in upstream.clients)
    assert len({id(c) for c in upstream.clients}) == 1
    client = upstream.clients[0]
    sent = {k.lower(): v for k, v in client.headers.items()}
    assert sent["user-agent"] == sources.USER_AGENT
    assert "authorization" not in sent and "cookie" not in sent
    assert client.follow_redirects is False
    assert client.is_closed
    # Nothing about this machine or its library is ever part of a URL.
    blob = " ".join(upstream.urls)
    for needle in ("4090", "RTX", "vram", "Ryzen", "probe-lora", "sd15-probe"):
        assert needle not in blob


def test_a_failing_source_keeps_the_entries_it_had(advisor, monkeypatch):
    before = _stored_ids()
    fake = FakeUpstream(failing=("comfy_manager", "ollama"))
    monkeypatch.setattr(sources, "fetch_json", fake)
    updater._run("manual", None, None)

    after = _stored_ids()
    # Everything the two failed sources had is still there...
    assert "comfy:models_upscale_models_4x_demo.pth" in after
    assert "ollama:demo:70b" in after
    assert "comfy:models_vae_fresh_vae.safetensors" not in after
    assert "ollama:fresh:9b" not in after
    # ...and the sources that answered were replaced by what they said.
    assert "civitai:4242" not in after
    assert {"civitai:777", "omdb:4x-Fresh", "hf:bartowski/Fresh-9B-GGUF"} <= after
    assert before - after == {
        "civitai:4242",                 # Civitai answered without it
        "hf:demo-labs/Demo-Video",      # the Hub answered without these three
        "hf:demo-labs/Demo-Giant",
        "hf:bartowski/Demo-7B-GGUF"}

    state = catalog.load_state()
    assert state["sources"]["comfy_manager"]["ok"] is False
    assert state["sources"]["comfy_manager"]["entries"] == 0
    assert "comfy_manager is down" in state["sources"]["comfy_manager"]["error"]
    assert state["sources"]["ollama"]["ok"] is False
    assert state["sources"]["civitai"]["ok"] is True
    assert state["sources"]["civitai"]["error"] is None
    assert sorted(state["last_run"]["result"]["failed_sources"]) == ["comfy_manager",
                                                                    "ollama"]
    assert state["last_run"]["error"] is None, "a failed source is not a failed run"
    status = updater.status()
    assert status["running"] is False and status["error"] is None


def test_a_source_that_crashes_is_isolated_like_one_that_is_down(advisor, monkeypatch):
    fake = FakeUpstream()
    original = fake.__call__

    def crash_on_civitai(url, **kwargs):
        if "civitai.com" in url:
            raise RuntimeError("unexpected shape")
        return original(url, **kwargs)

    monkeypatch.setattr(sources, "fetch_json", crash_on_civitai)
    updater._run("manual", None, None)
    state = catalog.load_state()
    assert state["sources"]["civitai"]["ok"] is False
    assert state["sources"]["civitai"]["error"] == "RuntimeError: unexpected shape"
    assert state["last_run"]["error"] is None
    after = _stored_ids()
    assert "civitai:4242" in after, "what Civitai had is kept"
    assert "ollama:fresh:9b" in after, "the sources after it still ran"


def test_a_refresh_in_which_every_other_source_fails_does_not_shrink_the_catalog(
        advisor, monkeypatch):
    before = _stored_ids()
    failing = ("vault", "civitai", "comfy_manager", "openmodeldb", "ollama", "github")
    monkeypatch.setattr(sources, "fetch_json", FakeUpstream(failing=failing))
    updater._run("manual", None, ["vault", "civitai", "comfy_manager", "openmodeldb",
                                  "ollama", "github"])
    assert _stored_ids() == before
    state = catalog.load_state()
    assert sorted(state["last_run"]["result"]["failed_sources"]) == sorted(failing)
    assert state["last_run"]["result"]["entries"] == len(before)
    assert catalog.load()["leads"][0]["name"] == "demo-labs/demo-video", "old leads kept"


def test_an_unreachable_hub_does_not_shrink_the_catalog(advisor, monkeypatch):
    before = _stored_ids()
    monkeypatch.setattr(sources, "fetch_json", FakeUpstream(failing=("huggingface",)))
    updater._run("manual", None, ["huggingface"])
    after = _stored_ids()
    assert {i for i in before if i.startswith("hf:")} <= after, before - after


def test_a_refresh_limited_to_some_sources_leaves_the_others_untouched(advisor, upstream):
    before = _stored_ids()
    updater._run("manual", None, ["ollama"])
    after = _stored_ids()
    assert after == (before - {"ollama:demo:70b"}) | {"ollama:fresh:9b"}
    assert upstream.urls == [sources.OLLAMA_TAGS]
    assert set(catalog.load_state()["sources"]) == {"ollama"}


def test_a_refresh_limited_to_some_kinds_keeps_every_other_kind(advisor, upstream):
    before = _stored_ids()
    updater._run("manual", ["llm"], None)
    after = _stored_ids()
    not_llm = {e["id"] for e in tiny_entries() if e["kind"] != "llm"}
    assert len(not_llm) == 5
    assert not_llm <= after, "image, video, lora and upscale rows were not in scope"
    assert {"hf:bartowski/Fresh-9B-GGUF", "ollama:fresh:9b"} <= after
    assert "civitai:777" not in after and "omdb:4x-Fresh" not in after
    assert "hf:bartowski/Demo-7B-GGUF" not in after, "the Hub answered for llm"
    assert before - after == {"hf:bartowski/Demo-7B-GGUF", "ollama:demo:70b"}
    assert catalog.load_state()["last_run"]["kinds"] == ["llm"]


def test_an_unchanged_hub_repository_is_not_fetched_again(advisor, monkeypatch):
    first = FakeUpstream()
    monkeypatch.setattr(sources, "fetch_json", first)
    updater._run("manual", None, ["huggingface"])
    detail_url = sources.hf_detail_url("bartowski/Fresh-9B-GGUF")
    assert detail_url in first.urls

    second = FakeUpstream()   # the listing still reports sha "rev-1"
    monkeypatch.setattr(sources, "fetch_json", second)
    updater._run("manual", None, ["huggingface"])
    assert detail_url not in second.urls, "same revision upstream - reused"
    assert "hf:bartowski/Fresh-9B-GGUF" in _stored_ids()


def test_the_first_run_does_not_flag_everything_as_new_but_the_second_flags_additions(
        advisor, monkeypatch):
    monkeypatch.setattr(sources, "fetch_json", FakeUpstream())
    updater._run("manual", None, None)
    state = catalog.load_state()
    assert state["last_run"]["result"]["new"] == 0
    assert state["first_seen"]
    assert set(state["first_seen"].values()) == {0}, "all already known"
    assert set(state["first_seen"]) == _stored_ids()
    service._invalidate()
    assert service.list_entries(new_only=True)["items"] == []
    assert service.overview()["new"] == 0

    # A second run that finds one more repository flags exactly that one.
    details = dict(HUB_DETAILS)
    details["demo-labs/Brand-New"] = {
        "id": "demo-labs/Brand-New", "pipeline_tag": "text-generation", "sha": "r1",
        "tags": ["gguf"], "siblings": [_sibling("Brand-New-Q4_K_M.gguf", 3 * GIB)]}
    listing = [*HUB_LISTING, {"id": "demo-labs/Brand-New", "sha": "r1"}]
    monkeypatch.setattr(sources, "fetch_json",
                        FakeUpstream(hub_details=details, hub_listing=listing))
    updater._run("manual", None, None)
    state = catalog.load_state()
    assert state["last_run"]["result"]["new"] == 1
    fresh = {k for k, v in state["first_seen"].items() if v}
    assert fresh == {"hf:demo-labs/Brand-New"}
    service._invalidate()
    assert [r["id"] for r in service.list_entries(new_only=True)["items"]] == [
        "hf:demo-labs/Brand-New"]
    detail = service.entry_detail("hf:demo-labs/Brand-New")
    assert detail["is_new"] is True
    assert detail["first_seen_at"] == state["first_seen"]["hf:demo-labs/Brand-New"]
    assert service.entry_detail("hf:demo-labs/Demo-Image")["is_new"] is False


def test_a_cancelled_refresh_keeps_the_previous_catalog(advisor, upstream):
    before = (advisor / "catalog.json").read_bytes()

    def cancel_on_first_listing(url: str) -> None:
        if url.startswith(sources.HF_BASE + "/api/models?"):
            updater.cancel()

    upstream.on_request = cancel_on_first_listing
    updater._run("manual", None, None)
    assert (advisor / "catalog.json").read_bytes() == before
    state = catalog.load_state()
    assert state["last_run"]["error"] == "Cancelled."
    assert state["last_run"]["result"] is None
    assert updater.status()["error"] == "Cancelled."
    assert updater.status()["running"] is False
    assert not (advisor / "catalog.json.tmp").exists()


def test_a_newer_source_list_from_the_repository_replaces_the_built_in_one(
        advisor, monkeypatch):
    newer = dict(TINY_SOURCES, revision=7,
                 pins={"huggingface": [{"repo": "demo-labs/Fresh-9B", "kind": "llm"}]})
    fake = FakeUpstream()
    original = fake.__call__

    def with_source_list(url, **kwargs):
        if url == sources.VAULT_SOURCES:
            fake.urls.append(url)
            return 200, newer
        return original(url, **kwargs)

    monkeypatch.setattr(sources, "fetch_json", with_source_list)
    updater._run("manual", None, ["vault"])
    defs = catalog.sources()
    assert defs["revision"] == 7
    assert defs["origin"] == "repository"
    assert [p["repo"] for p in defs["pins"]["huggingface"]] == ["demo-labs/Fresh-9B"]

    # An older or malformed one is ignored.
    for doc in (dict(TINY_SOURCES, revision=3), {"revision": "9", "sources": []},
                ["not", "a", "dict"]):
        monkeypatch.setattr(sources, "fetch_json",
                            lambda url, doc=doc, **kwargs: (200, doc))
        updater._run("manual", None, ["vault"])
        assert catalog.sources()["revision"] == 7


def test_a_source_list_cannot_add_a_host(advisor, monkeypatch):
    """Data only: a row naming a new endpoint changes nothing that is requested."""
    hostile = dict(TINY_SOURCES, revision=9)
    hostile["sources"] = [*TINY_SOURCES["sources"],
                          {"id": "evil", "name": "Evil", "url": "https://evil.test/api",
                           "provides": "x", "kinds": ["image"], "trust": "high"}]
    hostile["discovery"] = dict(TINY_SOURCES["discovery"],
                                evil=[{"url": "https://evil.test/list"}],
                                github=[{"q": "x&redirect=https://evil.test"}])
    hostile["pins"] = {"huggingface": [{"repo": "https://evil.test/a/b"},
                                       {"repo": "../../x"},
                                       {"repo": "demo-labs/Demo-Image"}]}
    fake = FakeUpstream()
    original = fake.__call__

    def with_source_list(url, **kwargs):
        if url == sources.VAULT_SOURCES:
            return 200, hostile
        return original(url, **kwargs)   # asserts the allowlist on every URL

    monkeypatch.setattr(sources, "fetch_json", with_source_list)
    updater._run("manual", None, None)
    assert catalog.sources()["revision"] == 9
    assert fake.urls, "the run went ahead"
    assert not [u for u in fake.urls if "evil" in u]
    assert not [u for u in fake.urls if "api.github.com" in u], \
        "the malformed search query was skipped, not sent"
    assert "hf:demo-labs/Demo-Image" in _stored_ids()
    assert catalog.load_state()["last_run"]["error"] is None


def test_a_pin_added_while_a_refresh_is_running_survives_it(advisor, upstream):
    added: list[str] = []

    def add_pin_mid_run(url: str) -> None:
        if url == sources.OLLAMA_TAGS and not added:
            updater.add_pin("owner/added-mid-run")
            added.append("owner/added-mid-run")

    upstream.on_request = add_pin_mid_run
    updater._run("manual", None, None)
    assert added
    assert [p["repo"] for p in catalog.load_state()["user_pins"]] == added


# ---------------------------------------------------------------------------
# Starting a refresh, and the self-initiated one
# ---------------------------------------------------------------------------

def test_start_runs_in_one_background_thread_and_refuses_a_second(advisor, upstream):
    go_online()
    gate = threading.Event()
    entered = threading.Event()

    def hold(url: str) -> None:
        entered.set()
        assert gate.wait(timeout=20), "the test never released the refresh"

    upstream.on_request = hold
    status = updater.start(trigger="manual", only=["ollama", "not-a-source"])
    try:
        assert status["running"] is True
        assert status["trigger"] == "manual"
        assert entered.wait(timeout=20)
        assert updater.is_running() is True
        assert updater._thread.daemon is True
        assert updater._thread.name == "advisor-update"
        with pytest.raises(ConflictError):
            updater.start()
        assert updater.maybe_auto_update() in ("running", "fresh")
    finally:
        gate.set()
        updater._thread.join(timeout=20)
    assert not updater._thread.is_alive()
    done = updater.status()
    assert done["running"] is False
    assert done["error"] is None
    assert done["result"]["failed_sources"] == []
    assert upstream.urls == [sources.OLLAMA_TAGS], "the unknown source id was dropped"
    assert "ollama:fresh:9b" in _stored_ids()


def test_the_update_route_answers_202_and_a_second_request_is_a_conflict(
        client, upstream):
    go_online()
    gate = threading.Event()
    entered = threading.Event()

    def hold(url: str) -> None:
        entered.set()
        assert gate.wait(timeout=20)

    upstream.on_request = hold
    try:
        response = client.post(API + "/update", json={"sources": ["ollama"]})
        assert response.status_code == 202, response.text
        assert response.json()["running"] is True
        assert response.json()["online_enabled"] is True
        assert entered.wait(timeout=20)
        assert client.get(API + "/update/status").json()["running"] is True
        assert_envelope(client.post(API + "/update", json={}), 409, "CONFLICT")
    finally:
        gate.set()
        updater._thread.join(timeout=20)
    status = client.get(API + "/update/status").json()
    assert status["running"] is False
    assert status["last_run"]["trigger"] == "manual"
    assert status["last_run"]["result"]["entries"] >= 1
    assert "ollama:fresh:9b" in _ids(client.get(API + "/catalog?kind=llm"))


@pytest.mark.parametrize("body", [{"kinds": ["hologram"]}, {"kinds": "llm"},
                                  {"sources": "ollama"}, {"sources": ["a"] * 9},
                                  {"url": "https://evil.test/catalog.json"},
                                  {"only": ["ollama"]}])
def test_a_malformed_update_body_is_refused_before_anything_starts(client, upstream,
                                                                   body):
    go_online()
    assert_envelope(client.post(API + "/update", json=body), 422, "VALIDATION_ERROR")
    assert upstream.urls == []
    assert updater.is_running() is False


def test_start_while_offline_raises_and_leaves_no_trace(advisor, upstream):
    with pytest.raises(FeatureUnavailable):
        updater.start()
    assert updater.is_running() is False
    assert upstream.urls == []
    assert not (advisor / "state.json").exists()


def test_maybe_auto_update_is_offline_when_outbound_lookups_are_off(advisor, upstream,
                                                                    monkeypatch):
    def no_start(**kwargs):
        raise AssertionError("an automatic refresh was started while offline")

    monkeypatch.setattr(updater, "start", no_start)
    assert config_service.get_config().online_enabled is False
    assert updater.maybe_auto_update() == "offline"
    # Offline wins over everything else, however stale the data is.
    stale = json.loads((advisor / "catalog.json").read_text(encoding="utf-8"))
    stale["generated_at"] = 1
    (advisor / "catalog.json").write_text(json.dumps(stale), encoding="utf-8")
    catalog.invalidate()
    assert updater.status()["stale"] is True
    assert updater.maybe_auto_update() == "offline"
    assert upstream.urls == []


def test_maybe_auto_update_is_fresh_when_the_catalog_is_new(advisor, upstream,
                                                            monkeypatch):
    go_online()
    monkeypatch.setattr(updater, "start", lambda **kwargs: pytest.fail("started"))
    assert updater.status()["stale"] is False
    assert updater.maybe_auto_update() == "fresh"
    assert upstream.urls == []


def _make_stale(advisor) -> None:
    doc = json.loads((advisor / "catalog.json").read_text(encoding="utf-8"))
    doc["generated_at"] = dbmod.now_ms() - updater.STALE_AFTER_MS - 60_000
    (advisor / "catalog.json").write_text(json.dumps(doc), encoding="utf-8")
    catalog.invalidate()


def test_maybe_auto_update_respects_the_switch_and_the_retry_gap(advisor, upstream,
                                                                 monkeypatch):
    go_online()
    _make_stale(advisor)
    started: list[dict] = []
    monkeypatch.setattr(updater, "start", lambda **kwargs: started.append(kwargs))

    config_service.set_config({"advisor_auto_update": False})
    assert updater.maybe_auto_update() == "disabled"
    config_service.set_config({"advisor_auto_update": True})

    state = catalog.load_state()
    state["last_run"] = {"finished_at": dbmod.now_ms() - 60_000}
    catalog.save_state(state)
    assert updater.maybe_auto_update() == "recently_tried"
    assert started == []

    state["last_run"] = {"finished_at": dbmod.now_ms() - updater.AUTO_MIN_GAP_MS - 60_000}
    catalog.save_state(state)
    config_service.set_config({"advisor_kinds": ["llm", "image"]})
    assert updater.maybe_auto_update() == "started"
    assert started == [{"trigger": "auto", "kinds": ["llm", "image"]}]

    def refuse(**kwargs):
        raise ConflictError("A catalog refresh is already running.")

    monkeypatch.setattr(updater, "start", refuse)
    assert updater.maybe_auto_update() == "skipped"
    assert upstream.urls == []


def test_an_auto_update_request_while_offline_is_an_answer_not_an_error(
        client, monkeypatch):
    """``{"auto": true}`` is what the interface sends when the tab opens."""
    _NoThread.started = 0
    monkeypatch.setattr(updater.threading, "Thread", _NoThread)
    response = client.post(API + "/update", json={"auto": True})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["auto_outcome"] == "offline"
    assert body["running"] is False
    assert body["online_enabled"] is False
    assert set(body) >= UPDATE_KEYS
    assert _NoThread.started == 0
    assert client.get(API + "/update/status").json()["last_run"] is None


def test_an_auto_update_request_on_fresh_data_starts_nothing(client, upstream,
                                                             monkeypatch):
    go_online()
    _NoThread.started = 0
    monkeypatch.setattr(updater.threading, "Thread", _NoThread)
    body = client.post(API + "/update", json={"auto": True}).json()
    assert body["auto_outcome"] == "fresh"
    assert body["running"] is False
    assert body["stale"] is False
    assert upstream.urls == []
    assert _NoThread.started == 0
    config_service.set_config({"advisor_auto_update": False})
    assert client.post(API + "/update",
                       json={"auto": True}).json()["auto_outcome"] == "disabled"


def test_an_auto_update_request_still_needs_the_csrf_header_and_a_boolean(advisor):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as naked:
        assert_envelope(naked.post(API + "/update", json={"auto": True}), 400,
                        "CSRF_HEADER_MISSING")
        assert_envelope(naked.post(API + "/update", json={"auto": "perhaps"},
                                   headers=CSRF), 422, "VALIDATION_ERROR")


def test_the_staleness_thresholds_are_a_day_and_six_hours():
    assert updater.STALE_AFTER_MS == 24 * 60 * 60 * 1000
    assert updater.AUTO_MIN_GAP_MS == 6 * 60 * 60 * 1000


def test_startup_schedules_a_refresh_only_when_outbound_lookups_are_on(advisor):
    """``core.startup`` must not even arm the timer while offline."""
    import inspect

    from app import core

    source = inspect.getsource(core)
    guard = "if cfg.online_enabled and cfg.raw.get(\"advisor_auto_update\", True):"
    assert guard in source
    assert source.index(guard) < source.index("_refresh_model_catalog)")
    assert core.ADVISOR_UPDATE_DELAY_S >= 10, "never inline with startup"
    assert core.ADVISOR_RECHECK_S == 6 * 60 * 60, "and re-armed every six hours"


# ---------------------------------------------------------------------------
# Isolation: the catalog files
# ---------------------------------------------------------------------------

def test_a_corrupt_refreshed_catalog_falls_back_instead_of_failing(advisor):
    (advisor / "catalog.json").write_text("{ this is not json", encoding="utf-8")
    catalog.invalidate()
    loaded = catalog.load()
    assert loaded["origin"] == "seed"
    assert loaded["entries"] == [], "the (absent) tmp seed, not an exception"
    (advisor / "state.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert catalog.load_state()["user_pins"] == []
    service._invalidate()
    assert service.list_entries()["items"] == []
    assert service.overview()["catalog_total"] == 0


def test_invalid_rows_in_a_catalog_file_are_skipped_not_served(advisor):
    doc = {"schema": 1, "generated_at": dbmod.now_ms(), "entries": [
        tiny_entries()[0], {"id": "hf:a/b"}, "a string", None,
        {"id": "x", "name": "No variants", "kind": "image", "variants": []},
        {"id": "y", "name": "Bad kind", "kind": "hologram", "variants": [{"id": "v"}]},
        {"id": 5, "name": "Numeric id", "kind": "image", "variants": [{"id": "v"}]}]}
    (advisor / "catalog.json").write_text(json.dumps(doc), encoding="utf-8")
    catalog.invalidate()
    assert [e["id"] for e in catalog.load()["entries"]] == ["hf:demo-labs/Demo-Image"]


def test_writes_are_atomic_and_compact(advisor):
    assert not (advisor / "catalog.json.tmp").exists()
    text = (advisor / "catalog.json").read_text(encoding="utf-8")
    assert "\n" not in text and '": ' not in text, "compact separators"
    doc = json.loads(text)
    lora = next(e for e in doc["entries"] if e["id"] == "civitai:4242")
    assert "companions" not in lora and "nsfw" not in lora, \
        "empty and false fields are not written"


def test_an_adult_flagged_entry_is_neither_stored_nor_served(advisor):
    doc = json.loads((advisor / "catalog.json").read_text(encoding="utf-8"))
    assert "hf:someone/Adult-Thing" not in [e["id"] for e in doc["entries"]]
    assert catalog.get("hf:someone/Adult-Thing") is None
    # Even a catalog file that somehow carries one does not serve it.
    doc["entries"].append(adult_entry())
    doc["generated_at"] += 1
    (advisor / "catalog.json").write_text(json.dumps(doc), encoding="utf-8")
    catalog.invalidate()
    service._invalidate()
    assert catalog.get("hf:someone/Adult-Thing") is None
    assert len(catalog.load()["entries"]) == LISTED
    assert "hf:someone/Adult-Thing" not in [
        r["id"] for r in service.list_entries()["items"]]


def test_the_pin_list_is_capped(advisor):
    state = catalog.load_state()
    state["user_pins"] = [{"repo": f"owner/model-{i}"} for i in range(catalog.MAX_USER_PINS)]
    catalog.save_state(state)
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError):
        updater.add_pin("owner/one-too-many")
    assert len(catalog.load_state()["user_pins"]) == catalog.MAX_USER_PINS
    # Re-pinning one that is already there is still allowed.
    assert updater.add_pin("owner/model-0", "llm")["kind"] == "llm"


# ---------------------------------------------------------------------------
# Links are scrubbed when a catalog is read
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,<script>x</script>",
    "http://huggingface.co/a/b", "ftp://example.test/x", "file:///etc/passwd",
    "//example.test/x", "example.test/x", "https://user@example.test/x",
    "https://user:pw@example.test/x", "https://example.test/a b",
    "https://example.test/a\nb", "https://exa\tmple.test/",
    "", None, 5, ["https://example.test/x"],
    "https://", "https:///path",
])
def test_only_a_plain_https_link_survives(value):
    assert catalog._https(value) is None


@pytest.mark.parametrize("value", [
    "https://huggingface.co/demo-labs/Demo-Image",
    "https://civitai.com/api/download/models/9001",
    "https://example.test/path/file.safetensors?download=true",
])
def test_an_ordinary_https_link_is_kept_as_it_is(value):
    assert catalog._https(value) == value
    assert catalog._https(f"  {value}\n") == value, "surrounding whitespace is trimmed"


def test_load_blanks_a_link_that_is_not_plain_https_and_keeps_the_entry(advisor):
    doc = json.loads((advisor / "catalog.json").read_text(encoding="utf-8"))
    image = next(e for e in doc["entries"] if e["id"] == "hf:demo-labs/Demo-Image")
    good = image["variants"][1]["download_url"]
    image["variants"][0]["download_url"] = "javascript:alert(document.cookie)"
    image["url"] = "http://huggingface.co/demo-labs/x"
    image["companions"][0]["download_url"] = "https://user:pw@example.test/t5"
    doc["leads"] = [{"name": "ok/lead", "url": "https://github.com/ok/lead"},
                    {"name": "bad/lead", "url": "javascript:alert(1)"},
                    {"name": "no/link"}]
    doc["generated_at"] += 1
    (advisor / "catalog.json").write_text(json.dumps(doc), encoding="utf-8")
    catalog.invalidate()
    service._invalidate()

    loaded = catalog.load()
    entry = loaded["by_id"]["hf:demo-labs/Demo-Image"]
    assert entry["variants"][0]["download_url"] is None
    assert entry["variants"][1]["download_url"] == good
    assert entry["url"] is None
    assert entry["companions"][0]["download_url"] is None
    assert [lead["name"] for lead in loaded["leads"]] == ["ok/lead"]
    assert len(loaded["entries"]) == LISTED, "the entry itself is still served"

    detail = service.entry_detail("hf:demo-labs/Demo-Image")
    assert detail["variants"][0]["download_url"] is None
    assert "javascript:" not in json.dumps(detail)
    assert "javascript:" not in json.dumps(service.list_entries())
    assert "javascript:" not in json.dumps(service.sources_report())


def test_load_makes_repeated_variant_ids_distinct(advisor):
    doc = json.loads((advisor / "catalog.json").read_text(encoding="utf-8"))
    upscaler = next(e for e in doc["entries"] if e["kind"] == "upscale")
    upscaler["variants"] = [dict(upscaler["variants"][0]), dict(upscaler["variants"][0])]
    doc["generated_at"] += 1
    (advisor / "catalog.json").write_text(json.dumps(doc), encoding="utf-8")
    catalog.invalidate()
    ids = [v["id"] for v in catalog.get(upscaler["id"])["variants"]]
    assert len(ids) == 2 and len(set(ids)) == 2, ids
