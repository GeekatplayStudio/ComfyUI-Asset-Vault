"""``/api/v1/workflows`` - API_CONTRACT 5."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ...services.queries import workflows_query
from ..deps import Page, apply_fields, check_group, check_sort, csv_param, page_params
from ..middleware import ApiError
from ..schemas.common import BASE_ERRORS, error_responses
from ..schemas.workflows import (
    GraphTooLarge,
    WorkflowDependencies,
    WorkflowDetail,
    WorkflowList,
)

router = APIRouter(prefix="/workflows", tags=["Workflows"])

GRAPH_INLINE_CAP = 32 * 1024 * 1024


def _filters(
    q: str | None = Query(None, max_length=512),
    smart: bool = Query(False),
    folder: str | None = Query(None, max_length=1024),
    base_model: list[str] | None = Query(None),
    runnable: bool | None = Query(None),
    missing_only: bool | None = Query(None),
    node_class: str | None = Query(None, max_length=200),
    model_id: int | None = Query(None),
    root_id: list[int] | None = Query(None),
    album_id: int | None = Query(None),
    tag: list[str] | None = Query(None),
    size_min: int | None = Query(None, ge=0),
    size_max: int | None = Query(None, ge=0),
    date_from: int | None = Query(None),
    date_to: int | None = Query(None),
    include_missing: bool = Query(False),
) -> dict[str, Any]:
    raw = {"q": q, "smart": smart, "folder": folder, "base_model": base_model,
           "runnable": runnable, "missing_only": missing_only, "node_class": node_class,
           "model_id": model_id, "root_id": root_id, "album_id": album_id, "tag": tag,
           "size_min": size_min, "size_max": size_max, "date_from": date_from,
           "date_to": date_to, "include_missing": include_missing}
    return {k: v for k, v in raw.items() if v is not None}


@router.get("", responses={200: {"model": WorkflowList}, **BASE_ERRORS},
            summary="List workflows")
def list_workflows(page: Page = Depends(page_params),
                   filters: dict = Depends(_filters),
                   sort: str | None = Query(None),
                   group: str | None = Query(None),
                   fields: str | None = Query(None)) -> dict:
    sort = check_sort("workflows", sort, has_q=bool(filters.get("q")))
    group = check_group("workflows", group)
    data = workflows_query.list_workflows(filters, sort=sort, group=group,
                                          limit=page.limit, offset=page.offset).as_dict()
    data["items"] = apply_fields(data["items"], csv_param("fields", fields))
    return data


@router.get("/{workflow_id}",
            responses={200: {"model": WorkflowDetail},
                       **error_responses("NOT_FOUND"), **BASE_ERRORS},
            summary="Workflow detail")
def get_workflow(workflow_id: int) -> dict:
    item = workflows_query.get_workflow(workflow_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"Workflow {workflow_id} does not exist.",
                       details={"uid": f"workflow:{workflow_id}"})
    return item


@router.get("/{workflow_id}/graph",
            responses={200: {"description": "The raw graph JSON"},
                       413: {"model": GraphTooLarge,
                             "description": "PAYLOAD_TOO_LARGE - use download_url"},
                       **error_responses("NOT_FOUND"), **BASE_ERRORS},
            summary="Raw workflow graph")
def get_workflow_graph(workflow_id: int,
                       format: str = Query("raw", pattern="^(ui|api|raw)$")) -> Any:
    if workflows_query.get_workflow(workflow_id) is None:
        raise ApiError("NOT_FOUND", f"Workflow {workflow_id} does not exist.",
                       details={"uid": f"workflow:{workflow_id}"})
    graph = workflows_query.workflow_graph(workflow_id, "api" if format == "api" else "raw")
    if graph is None:
        raise ApiError("NOT_FOUND", "This workflow has no readable graph.",
                       details={"uid": f"workflow:{workflow_id}", "format": format})
    blob = json.dumps(graph, ensure_ascii=False, default=str)
    if len(blob) > GRAPH_INLINE_CAP:
        raise ApiError(
            "PAYLOAD_TOO_LARGE",
            "This graph is larger than the 32 MB inline cap; download it instead.",
            details={"download_url": f"/api/v1/files/download?uid=workflow:{workflow_id}",
                     "size": len(blob)})
    return JSONResponse(content=graph)


@router.get("/{workflow_id}/dependencies",
            responses={200: {"model": WorkflowDependencies},
                       **error_responses("NOT_FOUND"), **BASE_ERRORS},
            summary="Model / node / embedding dependencies with registry hints")
def get_workflow_dependencies(workflow_id: int) -> dict:
    if workflows_query.get_workflow(workflow_id) is None:
        raise ApiError("NOT_FOUND", f"Workflow {workflow_id} does not exist.",
                       details={"uid": f"workflow:{workflow_id}"})
    return workflows_query.workflow_dependencies(workflow_id)
