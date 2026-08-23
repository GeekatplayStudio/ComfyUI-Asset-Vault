"""``/api/v1/albums`` - the left-rail tree and manual membership (API_CONTRACT 12)."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ...core.errors import AppError
from ...services.queries import albums_query
from ..deps import check_uids
from ..middleware import ApiError, normalize_code
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.library import (
    AlbumCreate,
    AlbumDeleted,
    AlbumItemsAdded,
    AlbumItemsRemoved,
    AlbumItemsRequest,
    AlbumNode,
    AlbumPatch,
    AlbumTree,
)

router = APIRouter(prefix="/albums", tags=["Albums & tags"])

ALBUM_UID_KINDS = ("model", "node_package", "node_class", "workflow", "output")


def _decorate(node: dict) -> dict:
    node = dict(node)
    node["editable"] = node.get("kind") != "system"
    node["children"] = [_decorate(child) for child in node.get("children") or []]
    return node


@router.get("", response_model=AlbumTree, responses=BASE_ERRORS,
            summary="Album tree with item counts")
def list_albums(scope: str | None = Query(None)) -> dict:
    if scope is not None and scope not in albums_query.SCOPES:
        raise ApiError("VALIDATION_ERROR", f"Unknown scope '{scope}'.",
                       field_errors=[{"field": "scope",
                                      "message": f"must be one of "
                                                 f"{'|'.join(albums_query.SCOPES)}"}])
    tree = albums_query.album_tree(scope)
    return {"scope": tree["scope"], "nodes": [_decorate(n) for n in tree["nodes"]]}


@router.post("", status_code=201, response_model=AlbumNode,
             responses={**error_responses("CONFLICT"), **MUTATION_ERRORS},
             summary="Create an album")
def create_album(body: AlbumCreate) -> dict:
    try:
        album = albums_query.create_album(
            body.name, scope=body.scope, kind=body.kind, parent_id=body.parent_id,
            icon=body.icon, color=body.color, query=body.query_json)
    except AppError as exc:
        raise ApiError(normalize_code(exc.code), exc.message,
                       details=exc.details) from exc
    return _decorate(album)


def _require_editable(album_id: int) -> dict:
    album = albums_query.get_album(album_id)
    if album is None:
        raise ApiError("NOT_FOUND", f"Album {album_id} does not exist.",
                       details={"album_id": album_id})
    if album.get("kind") == "system":
        raise ApiError("CONFLICT", "System albums are read-only.",
                       details={"album_id": album_id, "kind": "system"})
    return album


@router.patch("/{album_id}", response_model=AlbumNode,
              responses={**error_responses("NOT_FOUND", "CONFLICT"), **MUTATION_ERRORS},
              summary="Rename, recolour or reparent an album")
def patch_album(album_id: int, body: AlbumPatch) -> dict:
    _require_editable(album_id)
    patch = body.model_dump(exclude_unset=True)
    if "query_json" in patch:
        patch["query"] = patch.pop("query_json")
    if not patch:
        return _decorate(albums_query.get_album(album_id) or {})
    try:
        return _decorate(albums_query.update_album(album_id, patch))
    except AppError as exc:
        raise ApiError(normalize_code(exc.code), exc.message,
                       details=exc.details) from exc


@router.delete("/{album_id}", response_model=AlbumDeleted,
               responses={**error_responses("NOT_FOUND", "CONFLICT"), **MUTATION_ERRORS},
               summary="Delete an album (never touches files)")
def delete_album(album_id: int, delete_items: bool = Query(False)) -> dict:
    _require_editable(album_id)
    if delete_items:
        raise ApiError("VALIDATION_ERROR",
                       "Deleting an album never deletes files; use /fileops/delete.",
                       field_errors=[{"field": "delete_items",
                                      "message": "only false is supported"}])
    try:
        albums_query.delete_album(album_id)
    except AppError as exc:
        raise ApiError(normalize_code(exc.code), exc.message,
                       details=exc.details) from exc
    return {"deleted": True, "id": album_id}


@router.post("/{album_id}/items", response_model=AlbumItemsAdded,
             responses={**error_responses("NOT_FOUND"), **MUTATION_ERRORS},
             summary="Add assets to an album")
def add_album_items(album_id: int, body: AlbumItemsRequest) -> dict:
    uids = check_uids(body.uids, kinds=ALBUM_UID_KINDS)
    try:
        result = albums_query.set_album_items(album_id, uids, mode="add")
    except AppError as exc:
        raise ApiError(normalize_code(exc.code), exc.message,
                       details=exc.details) from exc
    return {"added": int(result.get("changed") or 0)}


@router.delete("/{album_id}/items", response_model=AlbumItemsRemoved,
               responses={**error_responses("NOT_FOUND"), **MUTATION_ERRORS},
               summary="Remove assets from an album")
def remove_album_items(album_id: int, body: AlbumItemsRequest) -> dict:
    uids = check_uids(body.uids, kinds=ALBUM_UID_KINDS)
    try:
        result = albums_query.set_album_items(album_id, uids, mode="remove")
    except AppError as exc:
        raise ApiError(normalize_code(exc.code), exc.message,
                       details=exc.details) from exc
    return {"removed": int(result.get("changed") or 0)}
