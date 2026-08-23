"""Tags and per-asset user metadata patches (API_CONTRACT 12)."""

from __future__ import annotations

import sqlite3
import time

from ...core import db as dbmod
from ...core.errors import NotFoundError, ValidationError
from ...search import sync
from . import ListResult, clamp_page, meta_dict, page_dict

SOURCES = ("user", "civitai", "derived", "ollama")
UID_TABLES = {
    "model": "models", "node_package": "node_packages", "node_class": "node_classes",
    "workflow": "workflows", "output": "outputs",
}
PATCHABLE = {
    "models": ("favorite", "user_rating", "user_notes", "color_label"),
    "outputs": ("favorite", "user_rating", "user_notes", "color_label", "album_id"),
    "workflows": ("description", "description_source"),
    "node_packages": (),
    "node_classes": (),
}


def parse_uid(uid: str) -> tuple[str, int]:
    kind, _sep, num = str(uid).partition(":")
    if kind not in UID_TABLES:
        raise ValidationError(f"Unknown uid kind '{kind}'.",
                              details={"allowed": sorted(UID_TABLES)})
    try:
        return kind, int(num)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Malformed uid '{uid}'.") from exc


def list_tags(q: str | None = None, limit: int = 200, offset: int = 0,
              conn: sqlite3.Connection | None = None) -> ListResult:
    t0 = time.perf_counter()
    conn = conn or dbmod.get_ro()
    limit, offset = clamp_page(limit, offset)
    where, args = "1=1", ()
    if q:
        where = "name_key LIKE ?"
        args = (f"%{str(q).lower()}%",)
    total = int(dbmod.scalar(conn, f"SELECT COUNT(*) FROM tags WHERE {where}",  # noqa: S608
                             args) or 0)
    rows = dbmod.rows(
        conn,
        f"SELECT t.*, (SELECT COUNT(*) FROM asset_tags a WHERE a.tag_id = t.id) uses "  # noqa: S608
        f"FROM tags t WHERE {where} ORDER BY uses DESC, name COLLATE NOCASE "
        "LIMIT ? OFFSET ?", (*args, limit, offset))
    items = [{"id": int(r["id"]), "name": r["name"], "color": r["color"],
              "source": r["source"], "count": int(r["uses"] or 0)} for r in rows]
    return ListResult(items=items, page=page_dict(limit, offset, total, len(items)),
                      meta=meta_dict(t0))


def ensure_tags(names: list[str], source: str = "user") -> dict[str, int]:
    clean = []
    for name in names or []:
        text = str(name).strip()
        if text and len(text) <= 120:
            clean.append(text)
    if not clean:
        return {}
    if source not in SOURCES:
        source = "user"
    now = dbmod.now_ms()

    def _op(conn: sqlite3.Connection) -> dict[str, int]:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO tags(name,name_key,source,created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(name_key) DO NOTHING",
            [(t, t.lower(), source, now) for t in clean])
        out: dict[str, int] = {}
        ph = ",".join("?" * len(clean))
        for r in conn.execute(
            f"SELECT id, name, name_key FROM tags WHERE name_key IN ({ph})",  # noqa: S608
            [t.lower() for t in clean],
        ):
            out[str(r["name_key"])] = int(r["id"])
        conn.commit()
        return out

    return dbmod.writer().run(_op)


def assign_tags(uids: list[str], add: list[str] | None = None,
                remove: list[str] | None = None) -> dict:
    uids = [str(u) for u in (uids or []) if u]
    if not uids:
        return {"updated": 0}
    for uid in uids:
        parse_uid(uid)
    add_ids = ensure_tags(add or [])
    remove_keys = [str(t).strip().lower() for t in (remove or []) if str(t).strip()]
    now = dbmod.now_ms()

    def _op(conn: sqlite3.Connection, _touch) -> int:
        n = 0
        if add_ids:
            pairs = [(uid, tag_id, now) for uid in uids for tag_id in add_ids.values()]
            conn.executemany(
                "INSERT OR IGNORE INTO asset_tags(uid,tag_id,added_at) VALUES (?,?,?)",
                pairs)
            n += len(pairs)
        if remove_keys:
            ph = ",".join("?" * len(remove_keys))
            for uid in uids:
                conn.execute(
                    f"DELETE FROM asset_tags WHERE uid = ? AND tag_id IN "  # noqa: S608
                    f"(SELECT id FROM tags WHERE name_key IN ({ph}))",
                    (uid, *remove_keys))
                n += 1
        conn.execute("UPDATE tags SET use_count = COALESCE((SELECT COUNT(*) FROM "
                     "asset_tags a WHERE a.tag_id = tags.id), 0)")
        return n

    return {"updated": int(sync.write_synced(_op, uids)), "uids": uids}


def tags_of(uid: str, conn: sqlite3.Connection | None = None) -> list[str]:
    conn = conn or dbmod.get_ro()
    return [str(r["name"]) for r in dbmod.rows(
        conn, "SELECT t.name FROM asset_tags a JOIN tags t ON t.id = a.tag_id "
              "WHERE a.uid = ? ORDER BY t.name", (str(uid),))]


def patch_asset(uid: str, patch: dict) -> dict:
    """Apply a user-metadata patch (favorite/rating/notes/tags/album)."""
    kind, row_id = parse_uid(uid)
    table = UID_TABLES[kind]
    allowed = PATCHABLE.get(table, ())
    fields = {k: v for k, v in (patch or {}).items() if k in allowed}
    tags = patch.get("tags") if isinstance(patch, dict) else None

    if fields:
        if "user_rating" in fields and fields["user_rating"] is not None:
            try:
                rating = int(fields["user_rating"])
            except (TypeError, ValueError) as exc:
                raise ValidationError("user_rating must be an integer 0-5.") from exc
            if not 0 <= rating <= 5:
                raise ValidationError("user_rating must be between 0 and 5.")
            fields["user_rating"] = rating
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = [dbmod.bind(v, kind="int" if k in ("user_rating", "album_id", "favorite")
                           else "text") for k, v in fields.items()]

        def _op(conn: sqlite3.Connection, _touch) -> int:
            cur = conn.execute(
                f"UPDATE {table} SET {cols}, updated_at = ? WHERE id = ?",  # noqa: S608
                (*vals, dbmod.now_ms(), row_id))
            return cur.rowcount or 0

        if int(sync.write_synced(_op, [uid])) == 0:
            raise NotFoundError(f"{uid} does not exist.")

    if isinstance(tags, list):
        current = set(tags_of(uid))
        wanted = {str(t).strip() for t in tags if str(t).strip()}
        assign_tags([uid], add=sorted(wanted - current),
                    remove=sorted(current - wanted))
    return {"uid": uid, "updated": True}


def bulk_patch(uids: list[str], patch: dict) -> dict:
    results = []
    updated = 0
    for uid in uids or []:
        try:
            patch_asset(uid, patch)
            results.append({"uid": uid, "ok": True})
            updated += 1
        except (ValidationError, NotFoundError) as exc:
            results.append({"uid": uid, "ok": False, "error": exc.code,
                            "message": exc.message})
        except sqlite3.DatabaseError as exc:
            results.append({"uid": uid, "ok": False, "error": "INTERNAL_ERROR",
                            "message": str(exc)[:200]})
    return {"updated": updated, "results": results}
