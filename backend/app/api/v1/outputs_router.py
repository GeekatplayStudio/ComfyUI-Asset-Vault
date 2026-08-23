"""``/api/v1/outputs`` - API_CONTRACT 6."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ...core import config_service
from ...core.errors import ValidationError
from ...core.pathsafe import is_contained, long_path, validate_filename
from ...indexing.service import get_indexer
from ...services.queries import albums_query, outputs_query, tags_query, workflows_query
from ..deps import (
    MEDIA_KINDS,
    Page,
    apply_fields,
    check_enum,
    check_group,
    check_sort,
    csv_param,
    page_params,
)
from ..middleware import ApiError
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.models import AssetPatch, BulkPatchRequest, BulkPatchResponse
from ..schemas.outputs import (
    ExtractWorkflowRequest,
    ExtractWorkflowResponse,
    OutputDetail,
    OutputList,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/outputs", tags=["Outputs"])

REINDEX_WAIT_S = 20.0


def _filters(
    q: str | None = Query(None, max_length=512),
    smart: bool = Query(False),
    folder: str | None = Query(None, max_length=1024),
    media_kind: list[str] | None = Query(None),
    model_id: int | None = Query(None),
    workflow_id: int | None = Query(None),
    album_id: int | None = Query(None),
    root_id: list[int] | None = Query(None),
    tag: list[str] | None = Query(None),
    favorite: bool | None = Query(None),
    min_rating: int | None = Query(None, ge=0, le=5),
    has_metadata: bool | None = Query(None),
    sampler: list[str] | None = Query(None),
    seed: str | None = Query(None, max_length=64),
    steps_min: int | None = Query(None, ge=0),
    steps_max: int | None = Query(None, ge=0),
    cfg_min: float | None = Query(None),
    cfg_max: float | None = Query(None),
    width_min: int | None = Query(None, ge=0),
    width_max: int | None = Query(None, ge=0),
    height_min: int | None = Query(None, ge=0),
    height_max: int | None = Query(None, ge=0),
    size_min: int | None = Query(None, ge=0),
    size_max: int | None = Query(None, ge=0),
    date_from: int | None = Query(None),
    date_to: int | None = Query(None),
    include_missing: bool = Query(False),
) -> dict[str, Any]:
    check_enum("media_kind", media_kind, MEDIA_KINDS)
    raw = {"q": q, "smart": smart, "folder": folder, "media_kind": media_kind,
           "model_id": model_id, "workflow_id": workflow_id, "album_id": album_id,
           "root_id": root_id, "tag": tag, "favorite": favorite,
           "min_rating": min_rating, "has_metadata": has_metadata, "sampler": sampler,
           "seed": seed, "steps_min": steps_min, "steps_max": steps_max,
           "cfg_min": cfg_min, "cfg_max": cfg_max, "width_min": width_min,
           "width_max": width_max, "height_min": height_min, "height_max": height_max,
           "size_min": size_min, "size_max": size_max, "date_from": date_from,
           "date_to": date_to, "include_missing": include_missing}
    return {k: v for k, v in raw.items() if v is not None}


@router.get("", responses={200: {"model": OutputList}, **BASE_ERRORS},
            summary="List generated outputs")
def list_outputs(page: Page = Depends(page_params),
                 filters: dict = Depends(_filters),
                 sort: str | None = Query(None),
                 group: str | None = Query(None),
                 fields: str | None = Query(None)) -> dict:
    sort = check_sort("outputs", sort, has_q=bool(filters.get("q")))
    group = check_group("outputs", group)
    data = outputs_query.list_outputs(filters, sort=sort, group=group,
                                      limit=page.limit, offset=page.offset).as_dict()
    data["items"] = apply_fields(data["items"], csv_param("fields", fields))
    return data


@router.get("/{output_id}",
            responses={200: {"model": OutputDetail},
                       **error_responses("NOT_FOUND"), **BASE_ERRORS},
            summary="Output detail with generation provenance")
def get_output(output_id: int) -> dict:
    item = outputs_query.get_output(output_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"Output {output_id} does not exist.",
                       details={"uid": f"output:{output_id}"})
    return item


@router.get("/{output_id}/graph",
            responses={200: {"description": "The embedded prompt/workflow graph"},
                       **error_responses("NOT_FOUND"), **BASE_ERRORS},
            summary="Embedded generation graph")
def get_output_graph(output_id: int) -> Any:
    if outputs_query.get_output(output_id) is None:
        raise ApiError("NOT_FOUND", f"Output {output_id} does not exist.",
                       details={"uid": f"output:{output_id}"})
    graph = outputs_query.output_graph(output_id)
    if graph is None:
        raise ApiError("NOT_FOUND", "This output carries no embedded graph.",
                       details={"uid": f"output:{output_id}"})
    return JSONResponse(content=graph)


def _workflow_dir(root_id: int) -> Path:
    """The indexed workflow directory owned by ``root_id`` (DECISIONS D5)."""
    for path, root in config_service.workflow_dirs():
        if int(root.id) == int(root_id):
            return Path(path)
    raise ApiError("NOT_FOUND",
                   f"Root {root_id} has no workflow directory to write into.",
                   details={"root_id": root_id})


@router.post("/{output_id}/extract-workflow", status_code=201,
             response_model=ExtractWorkflowResponse,
             responses={**error_responses("NOT_FOUND", "CONFLICT", "PATH_INVALID",
                                          "PATH_NOT_ALLOWED"),
                        **MUTATION_ERRORS},
             summary="Save the embedded graph as a real workflow file and index it")
def extract_workflow(output_id: int, body: ExtractWorkflowRequest) -> dict:
    if outputs_query.get_output(output_id) is None:
        raise ApiError("NOT_FOUND", f"Output {output_id} does not exist.",
                       details={"uid": f"output:{output_id}"})
    graph = outputs_query.output_graph(output_id)
    if graph is None:
        raise ApiError("NOT_FOUND", "This output carries no embedded graph.",
                       details={"uid": f"output:{output_id}"})

    base = _workflow_dir(body.root_id)
    folder = str(body.folder or "").replace("\\", "/").strip("/")
    stem = body.name[:-5] if body.name.lower().endswith(".json") else body.name
    try:
        for part in [p for p in folder.split("/") if p]:
            validate_filename(part)
        validate_filename(stem + ".json")
    except ValidationError as exc:
        raise ApiError("PATH_INVALID", exc.message,
                       details={"name": body.name, "folder": body.folder}) from exc

    target_dir = base / folder if folder else base
    target = target_dir / f"{stem}.json"
    if not is_contained(target, base):
        raise ApiError("PATH_NOT_ALLOWED", "The target escapes its workflow root.",
                       details={"root_id": body.root_id, "folder": body.folder})
    if os.path.exists(long_path(str(target))):
        raise ApiError("CONFLICT", "A workflow with that name already exists.",
                       details={"existing_path": str(target)})
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with open(long_path(str(target)), "w", encoding="utf-8") as fh:
            json.dump(graph, fh, ensure_ascii=False, indent=1, default=str)
    except OSError as exc:
        raise ApiError("PATH_NOT_ALLOWED", f"Could not write the workflow: {exc}",
                       details={"path": str(target)}) from exc

    indexer = get_indexer()
    try:
        indexer.start(mode="targeted", phases=["workflows"], force=True,
                      trigger="extract-workflow")
        deadline = time.monotonic() + REINDEX_WAIT_S
        while indexer.running() and time.monotonic() < deadline:
            time.sleep(0.05)
    except Exception as exc:  # noqa: BLE001 - a busy indexer must not fail the write
        log.info("extract-workflow: reindex deferred (%s)", exc)

    # `workflows.folder` is relative to the ComfyUI root, not to the workflow
    # directory, so match on the freshly written file rather than on `folder`.
    listed = workflows_query.list_workflows({}, sort="-modified", limit=200)
    tail = os.path.normcase(f"{stem}.json")
    uid = next(
        (item["uid"] for item in listed.items
         if os.path.normcase(os.path.basename(str(item.get("rel_path") or ""))) == tail
         and str(item.get("folder") or "").endswith(folder)),
        "workflow:pending")
    return {"uid": uid, "abs_path": str(target)}


@router.patch("/{output_id}",
              responses={200: {"model": OutputDetail},
                         **error_responses("NOT_FOUND"), **MUTATION_ERRORS},
              summary="Update user metadata")
def patch_output(output_id: int, body: AssetPatch) -> dict:
    uid = f"output:{output_id}"
    patch = body.model_dump(exclude_unset=True)
    if patch:
        tags_query.patch_asset(uid, patch)
    item = outputs_query.get_output(output_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"Output {output_id} does not exist.",
                       details={"uid": uid})
    return item


@router.post("/bulk", response_model=BulkPatchResponse, responses=MUTATION_ERRORS,
             summary="Apply one patch to many outputs")
def bulk_patch_outputs(body: BulkPatchRequest) -> dict:
    patch = body.patch.model_dump(exclude_unset=True)
    album_id = patch.get("album_id")
    result = tags_query.bulk_patch(list(body.uids), patch) if patch else \
        {"updated": len(body.uids), "results": [{"uid": u, "ok": True} for u in body.uids]}
    if album_id is not None:
        albums_query.set_album_items(int(album_id), list(body.uids), mode="add")
    return result
