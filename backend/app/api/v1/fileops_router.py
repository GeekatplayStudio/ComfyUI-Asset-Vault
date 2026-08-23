"""``/api/v1/fileops`` - rename / move / delete / trash (API_CONTRACT 11).

Batch calls are per-item isolated: ``services/file_ops`` returns one ``OpResult``
per uid and never aborts on the first failure, so this router always answers
``200`` with a ``results[]`` array and only sets ``ok:false`` at the top level
when *every* item failed.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends

from ...core import config_service
from ...core.errors import AppError
from ...core.pathsafe import long_path
from ...jobs.thumb_service import get_thumb_service
from ...services import file_ops
from ..deps import Page, check_uid, check_uids, page_params
from ..middleware import ApiError, normalize_code
from ..schemas.common import MUTATION_ERRORS, error_responses
from ..schemas.fileops import (
    CreateFolderRequest,
    CreateFolderResponse,
    DeleteRequest,
    DeleteResponse,
    MoveRequest,
    MoveResponse,
    RenameRequest,
    RenameResponse,
    TrashEmptyRequest,
    TrashEmptyResponse,
    TrashList,
    TrashRestoreRequest,
    TrashRestoreResponse,
)

router = APIRouter(prefix="/fileops", tags=["File operations"])

FILEOPS_KINDS = ("model", "workflow", "output")
#: ``on_conflict`` in the contract vs. the vocabulary ``file_ops`` implements.
CONFLICT_MAP = {"fail": "fail", "rename": "keep_both", "skip": "fail",
                "overwrite": "overwrite"}


def _item_error(result: file_ops.OpResult, *, path_op: bool = False) -> dict:
    code = normalize_code(result.code, default="INTERNAL")
    # A rename/move/create-folder VALIDATION_ERROR is always a name/path problem:
    # the uid itself was validated before the service was ever called.
    if path_op and code == "VALIDATION_ERROR":
        code = "PATH_INVALID"
    return {"code": code, "message": result.message, "details": result.details or {}}


def _reraise(exc: AppError) -> None:
    raise ApiError(normalize_code(exc.code), exc.message, details=exc.details) from exc


@router.post("/rename", response_model=RenameResponse,
             responses={**error_responses("NOT_FOUND", "CONFLICT", "PATH_INVALID",
                                          "PATH_NOT_ALLOWED", "FILE_LOCKED",
                                          "FILE_MISSING"),
                        **MUTATION_ERRORS},
             summary="Rename a single asset (and its sidecars)")
def rename(body: RenameRequest) -> dict:
    uid = check_uid(body.uid, kinds=FILEOPS_KINDS)
    try:
        info = file_ops.resolve_file(uid)
        old_path = info["path"]
    except AppError:
        old_path = None
    result = file_ops.rename(uid, body.new_name, keep_extension=body.keep_extension,
                             rename_sidecars=body.rename_sidecars)
    if not result.ok:
        err = _item_error(result, path_op=True)
        raise ApiError(err["code"], err["message"] or "The rename failed.",
                       details=err["details"])
    new_path = result.details.get("path")
    relocated = 0
    if new_path and old_path and new_path != old_path:
        get_thumb_service().relocate(uid, old_path, new_path)
        relocated = 1
    return {"ok": True, "uid": uid, "old_path": old_path, "new_path": new_path,
            "sidecars_renamed": int(result.details.get("sidecars") or 0),
            "db_updated": True, "thumbs_relocated": relocated}


@router.post("/move", response_model=MoveResponse,
             responses={**error_responses("NOT_FOUND", "PATH_NOT_ALLOWED",
                                          "PAYLOAD_TOO_LARGE"),
                        **MUTATION_ERRORS},
             summary="Move assets to another root/folder")
def move(body: MoveRequest) -> dict:
    uids = check_uids(body.uids, kinds=FILEOPS_KINDS)
    try:
        results = file_ops.move(uids, body.target_root_id, body.target_folder,
                                create_missing=body.create_missing,
                                on_conflict=CONFLICT_MAP[body.on_conflict])
    except AppError as exc:
        _reraise(exc)
        raise  # unreachable, keeps the type checker honest

    moved = skipped = failed = 0
    items = []
    for result in results:
        if result.ok:
            moved += 1
            items.append({"uid": result.uid, "ok": True,
                          "new_path": result.details.get("path")})
            continue
        err = _item_error(result, path_op=True)
        # 'skip' is implemented here: the service reports the conflict, the
        # router decides whether that counts as a skip or a failure.
        if body.on_conflict == "skip" and err["code"] == "CONFLICT":
            skipped += 1
            items.append({"uid": result.uid, "ok": True, "new_path": None,
                          "error": {**err, "code": "SKIPPED"}})
        else:
            failed += 1
            items.append({"uid": result.uid, "ok": False, "error": err})
    return {"ok": failed < len(results) or not results, "moved": moved,
            "skipped": skipped, "failed": failed, "results": items}


@router.post("/delete", response_model=DeleteResponse,
             responses={**error_responses("NOT_FOUND", "PATH_NOT_ALLOWED",
                                          "FILE_LOCKED", "PAYLOAD_TOO_LARGE"),
                        **MUTATION_ERRORS},
             summary="Trash (default) or permanently delete assets")
def delete(body: DeleteRequest) -> dict:
    uids = check_uids(body.uids, kinds=FILEOPS_KINDS)
    mode = body.mode or config_service.get_config().trash_mode or "trash"
    if mode == "permanent" and not body.confirm:
        raise ApiError("VALIDATION_ERROR",
                       "Permanent deletion requires confirm=true.",
                       field_errors=[{"field": "confirm",
                                      "message": "must be true when mode='permanent'"}],
                       details={"mode": mode})
    sizes: dict[str, int] = {}
    for uid in uids:
        try:
            info = file_ops.resolve_file(uid)
            sizes[uid] = int(os.path.getsize(long_path(info["path"])))
        except (AppError, OSError):
            sizes[uid] = 0
    try:
        results = file_ops.delete(uids, mode=mode, confirm=body.confirm)
    except AppError as exc:
        _reraise(exc)
        raise

    deleted = 0
    freed = 0
    trash_ids: list[int] = []
    items = []
    for result in results:
        if result.ok:
            deleted += 1
            freed += sizes.get(result.uid, 0)
            trash_id = result.details.get("trash_id")
            if trash_id is not None:
                trash_ids.append(int(trash_id))
            items.append({"uid": result.uid, "ok": True})
        else:
            items.append({"uid": result.uid, "ok": False, "error": _item_error(result)})
    return {"ok": deleted > 0, "deleted": deleted, "mode": mode,
            "trash_ids": trash_ids, "freed_bytes": freed, "results": items}


@router.get("/trash", response_model=TrashList, responses=error_responses("INTERNAL"),
            summary="List recoverable items")
def list_trash(page: Page = Depends(page_params)) -> dict:
    raw = file_ops.trash_list(limit=page.limit, offset=page.offset)
    items = []
    total_bytes = 0
    for row in raw.get("items", []):
        original = str(row.get("original_path") or "")
        trash_path = str(row.get("trash_path") or "")
        size = int(row.get("size") or 0)
        total_bytes += size
        items.append({
            "id": int(row["id"]), "uid": row.get("uid"), "kind": row.get("kind"),
            "filename": os.path.basename(original), "original_path": original,
            "size": size, "deleted_at": row.get("deleted_at"),
            "purge_after": row.get("purge_after"),
            "restorable": bool(trash_path and os.path.isfile(long_path(trash_path))),
        })
    page_info = raw.get("page") or {}
    return {"items": items, "page": page_info,
            "summary": {"count": int(page_info.get("total") or len(items)),
                        "bytes": total_bytes}}


@router.post("/trash/restore", response_model=TrashRestoreResponse,
             responses={**error_responses("NOT_FOUND", "CONFLICT"), **MUTATION_ERRORS},
             summary="Put trashed files back where they came from")
def restore_trash(body: TrashRestoreRequest) -> dict:
    results = file_ops.trash_restore(list(body.ids),
                                     on_conflict=CONFLICT_MAP[body.on_conflict])
    restored = 0
    items = []
    for result, trash_id in zip(results, list(body.ids), strict=False):
        if result.ok:
            restored += 1
            items.append({"id": trash_id, "ok": True,
                          "path": result.details.get("path"),
                          "uid": result.details.get("uid_restored")})
        else:
            items.append({"id": trash_id, "ok": False, "error": _item_error(result)})
    return {"restored": restored, "results": items}


@router.post("/trash/empty", response_model=TrashEmptyResponse,
             responses=MUTATION_ERRORS,
             summary="Purge trash entries for good")
def empty_trash(body: TrashEmptyRequest) -> dict:
    if not body.confirm:
        raise ApiError("VALIDATION_ERROR", "Emptying the trash requires confirm=true.",
                       field_errors=[{"field": "confirm", "message": "must be true"}])
    before = file_ops.trash_list(limit=500, offset=0)
    ids = set(int(i) for i in (body.ids or []))
    freed = sum(int(row.get("size") or 0) for row in before.get("items", [])
                if not ids or int(row["id"]) in ids)
    result = file_ops.trash_empty(body.ids, body.older_than_days, confirm=True)
    return {"purged": int(result.get("removed") or 0), "freed_bytes": freed}


@router.post("/create-folder", status_code=201, response_model=CreateFolderResponse,
             responses={**error_responses("NOT_FOUND", "CONFLICT", "PATH_INVALID",
                                          "PATH_NOT_ALLOWED"),
                        **MUTATION_ERRORS},
             summary="Create a folder inside a configured root")
def create_folder(body: CreateFolderRequest) -> dict:
    try:
        result = file_ops.create_folder(body.root_id, body.folder)
    except AppError as exc:
        code = normalize_code(exc.code)
        if code == "VALIDATION_ERROR":
            code = "PATH_INVALID"
        raise ApiError(code, exc.message, details=exc.details) from exc
    return {"path": result["abs_path"]}
