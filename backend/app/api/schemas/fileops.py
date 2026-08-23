"""File operations and trash (API_CONTRACT 11)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import Lenient, PageInfo, Strict


class RenameRequest(Strict):
    uid: str
    new_name: str = Field(min_length=1, max_length=255)
    keep_extension: bool = True
    rename_sidecars: bool = True


class RenameResponse(Lenient):
    ok: bool
    uid: str
    old_path: str | None = None
    new_path: str | None = None
    sidecars_renamed: int = 0
    db_updated: bool = True
    thumbs_relocated: int = 0


class MoveRequest(Strict):
    uids: list[str] = Field(min_length=1, max_length=200)
    target_root_id: int
    target_folder: str = Field(default="", max_length=1024)
    create_missing: bool = True
    on_conflict: Literal["fail", "skip", "rename"] = "fail"


class OpItemResult(Lenient):
    uid: str
    ok: bool
    new_path: str | None = None
    error: dict[str, Any] | None = None


class MoveResponse(Lenient):
    ok: bool
    moved: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[OpItemResult] = []


class DeleteRequest(Strict):
    uids: list[str] = Field(min_length=1, max_length=200)
    mode: Literal["trash", "permanent"] | None = None
    confirm: bool = False


class DeleteResponse(Lenient):
    ok: bool
    deleted: int = 0
    mode: str = "trash"
    trash_ids: list[int] = []
    freed_bytes: int = 0
    results: list[OpItemResult] = []


class TrashItem(Lenient):
    id: int
    uid: str | None = None
    kind: str | None = None
    filename: str | None = None
    original_path: str | None = None
    size: int | None = None
    deleted_at: int | None = None
    purge_after: int | None = None
    restorable: bool = True


class TrashSummary(Lenient):
    count: int = 0
    bytes: int = 0


class TrashList(Lenient):
    items: list[TrashItem]
    page: PageInfo
    summary: TrashSummary


class TrashRestoreRequest(Strict):
    ids: list[int] = Field(min_length=1, max_length=200)
    on_conflict: Literal["fail", "rename", "overwrite"] = "rename"


class TrashRestoreResult(Lenient):
    id: int
    ok: bool
    path: str | None = None
    uid: str | None = None
    error: dict[str, Any] | None = None


class TrashRestoreResponse(Lenient):
    restored: int
    results: list[TrashRestoreResult]


class TrashEmptyRequest(Strict):
    ids: list[int] | None = None
    older_than_days: int | None = Field(default=None, ge=0, le=3650)
    confirm: bool = False


class TrashEmptyResponse(Lenient):
    purged: int
    freed_bytes: int = 0


class CreateFolderRequest(Strict):
    root_id: int
    folder: str = Field(min_length=1, max_length=1024)


class CreateFolderResponse(Lenient):
    path: str


class RevealResponse(Lenient):
    ok: bool
