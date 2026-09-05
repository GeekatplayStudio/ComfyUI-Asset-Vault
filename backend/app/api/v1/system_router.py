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
from ...services import app_update_service
from ...services.ollama_service import ollama_service
from ...services.queries import models_query, nodes_query
from ..middleware import ApiError
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.system import (
    AppUpdateDiscardResponse,
    AppUpdateDownloadResponse,
    AppUpdateStatus,
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
        "smart_search_min_score": cfg.smart_search_min_score,
        "app_update_check_enabled": cfg.app_update_check_enabled,
        "app_update_auto_download": cfg.app_update_auto_download,
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
    if report.get("resolved_from"):
        # A portable build was pointed at by its parent; say which install is
        # actually going to be used before anything is saved.
        warnings.insert(0, f"Found the ComfyUI install inside that folder: {report['path']}")
    if report.get("install_kind") == "data_folder":
        # ComfyUI Desktop layout: indexing works in full; the source-tree
        # features do not, and saying so here beats a surprise later.
        warnings.append(
            "This looks like a ComfyUI Desktop data folder. Models, outputs, "
            "workflows and custom nodes will all be indexed. Detecting the "
            "ComfyUI version, launching and updating it are unavailable for "
            "this layout, and built-in node classes are not listed.")

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
        "install_kind": report.get("install_kind"),
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

    # Three distinct states, because they need three different responses from
    # the user: nothing set yet, set but unreachable (drive off?), and fine.
    if not cfg.comfyui_path:
        checks.append({"id": "comfyui_root", "status": "error",
                       "message": "No ComfyUI folder is configured. "
                                  "Set it in Settings -> Location."})
    elif not Path(cfg.comfyui_path).is_dir():
        checks.append({"id": "comfyui_root", "status": "error",
                       "message": f"The configured folder is not reachable "
                                  f"(drive offline?): {cfg.comfyui_path}"})
    else:
        checks.append({"id": "comfyui_root", "status": "ok",
                       "message": str(cfg.comfyui_path)})

    offline_roots = [r for r in cfg.roots
                     if r.kind != "data" and not os.path.isdir(r.path)]
    checks.append({
        "id": "scan_roots",
        "status": "warn" if offline_roots else "ok",
        "message": (f"{len(offline_roots)} scan root(s) offline: "
                    + "; ".join(r.path for r in offline_roots[:3])
                    if offline_roots
                    else f"{len(cfg.roots)} root(s), all reachable"),
        "count": len(offline_roots),
    })

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
    cfg = config_service.get_config()
    if any(os.path.normcase(r.path) == os.path.normcase(str(path))
           for r in cfg.roots):
        raise ApiError("CONFLICT", "That folder is already a scan root.",
                       details={"path": str(path)})
    if body.kind == "extra_models":
        category = (body.category or "").strip()
        if category not in config_service.MODEL_CATEGORY_DIRS:
            raise ApiError(
                "VALIDATION_ERROR",
                "A model folder needs a category so its files are classified "
                "and placed correctly.",
                field_errors=[{"field": "category",
                               "message": "one of: "
                               + ", ".join(config_service.MODEL_CATEGORY_DIRS)}])
        current = [dict(d) for d in (cfg.raw.get("extra_model_dirs") or [])]
        current.append({"path": str(path), "category": category})
        cfg = config_service.set_config({"extra_model_dirs": current})
    elif body.kind == "extra_outputs":
        current = [str(d) for d in cfg.extra_output_dirs]
        cfg = config_service.set_config({"extra_output_dirs": [*current, str(path)]})
    else:
        current = [str(d) for d in cfg.extra_workflow_dirs]
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
    if target.kind == "extra_workflows":
        keep = [str(d) for d in cfg.extra_workflow_dirs
                if os.path.normcase(str(normalize(d))) != os.path.normcase(target.path)]
        config_service.set_config({"extra_workflow_dirs": keep})
    elif target.kind == "extra_outputs":
        keep = [str(d) for d in cfg.extra_output_dirs
                if os.path.normcase(str(normalize(d))) != os.path.normcase(target.path)]
        config_service.set_config({"extra_output_dirs": keep})
    elif target.kind == "extra_models" and target.source == "manual":
        keep = [dict(d) for d in (cfg.raw.get("extra_model_dirs") or [])
                if os.path.normcase(str(normalize(str(d.get("path") or ""))))
                != os.path.normcase(target.path)]
        config_service.set_config({"extra_model_dirs": keep})
    else:
        raise ApiError("CONFLICT",
                       "Only folders added by hand can be removed here. Roots "
                       "from extra_model_paths.yaml are managed in that file.",
                       details={"kind": target.kind, "source": target.source})
    return {"deleted": True, "id": root_id}


@router.post("/thumbs/gc", response_model=ThumbsGcResponse, responses=MUTATION_ERRORS,
             summary="Trim the thumbnail cache to a byte budget")
def thumbs_gc(body: ThumbsGcRequest) -> dict:
    result = get_thumb_service().gc(body.max_mb)
    return {"deleted": int(result.get("removed") or 0),
            "freed_bytes": int(result.get("freed_bytes") or 0),
            "remaining_bytes": int(result.get("remaining_bytes") or 0)}


# ---------------------------------------------------------------------------
# App self-update (API_CONTRACT 1.9)
# ---------------------------------------------------------------------------

@router.get("/app-update", response_model=AppUpdateStatus, responses=BASE_ERRORS,
            summary="Current version, the newest release, and anything staged")
def app_update_status() -> dict:
    """Never fails: an unreachable GitHub is a ``state`` value, not a 5xx."""
    return app_update_service.status()


@router.post("/app-update/check", response_model=AppUpdateStatus,
             responses=MUTATION_ERRORS,
             summary="Re-check now, bypassing the cached answer")
def app_update_check() -> dict:
    return app_update_service.status(force=True)


@router.post("/app-update/download", response_model=AppUpdateDownloadResponse,
             responses={**error_responses("FEATURE_UNAVAILABLE", "VALIDATION_ERROR",
                                          "UPSTREAM_UNAVAILABLE"),
                        **MUTATION_ERRORS},
             summary="Download and stage the newest release; applied on next launch")
def app_update_download() -> dict:
    """Downloads, checksum-checks and unpacks into ``backend/data/updates``.

    Nothing in the running installation is touched - the swap happens in
    ``apply_update.py`` at the next launch, when no module is loaded.
    """
    release = app_update_service.fetch_latest(force=True)
    if release is None:
        raise ApiError("UPSTREAM_UNAVAILABLE",
                       "No published release was found to download.",
                       details={"repository": app_update_service.REPO_NAME})
    if not app_update_service.is_newer(release.version, buildcfg.VERSION):
        raise ApiError("VALIDATION_ERROR",
                       f"This install is already {buildcfg.VERSION}; "
                       f"the newest release is {release.version}.",
                       details={"current": buildcfg.VERSION,
                                "latest": release.version})
    return {"ok": True, "pending": app_update_service.stage(release),
            "restart_required": True}


@router.post("/app-update/discard", response_model=AppUpdateDiscardResponse,
             responses=MUTATION_ERRORS,
             summary="Throw away a staged update without applying it")
def app_update_discard() -> dict:
    return {"discarded": app_update_service.discard()}


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
