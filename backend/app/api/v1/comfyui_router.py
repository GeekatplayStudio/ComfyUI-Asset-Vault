"""``/api/v1/comfyui`` - version, updater and official templates (C8).

Every route is read-only except ``POST /update/run``, which is the only place in
the product that starts a process.  Its contract is deliberately awkward: the
caller must first fetch ``/update/plan``, show the resolved absolute path, and
send that exact path back as ``confirm_path``.  A confirmation that does not
name what will run is not a confirmation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ...core import config_service
from ...core import db as dbmod
from ...core.errors import AppError
from ...services import comfyui_service
from ...services.queries import workflows_query
from ..deps import Page, page_params, sse_response, sse_stream
from ..middleware import ApiError, normalize_code
from ..schemas.comfyui import (
    ComfyUIInfo,
    LatestVersion,
    TemplateCatalogue,
    UpdatePlan,
    UpdateRunRequest,
    UpdateRunResponse,
    UpdateStatus,
    WorkflowOrigins,
)
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses

router = APIRouter(prefix="/comfyui", tags=["ComfyUI"])


def _reraise(exc: AppError) -> None:
    raise ApiError(normalize_code(exc.code), exc.message,
                   details=exc.details) from exc


@router.get("/info", response_model=ComfyUIInfo, responses=BASE_ERRORS,
            summary="Installed version, install flavour, packages, and updaters")
def info() -> dict:
    return comfyui_service.report()


@router.get("/latest", response_model=LatestVersion, responses=BASE_ERRORS,
            summary="Newest published release (read-only; offline is a 200)")
async def latest(
    force: bool = Query(False, description="Bypass the 6-hour cache."),
) -> dict:
    return await comfyui_service.check_latest(force=force)


@router.get("/update/plan", response_model=UpdatePlan,
            responses={**error_responses("NOT_FOUND", "CONFLICT"), **BASE_ERRORS},
            summary="Exactly what would run, so the user can confirm it")
def update_plan(
    updater: str | None = Query(None, description="Updater id; omit for the "
                                                  "recommended one."),
) -> dict:
    try:
        chosen = comfyui_service.resolve_updater(updater)
    except AppError as exc:
        _reraise(exc)
        raise
    install = comfyui_service.probe()
    running = comfyui_service.is_running()
    warnings: list[str] = []
    if chosen["id"] == "git" and (install.git or {}).get("shallow"):
        warnings.append(
            "This checkout is shallow, so 'git pull --ff-only' may refuse to "
            "fast-forward. The portable updater handles that case.")
    if chosen["id"] == "portable_with_deps":
        warnings.append(
            "This variant reinstalls Python dependencies. Only run it if "
            "dependencies are actually broken.")
    warnings.append("Restart ComfyUI after the update, then re-scan the vault.")

    resolved = chosen.get("path") or ""
    return {
        "updater": chosen["id"], "label": chosen["label"], "path": resolved,
        "working_dir": chosen.get("working_dir"), "command": chosen.get("command", []),
        "confirm_path": resolved,
        "running": running,
        "can_run": bool(resolved) and not running["running"],
        "blocked_reason": ("comfyui_running" if running["running"]
                           else None if resolved else "updater_path_unresolved"),
        "warnings": warnings,
        "alternatives": [u for u in install.updaters if u["id"] != chosen["id"]],
    }


@router.get("/update/status", response_model=UpdateStatus, responses=BASE_ERRORS,
            summary="State of the current or last update run")
def update_status() -> dict:
    return comfyui_service.update_status()


@router.post("/update/run", status_code=202, response_model=UpdateRunResponse,
             responses={**error_responses("NOT_FOUND", "CONFLICT",
                                          "VALIDATION_ERROR"),
                        **MUTATION_ERRORS},
             summary="Run the confirmed updater (never automatic, never scheduled)")
def update_run(body: UpdateRunRequest) -> dict:
    try:
        return comfyui_service.run_updater(body.updater,
                                           confirm_path=body.confirm_path,
                                           trigger="api")
    except AppError as exc:
        _reraise(exc)
        raise


@router.get("/update/stream", include_in_schema=True, responses=BASE_ERRORS,
            summary="Live updater output (SSE): output, done, error, heartbeat")
async def update_stream(request: Request):
    return sse_response(
        sse_stream(comfyui_service.UPDATE_CHANNEL.subscribe(), request,
                   close_on_done=True))


@router.get("/templates", response_model=TemplateCatalogue, responses=BASE_ERRORS,
            summary="Official ComfyUI workflow templates shipped with this install")
def templates(
    page: Page = Depends(page_params),
    bundle: str | None = Query(None, max_length=80),
    q: str | None = Query(None, max_length=120),
) -> dict:
    return comfyui_service.template_catalogue(
        limit=page.limit, offset=page.offset, bundle=bundle, q=q)


@router.get("/workflow-origins", response_model=WorkflowOrigins,
            responses=BASE_ERRORS,
            summary="Indexed workflows grouped by origin, plus the official catalogue")
def workflow_origins(
    include_templates: bool = Query(
        True, description="Also return the official template catalogue summary."),
) -> dict:
    groups = workflows_query.origin_groups()
    indexed_total = sum(int(g["count"]) for g in groups)
    catalogue = (comfyui_service.template_catalogue(limit=1, offset=0)
                 if include_templates
                 else {"available": False, "reason": "not_requested", "items": [],
                       "total": 0, "bundles": []})
    if include_templates and catalogue.get("available"):
        # Listed alongside the indexed groups so the owner sees their official
        # templates in one place, but counted separately: these are catalogued
        # read-only from the ComfyUI distribution, not vault rows.
        groups.append({
            "origin": "official_template",
            "label": "official template",
            "package": None,
            "count": int(catalogue.get("total") or 0),
            "runnable": 0, "broken": 0,
            "catalogued_only": True,
        })
    return {
        "total": indexed_total,
        "indexed_total": indexed_total,
        "catalogued_total": int(catalogue.get("total") or 0),
        "groups": groups,
        "official_templates": catalogue,
    }


@router.get("/path-policy", responses=BASE_ERRORS,
            summary="What happens to existing rows when the ComfyUI path changes")
def path_policy() -> dict:
    """C7.3 - the UI must be able to state this plainly before a path is saved."""
    from ...services import storage_service

    report = storage_service.roots_report()
    cfg = config_service.get_config()
    return {
        "policy": "retain",
        "summary": ("Rows indexed under the previous ComfyUI folder are kept. "
                    "Nothing is deleted when you change the path."),
        "details": [
            "The old root is retired, not removed: its models, workflows and "
            "outputs stay in the vault with their ratings, tags, notes and "
            "album membership intact.",
            "Retired roots are skipped by the missing-file sweep, so the old "
            "library survives even if that drive is later disconnected.",
            "File operations (rename, move, delete) are refused for retired "
            "roots - only configured roots are writable.",
            "Extra model roots from extra_model_paths.yaml are re-read from the "
            "new install; they are never silently carried over.",
            "Remove retired content explicitly from Storage when you are sure.",
        ],
        "reindex_recommended": True,
        "current_roots": len(cfg.roots),
        "retired_roots": report["retired_roots"],
        "retired_bytes": report["retired_bytes"],
        "checked_at": dbmod.now_ms(),
    }
