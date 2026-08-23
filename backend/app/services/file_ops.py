"""Rename / move / delete / trash - uid-driven, root-validated, transactional.

The client never supplies a filesystem path: every operation resolves a ``uid``
to a DB row, validates both source and destination through ``core/pathsafe``,
then updates the row, FTS, embeddings and thumbnail cache in one transaction.

Trash is an app-managed ``<root>/.vault-trash/`` directory, not the Recycle Bin
(ARCHITECTURE 9.1): a same-volume ``shutil.move`` is O(1) even for a 24 GB file
and stores exact restore metadata.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import TRASH_DIRNAME
from ..core import config_service, errors
from ..core import db as dbmod
from ..core.errors import (
    ConflictError,
    NotFoundError,
    PathNotAllowed,
    ValidationError,
)
from ..core.fingerprint import file_fingerprint
from ..core.pathsafe import (
    is_contained,
    long_path,
    normalize,
    path_key,
    resolve_within_roots,
    safe_relpath,
    validate_filename,
)
from ..search import sync

log = logging.getLogger(__name__)

MAX_BATCH = 200
SIDECAR_SUFFIXES = (".preview.png", ".preview.jpg", ".civitai.info", ".json", ".txt",
                    ".png", ".jpg", ".webp")

# Tables a restore may write to.  Gate PRAGMA/INSERT on this so a table name
# can never be attacker- or payload-controlled.
_RESTORABLE_TABLES = frozenset({"models", "model_files", "workflows", "outputs",
                                "node_packages", "node_classes"})
# model_files is deliberately absent: the searchable identity of a model is the
# `models` row, so restoring a file row must not emit a second document.
_KIND_FOR_TABLE = {"models": "model", "workflows": "workflow", "outputs": "output"}

UID_TABLES = {
    "model": ("model_files", "models"),
    "workflow": ("workflows", None),
    "output": ("outputs", None),
    "node_package": ("node_packages", None),
}

# Node-package policy (API_CONTRACT 11).  A custom_nodes folder is a Python
# package that ComfyUI imports *by folder name*, usually a git checkout serving
# its own web assets from that path.  Deleting it is the operation owners
# actually want and is safe because it is trash-backed and reversible.  Renaming
# or moving it is not: the folder name is the module name, so a rename silently
# changes what ComfyUI loads, breaks WEB_DIRECTORY asset URLs, and detaches the
# checkout from its registry identity - with no way to detect the breakage until
# ComfyUI next starts.  Those two are refused with a specific, displayable
# reason rather than a generic "unsupported uid kind".
NODE_PACKAGE_RENAME_REASON = (
    "A node package cannot be renamed or moved: ComfyUI imports it by folder "
    "name, so changing the folder changes which nodes load and breaks the "
    "package's web assets and git checkout. Disable or delete it instead."
)
DIRECTORY_KINDS = frozenset({"node_package"})


@dataclass
class OpResult:
    uid: str
    ok: bool
    code: str | None = None
    message: str | None = None
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"uid": self.uid, "ok": self.ok}
        if not self.ok:
            d["error"] = {"code": self.code, "message": self.message,
                          "details": self.details}
        else:
            d.update(self.details)
        return d


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _parse_uid(uid: str) -> tuple[str, int]:
    kind, _sep, num = str(uid).partition(":")
    if kind not in UID_TABLES:
        raise ValidationError(f"Operations are not supported for '{kind}'.",
                              details={"allowed": sorted(UID_TABLES)})
    try:
        return kind, int(num)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Malformed uid '{uid}'.") from exc


def _resolve(uid: str, conn: sqlite3.Connection | None = None) -> dict:
    kind, row_id = _parse_uid(uid)
    conn = conn or dbmod.get_ro()
    if kind == "model":
        row = dbmod.one(
            conn, "SELECT f.*, m.name FROM model_files f JOIN models m "
                  "ON m.id = f.model_id WHERE f.model_id = ? "
                  "ORDER BY (f.id = m.primary_file_id) DESC LIMIT 1", (row_id,))
        if row is None:
            row = dbmod.one(conn, "SELECT * FROM model_files WHERE id = ?", (row_id,))
    else:
        table = UID_TABLES[kind][0]
        row = dbmod.one(conn, f"SELECT * FROM {table} WHERE id = ?", (row_id,))  # noqa: S608
    if row is None:
        raise NotFoundError(f"{uid} does not exist.")
    return {"uid": uid, "kind": kind, "id": row_id, "row": dict(row),
            "abs_path": str(row["abs_path"])}


def _roots():
    return config_service.get_config().roots


def _validate_path(path: str | Path):
    return resolve_within_roots(path, _roots())


def _sidecars(path: str) -> list[str]:
    directory = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    out = []
    for suffix in SIDECAR_SUFFIXES:
        cand = os.path.join(directory, stem + suffix)
        try:
            if os.path.isfile(long_path(cand)) and cand != path:
                out.append(cand)
        except OSError:
            continue
    return out


def _unique_target(target: str) -> str:
    base, ext = os.path.splitext(target)
    n = 2
    candidate = target
    while os.path.exists(long_path(candidate)):
        candidate = f"{base} ({n}){ext}"
        n += 1
        if n > 999:
            raise ConflictError("Could not find a free filename.",
                                details={"path": target})
    return candidate


# ---------------------------------------------------------------------------
# DB updates
# ---------------------------------------------------------------------------

def _relocate_row(kind: str, row_id: int, old_path: str, new_path: str,
                  root_id: int | None = None) -> None:
    table = UID_TABLES[kind][0]
    new_abs = str(normalize(new_path))
    try:
        st = os.stat(long_path(new_abs))
        size = int(st.st_size)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except OSError:
        size, mtime_ns = 0, 0
    fp = file_fingerprint(new_abs, size, mtime_ns)
    filename = os.path.basename(new_abs)
    stem, ext = os.path.splitext(filename)
    uid = f"{kind}:{row_id}"

    def _op(conn: sqlite3.Connection, _touch) -> None:
        root = root_id
        if root is None:
            cur = conn.execute(f"SELECT root_id FROM {table} WHERE "  # noqa: S608
                               f"{'model_id' if kind == 'model' else 'id'} = ?", (row_id,))
            r = cur.fetchone()
            root = int(r["root_id"]) if r and r["root_id"] is not None else None
        base = None
        if root is not None:
            r = conn.execute("SELECT path FROM roots WHERE id = ?", (root,)).fetchone()
            base = str(r["path"]) if r else None
        rel = safe_relpath(new_abs, base) if base else filename
        folder = os.path.dirname(rel).replace("\\", "/")
        if kind == "model":
            conn.execute(
                "UPDATE model_files SET abs_path=?, path_key=?, rel_path=?, folder=?, "
                "filename=?, stem=?, ext=?, size=?, mtime_ns=?, fingerprint=?, "
                "root_id=COALESCE(?, root_id) WHERE model_id=? AND path_key=?",
                (new_abs, path_key(new_abs), rel, folder, filename, stem, ext, size,
                 mtime_ns, fp, root, row_id, path_key(old_path)),
            )
            conn.execute("UPDATE models SET name=?, updated_at=? WHERE id=?",
                         (stem, dbmod.now_ms(), row_id))
        elif kind == "workflow":
            conn.execute(
                "UPDATE workflows SET abs_path=?, path_key=?, rel_path=?, folder=?, "
                "name=?, size=?, mtime_ns=?, fingerprint=?, root_id=COALESCE(?, root_id), "
                "updated_at=? WHERE id=?",
                (new_abs, path_key(new_abs), rel, folder, stem, size, mtime_ns, fp,
                 root, dbmod.now_ms(), row_id),
            )
        else:
            conn.execute(
                "UPDATE outputs SET abs_path=?, path_key=?, rel_path=?, folder=?, "
                "filename=?, ext=?, size=?, mtime_ns=?, fingerprint=?, "
                "root_id=COALESCE(?, root_id), updated_at=? WHERE id=?",
                (new_abs, path_key(new_abs), rel, folder, filename, ext, size, mtime_ns,
                 fp, root, dbmod.now_ms(), row_id),
            )

    # The row's title/body changed, so the search document must change with it -
    # otherwise the new name is unsearchable and the old one still matches.
    sync.write_synced(_op, [uid])
    try:
        from ..jobs.thumb_service import get_thumb_service

        get_thumb_service().relocate(uid, old_path, new_abs)
    except Exception as exc:  # noqa: BLE001 - a stale thumbnail is never fatal
        log.debug("thumbnail relocate failed for %s: %s", uid, exc)


def _delete_row(kind: str, row_id: int) -> dict | None:
    table = UID_TABLES[kind][0]
    uid = f"{kind}:{row_id}"

    def _op(conn: sqlite3.Connection, touch) -> dict | None:
        # ON DELETE CASCADE takes the children's rows; register them so their
        # documents are dropped in the same transaction rather than orphaned.
        if kind == "node_package":
            for r in conn.execute(
                    "SELECT id FROM node_classes WHERE package_id = ?", (row_id,)):
                touch(f"node_class:{int(r['id'])}")
        if kind == "model":
            row = conn.execute("SELECT * FROM models WHERE id = ?", (row_id,)).fetchone()
            payload = dict(row) if row else None
            conn.execute("DELETE FROM models WHERE id = ?", (row_id,))
        else:
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?",  # noqa: S608
                               (row_id,)).fetchone()
            payload = dict(row) if row else None
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))  # noqa: S608
        conn.execute("DELETE FROM album_items WHERE uid = ?", (uid,))
        conn.execute("DELETE FROM asset_tags WHERE uid = ?", (uid,))
        conn.execute("DELETE FROM thumb_cache WHERE uid = ?", (uid,))
        return payload

    # sync_uid sees the row is gone and drops the document.
    return sync.write_synced(_op, [uid])


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def _refuse_directory_move(kind: str) -> None:
    if kind in DIRECTORY_KINDS:
        raise ValidationError(
            NODE_PACKAGE_RENAME_REASON,
            details={"kind": kind, "reason": "node_package_immovable",
                     "allowed": ["delete"]},
        )


def rename(uid: str, new_name: str, *, keep_extension: bool = True,
           rename_sidecars: bool = True, **_kw) -> OpResult:
    try:
        info = _resolve(uid)
        _refuse_directory_move(info["kind"])
        old = info["abs_path"]
        _validate_path(old)
        name = str(new_name).strip()
        if keep_extension:
            old_ext = os.path.splitext(old)[1]
            if old_ext and not name.lower().endswith(old_ext.lower()):
                name = os.path.splitext(name)[0] + old_ext
        validate_filename(name)
        target = os.path.join(os.path.dirname(old), name)
        if path_key(target) == path_key(old):
            return OpResult(uid, True, details={"path": old, "unchanged": True})
        if os.path.exists(long_path(target)):
            raise ConflictError("A file with that name already exists.",
                                details={"path": target})
        _validate_path(os.path.dirname(target))
        sidecars = _sidecars(old) if rename_sidecars else []
        os.replace(long_path(old), long_path(target))
        stem = os.path.splitext(name)[0]
        for sc in sidecars:
            sc_suffix = os.path.basename(sc)[len(os.path.splitext(os.path.basename(old))[0]):]
            try:
                os.replace(long_path(sc),
                           long_path(os.path.join(os.path.dirname(old), stem + sc_suffix)))
            except OSError:
                continue
        _relocate_row(info["kind"], info["id"], old, target)
        return OpResult(uid, True, details={"path": target, "name": name,
                                            "sidecars": len(sidecars)})
    except (ValidationError, NotFoundError, ConflictError, PathNotAllowed) as exc:
        return OpResult(uid, False, exc.code, exc.message, exc.details)
    except OSError as exc:
        return OpResult(uid, False, errors.classify_os_error(exc), _os_message(exc))


def move(uids: list[str], target_root_id: int, target_folder: str, *,
         create_missing: bool = True, on_conflict: str = "fail", **_kw) -> list[OpResult]:
    uids = list(uids or [])
    if len(uids) > MAX_BATCH:
        raise ValidationError(
            f"A single operation may affect at most {MAX_BATCH} items.",
            details={"requested": len(uids), "max": MAX_BATCH},
        )
    conn = dbmod.get_ro()
    root = dbmod.one(conn, "SELECT * FROM roots WHERE id = ?", (int(target_root_id),))
    if root is None:
        raise NotFoundError(f"Root {target_root_id} does not exist.")
    folder = str(target_folder or "").replace("\\", "/").strip("/")
    if ".." in folder.split("/"):
        raise ValidationError("Target folder may not contain '..'.")
    dest_dir = Path(str(root["path"])) / folder if folder else Path(str(root["path"]))
    if not is_contained(dest_dir, str(root["path"])):
        raise PathNotAllowed("Target folder escapes its root.",
                             details={"folder": target_folder})
    if not dest_dir.is_dir():
        if not create_missing:
            raise NotFoundError(f"Target folder '{folder}' does not exist.")
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PathNotAllowed(f"Could not create '{folder}': {exc}") from exc

    results: list[OpResult] = []
    for uid in uids:
        try:
            info = _resolve(uid, conn)
            _refuse_directory_move(info["kind"])
            old = info["abs_path"]
            _validate_path(old)
            target = str(dest_dir / os.path.basename(old))
            if path_key(target) == path_key(old):
                results.append(OpResult(uid, True, details={"path": old,
                                                            "unchanged": True}))
                continue
            if os.path.exists(long_path(target)):
                if on_conflict == "keep_both":
                    target = _unique_target(target)
                elif on_conflict == "overwrite":
                    os.remove(long_path(target))
                else:
                    raise ConflictError("A file with that name already exists.",
                                        details={"path": target})
            sidecars = _sidecars(old)
            shutil.move(long_path(old), long_path(target))
            for sc in sidecars:
                try:
                    shutil.move(long_path(sc),
                                long_path(str(dest_dir / os.path.basename(sc))))
                except OSError:
                    continue
            _relocate_row(info["kind"], info["id"], old, target, int(root["id"]))
            results.append(OpResult(uid, True, details={"path": target}))
        except (ValidationError, NotFoundError, ConflictError, PathNotAllowed) as exc:
            results.append(OpResult(uid, False, exc.code, exc.message, exc.details))
        except OSError as exc:
            results.append(OpResult(uid, False, errors.classify_os_error(exc),
                                    _os_message(exc)))
    return results


def delete(uids: list[str], mode: str = "trash", confirm: bool = False,
           **_kw) -> list[OpResult]:
    uids = list(uids or [])
    if len(uids) > MAX_BATCH:
        raise ValidationError(
            f"A single operation may affect at most {MAX_BATCH} items.",
            details={"requested": len(uids), "max": MAX_BATCH},
        )
    mode = (mode or "trash").lower()
    if mode not in ("trash", "permanent"):
        raise ValidationError("mode must be 'trash' or 'permanent'.")
    if mode == "permanent" and not confirm:
        raise ValidationError(
            "Permanent deletion requires confirm=true.",
            details={"mode": mode, "hint": "Use mode='trash' to keep it recoverable."},
        )
    conn = dbmod.get_ro()
    results: list[OpResult] = []
    for uid in uids:
        try:
            info = _resolve(uid, conn)
            old = info["abs_path"]
            _, root = _validate_path(old)
            is_dir = os.path.isdir(long_path(old))
            sidecars = [] if is_dir else _sidecars(old)
            if mode == "permanent":
                if is_dir:
                    shutil.rmtree(long_path(old), ignore_errors=False)
                for p in ([] if is_dir else [old]) + sidecars:
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(long_path(p))
                _delete_row(info["kind"], info["id"])
                results.append(OpResult(uid, True, details={"mode": "permanent"}))
                continue
            else:
                entry = _to_trash(info, old, sidecars, root)
                _delete_row(info["kind"], info["id"])
                extra = {}
                if info["kind"] == "node_package":
                    warning = git_dirty_warning(old)
                    if warning:
                        extra["warning"] = warning
                results.append(OpResult(uid, True,
                                        details={"mode": "trash", **entry, **extra}))
        except (ValidationError, NotFoundError, ConflictError, PathNotAllowed) as exc:
            results.append(OpResult(uid, False, exc.code, exc.message, exc.details))
        except OSError as exc:
            results.append(OpResult(uid, False, errors.classify_os_error(exc),
                                    _os_message(exc)))
    return results


def _to_trash(info: dict, path: str, sidecars: list[str], root) -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slot = f"{stamp}-{uuid.uuid4().hex[:8]}"
    trash_dir = Path(root.path) / TRASH_DIRNAME / slot
    trash_dir.mkdir(parents=True, exist_ok=True)
    target = trash_dir / os.path.basename(path)
    if os.path.isdir(long_path(path)):
        size = directory_size(path)
    else:
        try:
            size = os.path.getsize(long_path(path))
        except OSError:
            size = 0
    # Same volume, so moving a whole package directory is a rename: O(1).
    shutil.move(long_path(path), long_path(str(target)))
    moved = [str(target)]
    for sc in sidecars:
        try:
            shutil.move(long_path(sc), long_path(str(trash_dir / os.path.basename(sc))))
            moved.append(str(trash_dir / os.path.basename(sc)))
        except OSError:
            continue
    # For a model the logical row lives in `models`; `info["row"]` is the
    # physical model_files row, so capture both for an exact restore.
    logical = info["row"]
    file_row = None
    if info["kind"] == "model":
        found = dbmod.one(dbmod.get_ro(), "SELECT * FROM models WHERE id = ?",
                          (int(info["id"]),))
        if found is not None:
            logical = dict(found)
        # `_resolve` hands back a JOINed row (model_files + models.name), so it
        # must not be stored verbatim: `name` is not a model_files column.
        physical = dbmod.one(
            dbmod.get_ro(), "SELECT * FROM model_files WHERE path_key = ?",
            (path_key(path),))
        if physical is not None:
            file_row = dict(physical)
    payload = {
        "row": logical,
        "file_row": file_row,
        "sidecars": moved[1:],
        "tags": _tags_of(info["uid"]),
        "albums": _albums_of(info["uid"]),
    }
    if info["kind"] == "node_package":
        # node_classes are ON DELETE CASCADE, so capture them or a restore
        # silently loses every class until the next scan.
        payload["node_classes"] = [
            dict(r) for r in dbmod.rows(
                dbmod.get_ro(), "SELECT * FROM node_classes WHERE package_id = ?",
                (int(info["id"]),))
        ][:5000]
    meta = {"original_path": path, "deleted_at": dbmod.now_ms(), "size": size,
            "uid": info["uid"], "kind": info["kind"]}
    with contextlib.suppress(OSError):
        (trash_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str),
                                             encoding="utf-8")
    cfg = config_service.get_config()
    purge_after = dbmod.now_ms() + cfg.trash_retention_days * 86_400_000

    def _op(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO trash_items(uid,kind,original_path,trash_path,size,root_id,"
            "payload_json,deleted_at,purge_after) VALUES (?,?,?,?,?,?,?,?,?)",
            (info["uid"], info["kind"], path, str(target), size, root.id,
             json.dumps(payload, default=str)[:1_000_000], meta["deleted_at"],
             purge_after),
        )
        tid = int(cur.lastrowid)
        conn.commit()
        return tid

    trash_id = int(dbmod.writer().run(_op))
    return {"trash_id": trash_id, "trash_path": str(target)}


def trash_list(limit: int = 100, offset: int = 0,
               conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or dbmod.get_ro()
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    total = int(dbmod.scalar(conn, "SELECT COUNT(*) FROM trash_items") or 0)
    rows = dbmod.rows(
        conn, "SELECT id, uid, kind, original_path, trash_path, size, deleted_at, "
              "purge_after FROM trash_items ORDER BY deleted_at DESC LIMIT ? OFFSET ?",
        (limit, offset))
    return {
        "items": [dict(r) for r in rows],
        "page": {"limit": limit, "offset": offset, "total": total,
                 "returned": len(rows), "has_more": offset + len(rows) < total},
    }


def trash_restore(ids: list[int], on_conflict: str = "keep_both") -> list[OpResult]:
    conn = dbmod.get_ro()
    results: list[OpResult] = []
    for tid in list(ids or [])[:MAX_BATCH]:
        uid = f"trash:{tid}"
        try:
            row = dbmod.one(conn, "SELECT * FROM trash_items WHERE id = ?", (int(tid),))
            if row is None:
                raise NotFoundError(f"Trash entry {tid} does not exist.")
            original = str(row["original_path"])
            trash_path = str(row["trash_path"])
            if not os.path.exists(long_path(trash_path)):
                raise NotFoundError("The trashed item is no longer on disk.")
            _validate_path(os.path.dirname(original))
            # Shape-check the payload first: nothing moves unless it can restore.
            plan = plan_restore(row)
            target = original
            if os.path.exists(long_path(target)):
                if on_conflict == "keep_both":
                    target = _unique_target(target)
                elif on_conflict != "overwrite":
                    raise ConflictError("The original path is occupied.",
                                        details={"path": original})
            os.makedirs(os.path.dirname(long_path(target)), exist_ok=True)
            shutil.move(long_path(trash_path), long_path(target))
            slot = os.path.dirname(trash_path)
            restored_name = os.path.basename(trash_path)
            for name in os.listdir(long_path(slot)):
                if name in ("meta.json", restored_name):
                    continue
                try:
                    shutil.move(long_path(os.path.join(slot, name)),
                                long_path(os.path.join(os.path.dirname(target), name)))
                except OSError:
                    continue
            _purge_slot(slot)
            restored_uid = _restore_row(plan, target)

            def _op(conn: sqlite3.Connection, tid=tid) -> None:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM trash_items WHERE id = ?", (int(tid),))
                conn.commit()

            dbmod.writer().run(_op)
            results.append(OpResult(uid, True, details={"path": target,
                                                        "uid_restored": restored_uid}))
        except (ValidationError, NotFoundError, ConflictError, PathNotAllowed) as exc:
            results.append(OpResult(uid, False, exc.code, exc.message, exc.details))
        except OSError as exc:
            results.append(OpResult(uid, False, errors.classify_os_error(exc),
                                    _os_message(exc)))
    return results


def _table_columns(table: str) -> set[str]:
    """Real column set of a destination table, straight from the live schema."""
    if table not in _RESTORABLE_TABLES:
        return set()
    conn = dbmod.get_ro()
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _root_path(root_id) -> str | None:
    if root_id is None:
        return None
    row = dbmod.one(dbmod.get_ro(), "SELECT path FROM roots WHERE id = ?",
                    (int(root_id),))
    return str(row["path"]) if row else None


def _identity_fields(kind: str, target: str, root_path: str | None) -> dict:
    """Identity columns recomputed from where the file actually landed.

    A restore can land on a suffixed path (``keep_both``), so path, size, mtime
    and fingerprint are re-derived rather than trusted from the stored payload.
    """
    abs_path = str(normalize(target))
    filename = os.path.basename(abs_path)
    stem, ext = os.path.splitext(filename)
    try:
        st = os.stat(long_path(abs_path))
        size = int(st.st_size)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        created_ms = int(getattr(st, "st_birthtime", st.st_ctime) * 1000)
    except OSError:
        size, mtime_ns, created_ms = 0, 0, dbmod.now_ms()
    now = dbmod.now_ms()

    if kind == "model":
        # `models` is the logical record and carries no path columns at all.
        return {"name": stem, "updated_at": now, "missing_since": None}

    if kind == "node_package":
        # A package is a directory: no rel_path/size/mtime_ns columns exist.
        pkg_size, pkg_files = (directory_size(abs_path), directory_files(abs_path))
        return {
            "abs_path": abs_path,
            "path_key": path_key(abs_path),
            "folder_name": filename,
            "total_size": pkg_size,
            "file_count": pkg_files,
            "updated_at": now,
            "missing_since": None,
        }

    rel = safe_relpath(abs_path, root_path) if root_path else filename
    fields = {
        "abs_path": abs_path,
        "path_key": path_key(abs_path),
        "rel_path": rel,
        "folder": os.path.dirname(rel).replace(os.sep, "/"),
        "size": size,
        "mtime_ns": mtime_ns,
        "fingerprint": file_fingerprint(abs_path, size, mtime_ns),
        "missing_since": None,
    }
    if kind == "model_file":
        fields.update({"filename": filename, "stem": stem, "ext": ext,
                       "last_seen_at": now})
        return fields
    fields["updated_at"] = now
    if kind == "workflow":
        fields["name"] = stem
    else:
        fields.update({"filename": filename, "ext": ext,
                       "created_at_file": created_ms})
    return fields


def _prepare_row(kind: str, table: str, payload_row: dict, target: str,
                 root_path: str | None) -> tuple[dict, int | None]:
    """Validate a trash payload against the destination table and build the row.

    The payload is data written by a possibly older build, so its key set can
    drift from the live schema.  Checking it against ``PRAGMA table_info`` and
    naming the offending keys turns a silently-failing INSERT - which is exactly
    how workflow restores broke - into an actionable error.
    """
    columns = _table_columns(table)
    if not columns:
        raise ValidationError(f"Unknown restore destination table '{table}'.")

    row = {k: v for k, v in payload_row.items() if not str(k).startswith("_")}
    original_id = row.pop("id", None)

    unknown = sorted(set(row) - columns)
    if unknown:
        log.error("trash payload for %s carries columns absent from %s: %s",
                  kind, table, unknown)
        raise ValidationError(
            f"This trash entry was written against a different {table} schema and "
            f"cannot be restored: unknown column(s) {', '.join(unknown)}.",
            details={"table": table, "unknown_columns": unknown,
                     "hint": "Restore the file, then re-scan to re-index it."},
        )

    identity = _identity_fields(kind, target, root_path)
    stale = sorted(set(identity) - columns)
    if stale:
        # An identity field we generate that the table lacks is a code bug, not
        # bad data - precisely the defect this guard exists to catch.
        log.error("restore identity fields %s do not exist on %s", stale, table)
        raise ValidationError(
            f"Internal restore error: table {table} has no column(s) "
            f"{', '.join(stale)}.",
            details={"table": table, "unknown_columns": stale},
        )
    row.update(identity)

    try:
        original_id = int(original_id) if original_id is not None else None
    except (TypeError, ValueError):
        original_id = None
    return row, original_id


def _bind_col(column: str, value):
    if column.endswith("_json"):
        return dbmod.bind(value, kind="json")
    if isinstance(value, str):
        return dbmod.bind(value)
    if isinstance(value, float):
        return dbmod.bind(value, kind="real")
    if isinstance(value, (bool, int)):
        return dbmod.bind(value, kind="int")
    return dbmod.bind(value)


def _insert_restored(table: str, row: dict, original_id: int | None) -> int | None:
    """Insert the rebuilt row, reusing the original id while it is still free so
    the restored asset keeps its uid."""
    cols = list(row)
    if not cols:
        return None

    def _op(conn: sqlite3.Connection, touch) -> int:
        use_id = None
        if original_id is not None:
            taken = conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (original_id,)  # noqa: S608
            ).fetchone()
            if taken is None:
                use_id = original_id
        values = [_bind_col(c, row[c]) for c in cols]
        names = list(cols)
        if use_id is not None:
            names.insert(0, "id")
            values.insert(0, use_id)
        placeholders = ",".join("?" * len(names))
        cur = conn.execute(
            f"INSERT INTO {table}({','.join(names)}) VALUES ({placeholders})",  # noqa: S608
            values,
        )
        new_id = int(cur.lastrowid)
        kind = _KIND_FOR_TABLE.get(table)
        if kind is not None:
            touch(f"{kind}:{new_id}")
        return new_id

    # A restored asset must be searchable again at once, not after a rescan.
    return sync.write_synced(_op)


def _tags_of(uid: str) -> list[str]:
    try:
        return [str(r["name"]) for r in dbmod.rows(
            dbmod.get_ro(),
            "SELECT t.name FROM asset_tags a JOIN tags t ON t.id = a.tag_id "
            "WHERE a.uid = ? ORDER BY t.name", (str(uid),))]
    except sqlite3.DatabaseError:
        return []


def _albums_of(uid: str) -> list[int]:
    try:
        return [int(r["album_id"]) for r in dbmod.rows(
            dbmod.get_ro(), "SELECT album_id FROM album_items WHERE uid = ?",
            (str(uid),))]
    except sqlite3.DatabaseError:
        return []


def _restore_metadata(uid: str, tags, albums) -> None:
    """Put tag and album membership back onto the restored uid."""
    now = dbmod.now_ms()
    names = [str(t) for t in (tags or []) if str(t).strip()]
    ids = []
    for a in albums or []:
        try:
            ids.append(int(a))
        except (TypeError, ValueError):
            continue
    if not names and not ids:
        return

    def _op(conn: sqlite3.Connection, _touch) -> None:
        for name in names:
            conn.execute(
                "INSERT INTO tags(name,name_key,source,created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(name_key) DO NOTHING",
                (name, name.lower(), "user", now))
            conn.execute(
                "INSERT OR IGNORE INTO asset_tags(uid,tag_id,added_at) "
                "SELECT ?, id, ? FROM tags WHERE name_key = ?",
                (uid, now, name.lower()))
        for album_id in ids:
            conn.execute(
                "INSERT OR IGNORE INTO album_items(album_id,uid,added_at) "
                "SELECT id, ?, ? FROM albums WHERE id = ?", (uid, now, album_id))

    with contextlib.suppress(BaseException):
        sync.write_synced(_op, [uid])


def plan_restore(trash_row) -> dict | None:
    """Validate a trash payload BEFORE any file is moved.

    Checking the shape up front means a payload that cannot rebuild its row
    never leaves the vault half-restored: file back on disk, row missing.
    """
    kind = str(trash_row["kind"])
    if kind not in UID_TABLES:
        return None
    try:
        payload = json.loads(trash_row["payload_json"] or "{}")
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        return None
    row = payload.get("row")
    if not isinstance(row, dict) or not row:
        return None
    table = "models" if kind == "model" else UID_TABLES[kind][0]
    file_row = payload.get("file_row")
    root_path = _root_path(trash_row["root_id"])
    # Probe against the original path: this exercises every table the restore
    # will touch, including the identity columns, before anything moves.
    probe = str(trash_row["original_path"])
    _prepare_row(kind, table, dict(row), probe, root_path)
    if kind == "model" and isinstance(file_row, dict) and file_row:
        _prepare_row("model_file", "model_files", dict(file_row), probe, root_path)
    classes = payload.get("node_classes") or []
    if kind == "node_package" and classes:
        columns = _table_columns("node_classes")
        stray = sorted({k for c in classes if isinstance(c, dict) for k in c}
                       - columns - {"id"})
        if stray:
            log.error("node_classes payload carries unknown columns: %s", stray)
            raise ValidationError(
                "This trash entry stores node classes with unknown column(s) "
                f"{', '.join(stray)} and cannot be restored.",
                details={"table": "node_classes", "unknown_columns": stray},
            )
    return {
        "kind": kind, "table": table, "row": row, "file_row": file_row,
        "root_path": root_path, "tags": payload.get("tags") or [],
        "albums": payload.get("albums") or [], "node_classes": classes,
    }


def _restore_row(plan: dict | None, target: str) -> str | None:
    """Rebuild the deleted DB row from ``payload_json`` (DATA_MODEL 11)."""
    if not plan:
        return None
    kind = plan["kind"]
    row, original_id = _prepare_row(kind, plan["table"], dict(plan["row"]), target,
                                    plan["root_path"])
    new_id = _insert_restored(plan["table"], row, original_id)
    if new_id is None:
        return None
    uid = f"{kind}:{new_id}"
    if kind == "model":
        _reattach_model_file(int(new_id), target, plan)
    if kind == "node_package":
        _reattach_node_classes(int(new_id), plan)
    _restore_metadata(uid, plan.get("tags"), plan.get("albums"))
    # The parent row was indexed before its children existed (a model's document
    # needs primary_file_id to carry the filename), so index the final state.
    sync.resync([uid])
    return uid


def _reattach_model_file(model_id: int, target: str, plan: dict) -> None:
    """A restored model needs its physical model_files row back as well."""
    file_row = plan.get("file_row")
    if not isinstance(file_row, dict) or not file_row:
        return
    row, original_id = _prepare_row("model_file", "model_files", dict(file_row),
                                    target, plan["root_path"])
    row["model_id"] = model_id
    file_id = _insert_restored("model_files", row, original_id)
    if file_id is None:
        return

    def _op(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE models SET primary_file_id=?, missing_since=NULL, "
            "file_count=(SELECT COUNT(*) FROM model_files WHERE model_id=?), "
            "total_size=(SELECT COALESCE(SUM(size),0) FROM model_files WHERE model_id=?) "
            "WHERE id=?", (file_id, model_id, model_id, model_id))
        conn.commit()

    with contextlib.suppress(BaseException):
        dbmod.writer().run(_op)


def trash_empty(ids: list[int] | None = None, older_than_days: int | None = None,
                confirm: bool = False) -> dict:
    if not confirm:
        raise ValidationError("Emptying the trash requires confirm=true.")
    conn = dbmod.get_ro()
    where = "1=1"
    args: tuple = ()
    if ids:
        ph = ",".join("?" * len(ids))
        where = f"id IN ({ph})"
        args = tuple(int(i) for i in ids)
    elif older_than_days is not None:
        where = "deleted_at < ?"
        args = (dbmod.now_ms() - int(older_than_days) * 86_400_000,)
    rows = dbmod.rows(conn, f"SELECT id, trash_path FROM trash_items WHERE {where}",  # noqa: S608
                      args)
    removed = 0
    for r in rows:
        slot = os.path.dirname(str(r["trash_path"]))
        if _purge_slot(slot, force=True):
            removed += 1

    def _op(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(f"DELETE FROM trash_items WHERE {where}", args)  # noqa: S608
        n = cur.rowcount or 0
        conn.commit()
        return n

    return {"removed": int(dbmod.writer().run(_op)), "files_removed": removed}


def _purge_slot(slot: str, force: bool = False) -> bool:
    try:
        if not os.path.isdir(long_path(slot)):
            return False
        entries = os.listdir(long_path(slot))
        if force or not [e for e in entries if e != "meta.json"]:
            shutil.rmtree(long_path(slot), ignore_errors=True)
            return True
    except OSError:
        return False
    return False


def purge_expired() -> int:
    """Startup sweep: drop trash entries past their retention window."""
    conn = dbmod.get_ro()
    rows = dbmod.rows(conn, "SELECT id FROM trash_items WHERE purge_after < ?",
                      (dbmod.now_ms(),))
    if not rows:
        return 0
    return int(trash_empty([int(r["id"]) for r in rows], confirm=True)["removed"])


def create_folder(root_id: int, folder: str) -> dict:
    conn = dbmod.get_ro()
    root = dbmod.one(conn, "SELECT * FROM roots WHERE id = ?", (int(root_id),))
    if root is None:
        raise NotFoundError(f"Root {root_id} does not exist.")
    clean = str(folder or "").replace("\\", "/").strip("/")
    if not clean:
        raise ValidationError("Folder name may not be empty.")
    for part in clean.split("/"):
        validate_filename(part)
    target = Path(str(root["path"])) / clean
    if not is_contained(target, str(root["path"])):
        raise PathNotAllowed("Folder escapes its root.", details={"folder": folder})
    if target.exists():
        raise ConflictError("That folder already exists.", details={"path": str(target)})
    try:
        target.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise PathNotAllowed(f"Could not create the folder: {exc}") from exc
    return {"root_id": int(root_id), "folder": clean, "abs_path": str(target)}


def resolve_file(uid: str, conn: sqlite3.Connection | None = None) -> dict:
    """Path resolution for the raw/download endpoints - always root-validated."""
    info = _resolve(uid, conn)
    path, root = _validate_path(info["abs_path"])
    if not os.path.isfile(long_path(str(path))):
        raise NotFoundError(f"The file for {uid} is no longer on disk.",
                            details={"path": str(path)})
    row = info["row"]
    return {
        "uid": uid, "kind": info["kind"], "path": str(path), "root_id": root.id,
        "filename": row.get("filename") or os.path.basename(str(path)),
        "size": row.get("size"), "mime": row.get("mime"),
        "fingerprint": row.get("fingerprint"),
    }


def directory_size(path: str) -> int:
    """True recursive byte size of a directory - nothing pruned."""
    total = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    total += int(e.stat(follow_symlinks=False).st_size)
            except OSError:
                continue
    return total


def directory_files(path: str) -> int:
    count = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    count += 1
            except OSError:
                continue
    return count


def git_dirty_warning(path: str) -> str | None:
    """Best-effort 'uncommitted changes' hint, read from .git as files only.

    No subprocess: compare tracked working files against the mtime of
    ``.git/index``, which git rewrites on every stage/commit.  Deliberately a
    hint, not a verdict - it warns before a destructive delete, nothing more.
    """
    gitdir = os.path.join(str(path), ".git")
    try:
        if os.path.isfile(gitdir):  # worktree pointer
            return None
        index = os.path.join(gitdir, "index")
        if not os.path.isfile(long_path(index)):
            return None
        index_mtime = os.stat(long_path(index)).st_mtime
    except OSError:
        return None

    newer: list[str] = []
    stack = [str(path)]
    while stack and len(newer) < 5:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in (".git", "__pycache__", "node_modules"):
                        continue
                    stack.append(e.path)
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                if e.name.endswith((".pyc", ".log", ".tmp")):
                    continue
                if e.stat(follow_symlinks=False).st_mtime > index_mtime + 1:
                    newer.append(os.path.basename(e.path))
                    if len(newer) >= 5:
                        break
            except OSError:
                continue
    if not newer:
        return None
    return ("This looks like a git checkout with uncommitted changes "
            f"({', '.join(newer[:3])}"
            f"{' and more' if len(newer) > 3 else ''}). "
            "Those edits are preserved in the trash copy.")


def _reattach_node_classes(package_id: int, plan: dict) -> None:
    """node_classes are CASCADE-deleted with their package; put them back."""
    classes = plan.get("node_classes") or []
    if not classes:
        return
    columns = _table_columns("node_classes")
    rows = []
    for entry in classes:
        if not isinstance(entry, dict):
            continue
        row = {k: v for k, v in entry.items() if k in columns and k != "id"}
        if not row.get("node_id"):
            continue
        row["package_id"] = package_id
        rows.append(row)
    if not rows:
        return
    cols = sorted({k for r in rows for k in r})
    placeholders = ",".join("?" * len(cols))

    def _op(conn: sqlite3.Connection, touch) -> int:
        written = 0
        for row in rows:
            values = [_bind_col(c, row.get(c)) for c in cols]
            try:
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO node_classes({','.join(cols)}) "  # noqa: S608
                    f"VALUES ({placeholders})", values)
            except sqlite3.DatabaseError:
                continue
            if cur.lastrowid:
                touch(f"node_class:{int(cur.lastrowid)}")
            written += 1
        conn.execute("UPDATE node_packages SET class_count = (SELECT COUNT(*) "
                     "FROM node_classes WHERE package_id = ?) WHERE id = ?",
                     (package_id, package_id))
        return written

    with contextlib.suppress(BaseException):
        sync.write_synced(_op, [f"node_package:{package_id}"])


def _os_message(exc: OSError) -> str:
    code = errors.classify_os_error(exc)
    if code == errors.FILE_LOCKED:
        return ("The file is in use by another program - ComfyUI is the usual holder. "
                f"({exc})")
    return str(exc)[:400]
