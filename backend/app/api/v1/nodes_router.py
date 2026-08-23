"""``/api/v1/node-packages`` and ``/api/v1/node-classes`` - API_CONTRACT 4."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query

from ...core import config_service
from ...services.queries import nodes_query
from ..deps import Page, apply_fields, check_group, check_sort, csv_param, page_params
from ..middleware import ApiError
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.nodes import (
    CheckUpdateResponse,
    CheckUpdatesRequest,
    CheckUpdatesResponse,
    NodeClassDetail,
    NodeClassList,
    NodePackageDetail,
    NodePackageList,
    UpdateStatusResponse,
)

packages_router = APIRouter(prefix="/node-packages", tags=["Nodes"])
classes_router = APIRouter(prefix="/node-classes", tags=["Nodes"])


def _package_filters(
    q: str | None = Query(None, max_length=512),
    smart: bool = Query(False),
    official: bool | None = Query(None),
    enabled: bool | None = Query(None),
    has_update: bool | None = Query(None),
    author: list[str] | None = Query(None),
    tag: list[str] | None = Query(None),
    update_state: list[str] | None = Query(None),
    include_missing: bool = Query(False),
) -> dict[str, Any]:
    raw = {"q": q, "smart": smart, "official": official, "enabled": enabled,
           "has_update": has_update, "author": author, "tag": tag,
           "update_state": update_state, "include_missing": include_missing}
    return {k: v for k, v in raw.items() if v is not None}


def _class_filters(
    q: str | None = Query(None, max_length=512),
    smart: bool = Query(False),
    package_id: int | None = Query(None),
    category: list[str] | None = Query(None),
    official: bool | None = Query(None),
    deprecated: bool | None = Query(None),
    experimental: bool | None = Query(None),
    confidence: list[str] | None = Query(None),
) -> dict[str, Any]:
    raw = {"q": q, "smart": smart, "package_id": package_id, "category": category,
           "official": official, "deprecated": deprecated,
           "experimental": experimental, "confidence": confidence}
    return {k: v for k, v in raw.items() if v is not None}


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

@packages_router.get("", responses={200: {"model": NodePackageList}, **BASE_ERRORS},
                     summary="List custom node packages")
def list_node_packages(page: Page = Depends(page_params),
                       filters: dict = Depends(_package_filters),
                       sort: str | None = Query(None),
                       group: str | None = Query(None),
                       fields: str | None = Query(None)) -> dict:
    sort = check_sort("node_packages", sort, has_q=bool(filters.get("q")))
    group = check_group("node_packages", group)
    data = nodes_query.list_node_packages(filters, sort=sort, group=group,
                                          limit=page.limit, offset=page.offset).as_dict()
    data["items"] = apply_fields(data["items"], csv_param("fields", fields))
    return data


@packages_router.get("/update-status", response_model=UpdateStatusResponse,
                     responses=BASE_ERRORS,
                     summary="Aggregate update-check progress")
def node_package_update_status() -> dict:
    facets = nodes_query.node_facets({})
    states = [{"value": row["value"], "count": int(row["count"])}
              for row in facets.get("update_state", [])]
    by_state = {str(row["value"]): int(row["count"]) for row in states}
    total = sum(by_state.values())
    listed = nodes_query.list_node_packages({"has_update": True}, limit=1)
    return {
        "states": states,
        "pending": by_state.get("pending", 0),
        "with_update": int(listed.page.get("total") or 0),
        "checked": total - by_state.get("none", 0) - by_state.get("pending", 0),
        "total": total,
    }


@packages_router.post("/check-updates", status_code=202,
                      response_model=CheckUpdatesResponse,
                      responses={**error_responses("FEATURE_UNAVAILABLE"),
                                 **MUTATION_ERRORS},
                      summary="Queue an update check for many packages")
def check_node_package_updates(body: CheckUpdatesRequest) -> dict:
    cfg = config_service.get_config()
    if not cfg.online_enabled:
        raise ApiError("FEATURE_UNAVAILABLE",
                       "Online checks are disabled. Enable them in Settings first.",
                       details={"reason": "online_disabled"})
    raise ApiError("FEATURE_UNAVAILABLE",
                   "Git upstream comparison is not available in this build.",
                   details={"reason": "update_check_unavailable",
                            "requested": len(body.ids or [])})


@packages_router.get("/{package_id}",
                     responses={200: {"model": NodePackageDetail},
                                **error_responses("NOT_FOUND"), **BASE_ERRORS},
                     summary="Node package detail")
def get_node_package(package_id: int) -> dict:
    item = nodes_query.get_node_package(package_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"Node package {package_id} does not exist.",
                       details={"uid": f"node_package:{package_id}"})
    return item


@packages_router.get("/{package_id}/classes",
                     responses={200: {"model": NodeClassList},
                                **error_responses("NOT_FOUND"), **BASE_ERRORS},
                     summary="Node classes provided by one package")
def get_node_package_classes(package_id: int, page: Page = Depends(page_params),
                             sort: str | None = Query(None),
                             group: str | None = Query(None)) -> dict:
    if nodes_query.get_node_package(package_id) is None:
        raise ApiError("NOT_FOUND", f"Node package {package_id} does not exist.",
                       details={"uid": f"node_package:{package_id}"})
    sort = check_sort("node_classes", sort, has_q=False)
    group = check_group("node_classes", group)
    return nodes_query.list_node_classes({"package_id": package_id}, sort=sort,
                                         group=group, limit=page.limit,
                                         offset=page.offset).as_dict()


@packages_router.post("/{package_id}/check-update", status_code=202,
                      response_model=CheckUpdateResponse,
                      responses={200: {"model": CheckUpdateResponse,
                                       "description": "suspect_remote"},
                                 **error_responses("NOT_FOUND", "FEATURE_UNAVAILABLE",
                                                   "RATE_LIMITED"),
                                 **MUTATION_ERRORS},
                      summary="Compare one package against its git remote")
def check_node_package_update(package_id: int) -> dict:
    item = nodes_query.get_node_package(package_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"Node package {package_id} does not exist.",
                       details={"uid": f"node_package:{package_id}"})
    repo = item.get("repo") or {}
    if repo.get("suspect"):
        return {"state": "suspect_remote",
                "reason": "remote does not match folder"}
    if not repo.get("url"):
        return {"state": "none", "reason": "no git remote recorded"}
    if not config_service.get_config().online_enabled:
        raise ApiError("FEATURE_UNAVAILABLE",
                       "Online checks are disabled. Enable them in Settings first.",
                       details={"reason": "online_disabled"})
    raise ApiError("FEATURE_UNAVAILABLE",
                   "Git upstream comparison is not available in this build.",
                   details={"reason": "update_check_unavailable",
                            "job_hint": f"upd-{uuid.uuid4().hex[:4]}"})


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

@classes_router.get("", responses={200: {"model": NodeClassList}, **BASE_ERRORS},
                    summary="List node classes")
def list_node_classes(page: Page = Depends(page_params),
                      filters: dict = Depends(_class_filters),
                      sort: str | None = Query(None),
                      group: str | None = Query(None),
                      fields: str | None = Query(None)) -> dict:
    sort = check_sort("node_classes", sort, has_q=bool(filters.get("q")))
    group = check_group("node_classes", group)
    data = nodes_query.list_node_classes(filters, sort=sort, group=group,
                                         limit=page.limit, offset=page.offset).as_dict()
    data["items"] = apply_fields(data["items"], csv_param("fields", fields))
    return data


@classes_router.get("/{class_id}",
                    responses={200: {"model": NodeClassDetail},
                               **error_responses("NOT_FOUND"), **BASE_ERRORS},
                    summary="Node class detail")
def get_node_class(class_id: int) -> dict:
    item = nodes_query.get_node_class(class_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"Node class {class_id} does not exist.",
                       details={"uid": f"node_class:{class_id}"})
    return item
