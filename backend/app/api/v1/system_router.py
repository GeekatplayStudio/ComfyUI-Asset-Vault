"""``/api/v1/system`` - API_CONTRACT 1."""

from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path

from fastapi import APIRouter

from ... import config as buildcfg
from ...core import config_service, vault_stats
from ...core import db as dbmod
from ...core.pathsafe import normalize
from ...indexing import walker
from ...indexing.service import get_indexer
from ...jobs.embed_service import get_embed_service
from ...jobs.thumb_service import get_thumb_service
from ...services.ollama_service import ollama_service
from ...services.queries import models_query, nodes_query
from ..middleware import ApiError
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.system import (
    ConfigPatch,
    HealthReport,
    OllamaTestRequest,
    OllamaTestResponse,
    RootCreate,
    RootDeleted,
    RootItem,
    RootsList,
    SystemConfig,
    SystemInfo,
    ThumbsGcRequest,
    ThumbsGcResponse,
    ValidatePathRequest,
    ValidatePathResponse,
    VaultStats,
    WizardCompleteRequest,
    WizardCompleteResponse,
)

router = APIRouter(prefix="/system", tags=["System"])

PREVIEW_ENTRY_CAP = 60_000
PREVIEW_TIME_CAP_S = 4.0


# ---------------------------------------------------------------------------
# Shaping helpers (presentation only - no SQL, no business rules)
# ---------------------------------------------------------------------------

def _root_items(cfg) -> list[dict]:
    return [{
        "id": root.id, "kind": root.kind, "path": root.path, "label": root.label,
        "available": os.path.isdir(root.path), "is_default": bool(root.is_default),
        "source": root.source, "category": root.category,
    } for root in cfg.roots]


def _config_payload(cfg, *, roots_changed: bool | None = None) -> dict:
    payload = {
        "comfyui_path": str(cfg.comfyui_path) if cfg.comfyui_path else None,
        "path_exists": bool(cfg.comfyui_path and Path(cfg.comfyui_path).is_dir()),
        "is_configured": cfg.is_configured,
        "auto_reindex": cfg.auto_reindex,
        "watch_enabled": cfg.watch_enabled,
        "online_enabled": cfg.online_enabled,
        "civitai_enabled": cfg.civitai_enabled,
        "civitai_api_key_set": bool(cfg.civitai_api_key),
        "ollama_enabled": cfg.ollama_enabled,
        "ollama_url": cfg.ollama_url,
        "ollama_model": cfg.ollama_model,
        "smart_search_enabled": cfg.smart_search_enabled,
        "hash_concurrency": cfg.hash_concurrency,
        "hash_throttle_mbps": cfg.hash_throttle_mbps,
        "thumb_cache_max_mb": cfg.thumb_cache_max_mb,
        "thumb_video_ffmpeg": cfg.thumb_video_ffmpeg,
        "page_size_default": cfg.page_size_default,
        "trash_mode": cfg.trash_mode,
        "trash_retention_days": cfg.trash_retention_days,
        "read_held_extra_paths": cfg.read_held_extra_paths,
        "mcp_read_only": cfg.mcp_read_only,
        "extra_workflow_dirs": [str(d) for d in cfg.extra_workflow_dirs],
        "roots": _root_items(cfg),
    }
    if roots_changed is not None:
        payload["roots_changed"] = roots_changed
    return payload


@router.get("/info", response_model=SystemInfo, responses=BASE_ERRORS,
            summary="Build identity and feature flags (never fails)")
def system_info() -> dict:
    cfg = config_service.get_config()
    embed = get_embed_service()
    return {
        "app": buildcfg.APP_NAME,
        "version": buildcfg.VERSION,
        "author": buildcfg.AUTHOR,
        "api_version": 1,
        "schema_version": buildcfg.SCHEMA_VERSION,
        "python": platform.python_version(),
        "platform": sys.platform,
        "features": {
            "smart_search": bool(embed.available and cfg.smart_search_enabled),
            "civitai": bool(cfg.online_enabled and cfg.civitai_enabled),
            "ollama": bool(cfg.ollama_enabled),
            "mcp": True,
            "video_thumbnails": bool(cfg.thumb_video_ffmpeg),
        },
    }


@router.get("/config", response_model=SystemConfig, responses=BASE_ERRORS,
            summary="Current configuration (the API key itself is never returned)")
def get_config() -> dict:
    return _config_payload(config_service.get_config())


@router.patch("/config", response_model=SystemConfig,
              responses={**error_responses("PATH_INVALID", "PATH_NOT_ALLOWED"),
                         **MUTATION_ERRORS},
              summary="Update any subset of the writable keys")
def patch_config(body: ConfigPatch) -> dict:
    patch = body.model_dump(exclude_unset=True)
    before = config_service.get_config()
    if patch.get("comfyui_path"):
        candidate = normalize(patch["comfyui_path"])
        if not candidate.is_dir():
            raise ApiError("PATH_INVALID", "That directory does not exist.",
                           details={"path": str(candidate)},
                           field_errors=[{"field": "comfyui_path",
                                          "message": "directory does not exist"}])
        patch["comfyui_path"] = str(candidate)
    if patch.get("extra_workflow_dirs") is not None:
        patch["extra_workflow_dirs"] = [str(normalize(d))
                                        for d in patch["extra_workflow_dirs"]]
    cfg = config_service.set_config(patch)
    changed = [r.path for r in cfg.roots] != [r.path for r in before.roots]
    return _config_payload(cfg, roots_changed=changed)


def _bounded_preview(root: Path) -> dict:
    """Cheap enough for a live wizard field: hard caps at 60k entries / 4 s."""
    counts = {"model_files": 0, "model_bytes": 0, "custom_node_packages": 0,
              "workflows": 0, "outputs": 0, "inputs": 0, "truncated": False}
    deadline = time.monotonic() + PREVIEW_TIME_CAP_S
    seen = 0
    for name, key in (("models", "model_files"), ("output", "outputs"),
                      ("input", "inputs")):
        directory = root / name
        if not directory.is_dir():
            continue
        for entry in walker.walk(directory):
            seen += 1
            if key == "model_files":
                if entry.ext not in walker.MODEL_EXTS or entry.size < walker.MIN_MODEL_BYTES:
                    continue
                counts["model_bytes"] += entry.size
            counts[key] += 1
            if seen >= PREVIEW_ENTRY_CAP or time.monotonic() > deadline:
                counts["truncated"] = True
                break
        if counts["truncated"]:
            break
    custom = root / "custom_nodes"
    if custom.is_dir():
        counts["custom_node_packages"] = len(walker.top_level_dirs(custom))
    for rel in ("workflows", os.path.join("user", "default", "workflows")):
        directory = root / rel
        if directory.is_dir():
            counts["workflows"] += sum(1 for _ in walker.walk_json(directory))
    return counts


@router.post("/validate-path", response_model=ValidatePathResponse,
             responses=MUTATION_ERRORS,
             summary="Wizard live preview - valid:false is still a 200")
def validate_path(body: ValidatePathRequest) -> dict:
    report = config_service.validate_comfyui_path(body.path)
    root = Path(report["path"])
    found = report.get("found") or {}
    exists = root.is_dir()
    warnings: list[str] = list(report.get("issues") or [])

    yaml_present = (root / "extra_model_paths.yaml").is_file()
    held_present = (root / "extra_model_paths.yaml.hold").is_file()
    if not yaml_present and held_present:
        warnings.append("extra_model_paths.yaml not found "
                        "(extra_model_paths.yaml.hold exists but is not loaded)")

    version = None
    version_file = root / "comfyui_version.py"
    if version_file.is_file():
        try:
            text = version_file.read_text(encoding="utf-8", errors="replace")
            version = text.split("=", 1)[1].strip().strip("'\"") if "=" in text else None
        except OSError:
            version = None

    preview = _bounded_preview(root) if exists else {
        "model_files": 0, "model_bytes": 0, "custom_node_packages": 0,
        "workflows": 0, "outputs": 0, "inputs": 0, "truncated": False}

    return {
        "valid": bool(report.get("valid")),
        "normalized": report["path"],
        "exists": exists,
        "is_comfyui_root": bool(report.get("valid")),
        "signals": {
            "has_models": bool(found.get("models")),
            "has_custom_nodes": bool(found.get("custom_nodes")),
            "has_output": bool(found.get("output")),
            "has_input": (root / "input").is_dir(),
            "has_user_workflows": (root / "user" / "default" / "workflows").is_dir(),
            "has_root_workflows": (root / "workflows").is_dir(),
            "has_main_py": bool(found.get("main.py")),
            "comfyui_version": version,
        },
        "extra_model_paths": {"present": yaml_present, "held_present": held_present,
                              "roots": []},
        "preview": preview,
        "warnings": warnings,
        "reason": (warnings[0] if warnings and not report.get("valid") else None),
    }


@router.post("/wizard/complete", status_code=202,
             response_model=WizardCompleteResponse,
             responses={**error_responses("PATH_INVALID"), **MUTATION_ERRORS},
             summary="Persist the wizard answers and (optionally) start the first scan")
def wizard_complete(body: WizardCompleteRequest) -> dict:
    candidate = normalize(body.comfyui_path)
    if not candidate.is_dir():
        raise ApiError("PATH_INVALID", "That directory does not exist.",
                       details={"path": str(candidate)},
                       field_errors=[{"field": "comfyui_path",
                                      "message": "directory does not exist"}])
    config_service.set_config({
        "comfyui_path": str(candidate),
        "is_configured": True,
        "online_enabled": body.online_enabled,
        "civitai_enabled": body.online_enabled,
        "auto_reindex": body.auto_reindex,
        "smart_search_enabled": body.smart_search_enabled,
        "ollama_enabled": body.ollama_enabled,
        "ollama_url": body.ollama_url,
        "ollama_model": body.ollama_model,
    })
    job_id = None
    if body.start_scan:
        # Never awaited inside the request: the client subscribes to /index/stream.
        try:
            job_id = get_indexer().start(mode="full", force=False,
                                         enrich_online=body.online_enabled,
                                         trigger="wizard")
        except Exception as exc:  # noqa: BLE001 - a busy indexer is not a wizard failure
            job_id = (getattr(exc, "details", {}) or {}).get("job_id")
    return {"is_configured": True, "job_id": job_id, "scan_started": job_id is not None}


@router.get("/stats", response_model=VaultStats, responses=BASE_ERRORS,
            summary="Status-bar and dashboard counters")
def system_stats() -> dict:
    stats = vault_stats()
    by_category = [{"category": g["key"] or None, "count": g["count"],
                    "bytes": g.get("bytes") or 0}
                   for g in models_query.model_groups({}, "category")]
    by_base = [{"base_model": g["key"] or None, "count": g["count"]}
               for g in models_query.model_groups({}, "base_model")]
    official = nodes_query.list_node_classes({"official": True}, limit=1)
    cfg = config_service.get_config()
    inputs = 0
    if cfg.comfyui_path and (Path(cfg.comfyui_path) / "input").is_dir():
        inputs = sum(1 for _ in walker.walk(Path(cfg.comfyui_path) / "input"))
    last = stats.get("last_scan") or {}
    return {
        "models": int(stats.get("models") or 0),
        "model_files": int(stats.get("model_files") or 0),
        "models_bytes": int(stats.get("models_bytes") or 0),
        "models_hashed": int(stats.get("models_hashed") or 0),
        "node_packages": int(stats.get("node_packages") or 0),
        "node_classes": int(stats.get("node_classes") or 0),
        "official_node_classes": int(official.page.get("total") or 0),
        "workflows": int(stats.get("workflows") or 0),
        "workflows_broken": int(stats.get("workflows_broken") or 0),
        "outputs": int(stats.get("outputs") or 0),
        "outputs_bytes": int(stats.get("outputs_bytes") or 0),
        "inputs": inputs,
        "embedded": int(stats.get("embedded") or 0),
        "integrity_issues": int(stats.get("integrity_issues") or 0),
        "by_category": by_category,
        "by_base_model": by_base,
        "last_scan": {
            "job_id": last.get("id"),
            "finished_at": last.get("finished_at"),
            "duration_ms": last.get("duration_ms"),
            "errors": last.get("error_count"),
        } if last else None,
    }


def _integrity_check() -> dict:
    facets = models_query.model_facets({})
    bad = [row for row in facets.get("integrity", [])
           if row.get("value") not in (None, "ok")]
    if not bad:
        return {"id": "integrity", "status": "ok", "count": 0}
    values = [str(row["value"]) for row in bad]
    total = sum(int(row["count"]) for row in bad)
    listed = models_query.list_models({"integrity": values}, limit=20)
    return {
        "id": "integrity", "status": "error", "count": total,
        "message": f"{total} model file(s) failed the integrity check",
        "items": [{"uid": item["uid"], "name": item.get("filename"),
                   "reason": item.get("integrity")} for item in listed.items],
    }


def _partial_downloads_check(cfg) -> dict:
    items: list[dict] = []
    for _category, directory, _root in config_service.model_dirs(cfg):
        for entry in walker.walk_partials(directory):
            if entry.size > 0:
                items.append({"path": entry.path})
            if len(items) >= 25:
                break
        if len(items) >= 25:
            break
    return {"id": "partial_downloads", "status": "warn" if items else "ok",
            "count": len(items), "items": items}


def _suspect_remotes_check() -> dict:
    listed = nodes_query.list_node_packages({}, limit=500)
    items = [{"package": item.get("folder_name"),
              "repo_url": (item.get("repo") or {}).get("url")}
             for item in listed.items if (item.get("repo") or {}).get("suspect")]
    return {"id": "suspect_remotes", "status": "warn" if items else "ok",
            "count": len(items), "items": items}


@router.get("/health", response_model=HealthReport, responses=BASE_ERRORS,
            summary="The Health drawer")
def system_health() -> dict:
    cfg = config_service.get_config()
    checks: list[dict] = []

    root_ok = bool(cfg.comfyui_path and Path(cfg.comfyui_path).is_dir())
    checks.append({"id": "comfyui_root",
                   "status": "ok" if root_ok else "error",
                   "message": str(cfg.comfyui_path or "not configured")})

    db = dbmod.db_stat()
    checks.append({"id": "database", "status": "ok" if db["exists"] else "error",
                   "message": f"WAL, {db['size_bytes'] / 1_048_576:.1f} MB"})

    embed = get_embed_service().status()
    embed_ok = embed.get("state") == "ready"
    checks.append({"id": "embeddings",
                   "status": "ok" if embed_ok else "warn",
                   "message": embed.get("reason") or "Ready",
                   "action": None if embed_ok else "POST /api/v1/embeddings/enable"})

    checks.append({"id": "civitai",
                   "status": "ok" if (cfg.online_enabled and cfg.civitai_enabled)
                   else "warn",
                   "message": None if (cfg.online_enabled and cfg.civitai_enabled)
                   else "Online enrichment is disabled"})

    checks.append({"id": "ollama",
                   "status": "ok" if cfg.ollama_enabled else "warn",
                   "message": None if cfg.ollama_enabled
                   else f"Disabled ({cfg.ollama_url})"})

    checks.append(_integrity_check())
    checks.append(_partial_downloads_check(cfg))
    checks.append(_suspect_remotes_check())

    thumbs = get_thumb_service().stats()
    checks.append({"id": "thumb_cache", "status": "ok",
                   "message": f"{thumbs['bytes'] / 1_048_576:.0f} MB / "
                              f"{cfg.thumb_cache_max_mb} MB",
                   "count": thumbs["count"]})

    status = "ok"
    if any(c["status"] == "error" for c in checks):
        status = "error"
    elif any(c["status"] == "warn" for c in checks):
        status = "degraded"
    return {"status": status, "checks": checks}


@router.get("/roots", response_model=RootsList, responses=BASE_ERRORS,
            summary="List scan roots")
def list_roots() -> dict:
    return {"items": _root_items(config_service.get_config())}


@router.post("/roots", status_code=201, response_model=RootItem,
             responses={**error_responses("PATH_INVALID", "CONFLICT"),
                        **MUTATION_ERRORS},
             summary="Add an extra scan root")
def add_root(body: RootCreate) -> dict:
    path = normalize(body.path)
    if not path.is_dir():
        raise ApiError("PATH_INVALID", "That directory does not exist.",
                       details={"path": str(path)},
                       field_errors=[{"field": "path",
                                      "message": "directory does not exist"}])
    if body.kind == "extra_models":
        raise ApiError(
            "VALIDATION_ERROR",
            "Extra model roots come from extra_model_paths.yaml, not from this endpoint.",
            field_errors=[{"field": "kind",
                           "message": "only 'extra_workflows' can be added here"}])
    cfg = config_service.get_config()
    current = [str(d) for d in cfg.extra_workflow_dirs]
    if any(os.path.normcase(d) == os.path.normcase(str(path)) for d in current):
        raise ApiError("CONFLICT", "That root is already configured.",
                       details={"path": str(path)})
    cfg = config_service.set_config({"extra_workflow_dirs": [*current, str(path)]})
    for root in _root_items(cfg):
        if os.path.normcase(root["path"]) == os.path.normcase(str(path)):
            return root
    raise ApiError("INTERNAL", "The root was saved but could not be resolved.")


@router.delete("/roots/{root_id}", response_model=RootDeleted,
               responses={**error_responses("NOT_FOUND"), **MUTATION_ERRORS},
               summary="Remove an extra scan root")
def delete_root(root_id: int) -> dict:
    cfg = config_service.get_config()
    target = next((r for r in cfg.roots if r.id == root_id), None)
    if target is None:
        raise ApiError("NOT_FOUND", f"Root {root_id} does not exist.",
                       details={"root_id": root_id})
    if target.kind != "extra_workflows":
        raise ApiError("CONFLICT",
                       "Only extra workflow roots added by hand can be removed.",
                       details={"kind": target.kind})
    keep = [str(d) for d in cfg.extra_workflow_dirs
            if os.path.normcase(str(normalize(d))) != os.path.normcase(target.path)]
    config_service.set_config({"extra_workflow_dirs": keep})
    return {"deleted": True, "id": root_id}


@router.post("/thumbs/gc", response_model=ThumbsGcResponse, responses=MUTATION_ERRORS,
             summary="Trim the thumbnail cache to a byte budget")
def thumbs_gc(body: ThumbsGcRequest) -> dict:
    result = get_thumb_service().gc(body.max_mb)
    return {"deleted": int(result.get("removed") or 0),
            "freed_bytes": int(result.get("freed_bytes") or 0),
            "remaining_bytes": int(result.get("remaining_bytes") or 0)}


@router.post("/ollama/test", response_model=OllamaTestResponse,
             responses=MUTATION_ERRORS,
             summary="Probe an Ollama endpoint (unavailability is a 200, never a 5xx)")
async def ollama_test(body: OllamaTestRequest) -> dict:
    service = ollama_service if not body.url else type(ollama_service)(body.url)
    started = time.perf_counter()
    ok, message = await service.check_connection()
    latency = int((time.perf_counter() - started) * 1000)
    if not ok:
        return {"available": False, "models": [], "latency_ms": latency,
                "reason": message}
    return {"available": True, "models": await service.list_models(),
            "latency_ms": latency, "reason": None}
