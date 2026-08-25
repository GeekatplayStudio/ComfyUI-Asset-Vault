"""``/api/v1/models`` - API_CONTRACT 3."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from ...core import config_service
from ...services import civitai_service
from ...services.queries import albums_query, models_query, tags_query
from ..deps import (
    HASH_STATES,
    INTEGRITY_STATES,
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
from ..schemas.models import AssetPatch as ModelPatch
from ..schemas.models import (
    BulkPatchRequest,
    BulkPatchResponse,
    ModelDetail,
    ModelFacets,
    ModelGroups,
    ModelList,
    ModelUsage,
    RefreshMetadataRequest,
    RefreshMetadataResponse,
)

router = APIRouter(prefix="/models", tags=["Models"])


def _filters(
    q: str | None, smart: bool, category: list[str] | None, base_model: list[str] | None,
    role: list[str] | None, modality: list[str] | None, precision: list[str] | None,
    hash_state: list[str] | None, integrity: list[str] | None, root_id: list[int] | None,
    folder: str | None, album_id: int | None, tag: list[str] | None,
    favorite: bool | None, min_rating: int | None, has_update: bool | None,
    is_adapter: bool | None, size_min: int | None, size_max: int | None,
    date_from: int | None, date_to: int | None, include_missing: bool,
    missing_files_only: bool | None,
    integrity_not_ok: bool | None, unused: bool | None, untagged: bool | None,
) -> dict[str, Any]:
    check_enum("hash_state", hash_state, HASH_STATES)
    check_enum("integrity", integrity, INTEGRITY_STATES)
    raw = {
        "q": q, "smart": smart, "category": category, "base_model": base_model,
        "role": role, "modality": modality, "precision": precision,
        "hash_state": hash_state, "integrity": integrity, "root_id": root_id,
        "folder": folder, "album_id": album_id, "tag": tag, "favorite": favorite,
        "min_rating": min_rating, "has_update": has_update, "is_adapter": is_adapter,
        "size_min": size_min, "size_max": size_max, "date_from": date_from,
        "date_to": date_to, "include_missing": include_missing,
        "missing_files_only": missing_files_only,
        "integrity_not_ok": integrity_not_ok,
        "unused": unused, "untagged": untagged,
    }
    return {k: v for k, v in raw.items() if v is not None}


def _filter_query(
    q: str | None = Query(None, max_length=512),
    smart: bool = Query(False),
    category: list[str] | None = Query(None),
    base_model: list[str] | None = Query(None),
    role: list[str] | None = Query(None),
    modality: list[str] | None = Query(None),
    precision: list[str] | None = Query(None),
    hash_state: list[str] | None = Query(None),
    integrity: list[str] | None = Query(None),
    root_id: list[int] | None = Query(None),
    folder: str | None = Query(None, max_length=1024),
    album_id: int | None = Query(None),
    tag: list[str] | None = Query(None),
    favorite: bool | None = Query(None),
    min_rating: int | None = Query(None, ge=0, le=5),
    has_update: bool | None = Query(None),
    is_adapter: bool | None = Query(None),
    size_min: int | None = Query(None, ge=0),
    size_max: int | None = Query(None, ge=0),
    date_from: int | None = Query(None),
    date_to: int | None = Query(None),
    include_missing: bool = Query(False),
    missing_files_only: bool | None = Query(
        None, description="Return only models whose indexed file is missing."),
    integrity_not_ok: bool | None = Query(
        None, description="Return only models whose integrity state is not 'ok'."),
    unused: bool | None = Query(
        None, description="Return only models referenced by no workflow or output."),
    untagged: bool | None = Query(None, description="Return only models with no tags."),
) -> dict[str, Any]:
    return _filters(q, smart, category, base_model, role, modality, precision,
                    hash_state, integrity, root_id, folder, album_id, tag, favorite,
                    min_rating, has_update, is_adapter, size_min, size_max, date_from,
                    date_to, include_missing, missing_files_only,
                    integrity_not_ok, unused, untagged)


@router.get("", responses={200: {"model": ModelList}, **BASE_ERRORS},
            summary="List models")
def list_models(page: Page = Depends(page_params),
                filters: dict = Depends(_filter_query),
                sort: str | None = Query(None),
                group: str | None = Query(None),
                fields: str | None = Query(None)) -> dict:
    sort = check_sort("models", sort, has_q=bool(filters.get("q")))
    group = check_group("models", group)
    result = models_query.list_models(filters, sort=sort, group=group,
                                      limit=page.limit, offset=page.offset)
    data = result.as_dict()
    data["items"] = apply_fields(data["items"], csv_param("fields", fields))
    return data


@router.get("/facets", responses={200: {"model": ModelFacets}, **BASE_ERRORS},
            summary="Facet counts honouring every filter except the facet's own field")
def model_facets(filters: dict = Depends(_filter_query)) -> dict:
    return models_query.model_facets(filters)


@router.get("/groups", responses={200: {"model": ModelGroups}, **BASE_ERRORS},
            summary="Left-rail group tree")
def model_groups(filters: dict = Depends(_filter_query),
                 group: str = Query("folder")) -> dict:
    group = check_group("models", group)
    if group in ("folder", "none"):
        return models_query.model_tree(filters)
    nodes = models_query.model_groups(filters, group)
    return {"group": group, "nodes": [{**n, "children": []} for n in nodes]}


@router.get("/{model_id}", responses={200: {"model": ModelDetail},
                                      **error_responses("NOT_FOUND"), **BASE_ERRORS},
            summary="Model detail")
def get_model(model_id: int) -> dict:
    item = models_query.get_model(model_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"Model {model_id} does not exist.",
                       details={"uid": f"model:{model_id}"})
    return item


@router.get("/{model_id}/usage", responses={200: {"model": ModelUsage},
                                            **error_responses("NOT_FOUND"), **BASE_ERRORS},
            summary="Workflows and outputs that use this model")
def get_model_usage(model_id: int, page: Page = Depends(page_params)) -> dict:
    if models_query.get_model(model_id) is None:
        raise ApiError("NOT_FOUND", f"Model {model_id} does not exist.",
                       details={"uid": f"model:{model_id}"})
    return models_query.model_usage(model_id, limit=page.limit, offset=page.offset)


@router.post("/{model_id}/refresh-metadata", status_code=202,
             response_model=RefreshMetadataResponse,
             responses={**error_responses("NOT_FOUND", "CONFLICT", "FEATURE_UNAVAILABLE"),
                        **MUTATION_ERRORS},
             summary="Re-query Civitai for this model")
def refresh_metadata(model_id: int, body: RefreshMetadataRequest,
                     background: BackgroundTasks) -> dict:
    item = models_query.get_model(model_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"Model {model_id} does not exist.",
                       details={"uid": f"model:{model_id}"})
    cfg = config_service.get_config()
    if not (cfg.online_enabled and cfg.civitai_enabled):
        raise ApiError("FEATURE_UNAVAILABLE",
                       "Online enrichment is disabled. Enable it in Settings first.",
                       details={"reason": "online_disabled"})
    if (item.get("hash") or {}).get("state") != "done":
        raise ApiError("CONFLICT",
                       "This model has no SHA-256 yet. Hash it to enable Civitai matching.",
                       details={"reason": "hash_required"})
    background.add_task(civitai_service.enrich_model, model_id, force=body.force)
    return {"state": "pending"}


@router.patch("/{model_id}", responses={200: {"model": ModelDetail},
                                        **error_responses("NOT_FOUND"), **MUTATION_ERRORS},
              summary="Update user metadata")
def patch_model(model_id: int, body: ModelPatch) -> dict:
    uid = f"model:{model_id}"
    patch = body.model_dump(exclude_unset=True)
    album_id = patch.pop("album_id", None)
    if patch:
        tags_query.patch_asset(uid, patch)
    elif models_query.get_model(model_id) is None:
        raise ApiError("NOT_FOUND", f"Model {model_id} does not exist.",
                       details={"uid": uid})
    if album_id is not None:
        albums_query.set_album_items(int(album_id), [uid], mode="add")
    item = models_query.get_model(model_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"Model {model_id} does not exist.",
                       details={"uid": uid})
    return item


@router.post("/bulk", response_model=BulkPatchResponse, responses=MUTATION_ERRORS,
             summary="Apply one patch to many models")
def bulk_patch_models(body: BulkPatchRequest) -> dict:
    patch = body.patch.model_dump(exclude_unset=True)
    album_id = patch.pop("album_id", None)
    result = tags_query.bulk_patch(list(body.uids), patch) if patch else \
        {"updated": len(body.uids), "results": [{"uid": u, "ok": True} for u in body.uids]}
    if album_id is not None:
        albums_query.set_album_items(int(album_id), list(body.uids), mode="add")
    return result
