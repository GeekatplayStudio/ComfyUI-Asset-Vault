"""``/api/v1/tags`` - API_CONTRACT 12.

Listing, creation and assignment go through ``services/queries/tags_query``.
Rename / recolour / delete of a tag *row* has no service function in the current
backend-core surface, so this router performs those three writes itself through
the shared writer thread with a fixed column allowlist and fully bound
parameters.  Flagged for backend-core to absorb into ``tags_query``.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from ...core import db as dbmod
from ...core.errors import AppError
from ...services.queries import tags_query
from ..deps import Page, check_uids, page_params
from ..middleware import ApiError, normalize_code
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.library import (
    TagAssignRequest,
    TagAssignResponse,
    TagCreate,
    TagDeleted,
    TagItem,
    TagList,
    TagPatch,
)

router = APIRouter(prefix="/tags", tags=["Albums & tags"])

TAG_UID_KINDS = ("model", "node_package", "node_class", "workflow", "output")
#: The only columns this router may write.  Never derived from user input.
TAG_COLUMNS = ("name", "name_key", "color")


def _get_tag(tag_id: int) -> dict | None:
    listed = tags_query.list_tags(limit=500)
    return next((t for t in listed.items if int(t["id"]) == int(tag_id)), None)


def _write_tag(tag_id: int, fields: dict) -> int:
    columns = [c for c in TAG_COLUMNS if c in fields]
    if not columns:
        return 0
    assignment = ", ".join(f"{c} = ?" for c in columns)
    values = [fields[c] for c in columns]

    def _op(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(f"UPDATE tags SET {assignment} WHERE id = ?",  # noqa: S608
                           (*values, int(tag_id)))
        count = cur.rowcount or 0
        conn.commit()
        return count

    return int(dbmod.writer().run(_op))


def _delete_tag(tag_id: int) -> int:
    def _op(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM asset_tags WHERE tag_id = ?", (int(tag_id),))
        cur = conn.execute("DELETE FROM tags WHERE id = ?", (int(tag_id),))
        count = cur.rowcount or 0
        conn.commit()
        return count

    return int(dbmod.writer().run(_op))


@router.get("", response_model=TagList, responses=BASE_ERRORS,
            summary="Tags ordered by use count")
def list_tags(page: Page = Depends(page_params),
              q: str | None = Query(None, max_length=200),
              scope: str | None = Query(None)) -> dict:
    result = tags_query.list_tags(q=q, limit=page.limit, offset=page.offset)
    return {"items": result.items, "page": result.page}


@router.post("", status_code=201, response_model=TagItem,
             responses={**error_responses("CONFLICT"), **MUTATION_ERRORS},
             summary="Create a tag")
def create_tag(body: TagCreate) -> dict:
    created = tags_query.ensure_tags([body.name])
    tag_id = created.get(body.name.strip().lower())
    if tag_id is None:
        raise ApiError("VALIDATION_ERROR", "That tag name could not be stored.",
                       field_errors=[{"field": "name", "message": "invalid tag name"}])
    if body.color:
        _write_tag(tag_id, {"color": body.color})
    item = _get_tag(tag_id)
    return item or {"id": tag_id, "name": body.name, "color": body.color,
                    "source": "user", "count": 0}


@router.patch("/{tag_id}", response_model=TagItem,
              responses={**error_responses("NOT_FOUND", "CONFLICT"), **MUTATION_ERRORS},
              summary="Rename or recolour a tag")
def patch_tag(tag_id: int, body: TagPatch) -> dict:
    if _get_tag(tag_id) is None:
        raise ApiError("NOT_FOUND", f"Tag {tag_id} does not exist.",
                       details={"tag_id": tag_id})
    fields: dict = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
        fields["name_key"] = body.name.strip().lower()
    if body.color is not None:
        fields["color"] = body.color
    if fields:
        try:
            _write_tag(tag_id, fields)
        except sqlite3.IntegrityError as exc:
            raise ApiError("CONFLICT", "Another tag already uses that name.",
                           details={"name": body.name}) from exc
    item = _get_tag(tag_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"Tag {tag_id} does not exist.",
                       details={"tag_id": tag_id})
    return item


@router.delete("/{tag_id}", response_model=TagDeleted,
               responses={**error_responses("NOT_FOUND"), **MUTATION_ERRORS},
               summary="Delete a tag and every assignment of it")
def delete_tag(tag_id: int) -> dict:
    if _get_tag(tag_id) is None:
        raise ApiError("NOT_FOUND", f"Tag {tag_id} does not exist.",
                       details={"tag_id": tag_id})
    _delete_tag(tag_id)
    return {"deleted": True, "id": tag_id}


@router.post("/assign", response_model=TagAssignResponse, responses=MUTATION_ERRORS,
             summary="Add and/or remove tags across many assets")
def assign_tags(body: TagAssignRequest) -> dict:
    uids = check_uids(body.uids, kinds=TAG_UID_KINDS)
    try:
        return tags_query.assign_tags(uids, add=body.add, remove=body.remove)
    except AppError as exc:
        raise ApiError(normalize_code(exc.code), exc.message,
                       details=exc.details) from exc
