"""Albums: the left-rail tree, system albums, and membership (API_CONTRACT 12)."""

from __future__ import annotations

import json
import sqlite3
import time

from ...core import db as dbmod
from ...core.errors import ConflictError, NotFoundError, ValidationError
from ...search import sync
from . import ListResult, clamp_page, meta_dict, page_dict

SCOPES = ("models", "nodes", "workflows", "outputs", "all")
KINDS = ("folder", "smart", "manual", "system")

SYSTEM_ALBUMS: tuple[tuple[str, str, str, dict], ...] = (
    ("All", "all", "layers", {}),
    ("Recently added", "all", "clock", {"sort": "-created", "limit": 200}),
    ("Favorites", "all", "star", {"favorite": True}),
    ("Needs hashing", "models", "hash", {"hash_state": ["unhashed", "failed", "stale"]}),
    ("Updates available", "all", "arrow-up", {"has_update": True}),
    # This is deliberately distinct from workflow ``missing_only``, which
    # means unresolved workflow dependencies rather than an absent file.
    ("Missing files", "all", "alert", {"missing_files_only": True}),
    ("Integrity issues", "models", "shield", {"integrity_not_ok": True}),
    ("Unused models", "models", "box", {"unused": True}),
    ("Broken workflows", "workflows", "unlink", {"runnable": False}),
    ("Untagged", "all", "tag", {"untagged": True}),
)


def ensure_system_albums(conn: sqlite3.Connection | None = None) -> int:
    now = dbmod.now_ms()
    rows = [(name, "system", scope, icon, json.dumps(query), i, now, now)
            for i, (name, scope, icon, query) in enumerate(SYSTEM_ALBUMS)]

    def _op(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        # Conflict target is the COALESCE index: (parent_id, scope, name) never
        # fires for root albums because NULL <> NULL in SQLite.
        conn.executemany(
            "INSERT INTO albums(name,kind,scope,icon,query_json,sort_order,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(COALESCE(parent_id,0),scope,name) DO UPDATE SET "
            "icon=excluded.icon, query_json=excluded.query_json, "
            "sort_order=excluded.sort_order, updated_at=excluded.updated_at", rows,
        )
        conn.commit()
        return len(rows)

    return int(dbmod.writer().run(_op))


def _counts(conn: sqlite3.Connection) -> dict[int, int]:
    out: dict[int, int] = {}
    for r in dbmod.rows(conn, "SELECT album_id, COUNT(*) n FROM album_items "
                              "GROUP BY album_id"):
        out[int(r["album_id"])] = int(r["n"])
    for r in dbmod.rows(conn, "SELECT album_id, COUNT(*) n FROM outputs "
                              "WHERE album_id IS NOT NULL GROUP BY album_id"):
        aid = int(r["album_id"])
        out[aid] = out.get(aid, 0) + int(r["n"])
    return out


def _item(row: sqlite3.Row, count: int) -> dict:
    return {
        "id": int(row["id"]), "uid": f"album:{row['id']}", "name": row["name"],
        "kind": row["kind"], "scope": row["scope"], "icon": row["icon"],
        "color": row["color"], "parent_id": row["parent_id"],
        "sort_order": int(row["sort_order"] or 0),
        "item_count": count,
        "query": json.loads(row["query_json"]) if row["query_json"] else None,
    }


def list_albums(scope: str | None = None, limit: int = 200, offset: int = 0,
                conn: sqlite3.Connection | None = None) -> ListResult:
    t0 = time.perf_counter()
    conn = conn or dbmod.get_ro()
    limit, offset = clamp_page(limit, offset)
    where = "1=1"
    args: tuple = ()
    if scope and scope in SCOPES:
        where = "(scope = ? OR scope = 'all')"
        args = (scope,)
    total = int(dbmod.scalar(conn, f"SELECT COUNT(*) FROM albums WHERE {where}",  # noqa: S608
                             args) or 0)
    rows = dbmod.rows(
        conn, f"SELECT * FROM albums WHERE {where} ORDER BY kind='system' DESC, "  # noqa: S608
              "sort_order, name COLLATE NOCASE LIMIT ? OFFSET ?", (*args, limit, offset))
    counts = _counts(conn)
    items = [_item(r, counts.get(int(r["id"]), 0)) for r in rows]
    return ListResult(items=items, page=page_dict(limit, offset, total, len(items)),
                      meta=meta_dict(t0))


def album_tree(scope: str | None = None,
               conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or dbmod.get_ro()
    result = list_albums(scope, limit=500, conn=conn)
    by_parent: dict[int | None, list[dict]] = {}
    for item in result.items:
        by_parent.setdefault(item["parent_id"], []).append(item)

    def build(parent: int | None, depth: int = 0) -> list[dict]:
        if depth > 6:
            return []
        out = []
        for node in by_parent.get(parent, []):
            node = dict(node)
            node["children"] = build(node["id"], depth + 1)
            out.append(node)
        return out

    return {"scope": scope or "all", "nodes": build(None)}


def get_album(album_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, "SELECT * FROM albums WHERE id = ?", (int(album_id),))
    if row is None:
        return None
    return _item(row, _counts(conn).get(int(album_id), 0))


def create_album(name: str, scope: str = "all", kind: str = "manual",
                 parent_id: int | None = None, icon: str | None = None,
                 color: str | None = None, query: dict | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Album name may not be empty.")
    if scope not in SCOPES:
        raise ValidationError(f"Unknown scope '{scope}'.", details={"allowed": list(SCOPES)})
    if kind not in KINDS or kind == "system":
        raise ValidationError("Albums may be created as 'manual', 'smart', or 'folder'.")
    now = dbmod.now_ms()

    def _op(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM albums WHERE name = ? AND scope = ? AND "
            "((parent_id IS NULL AND ? IS NULL) OR parent_id = ?)",
            (name, scope, parent_id, parent_id)).fetchone()
        if existing is not None:
            conn.commit()
            raise ConflictError(f"An album named '{name}' already exists here.",
                                details={"id": int(existing["id"])})
        cur = conn.execute(
            "INSERT INTO albums(parent_id,name,kind,scope,icon,color,query_json,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (parent_id, name, kind, scope, icon, color,
             json.dumps(query) if query else None, now, now))
        conn.commit()
        return int(cur.lastrowid)

    album_id = int(dbmod.writer().run(_op))
    return get_album(album_id) or {"id": album_id}


def update_album(album_id: int, patch: dict) -> dict:
    fields = {k: v for k, v in (patch or {}).items()
              if k in ("name", "icon", "color", "sort_order", "parent_id", "query_json")}
    if "query" in (patch or {}):
        fields["query_json"] = json.dumps(patch["query"])
    if not fields:
        raise ValidationError("Nothing to update.")
    cols = ", ".join(f"{k} = ?" for k in fields)
    def _kind(k: str) -> str:
        if k.endswith("_json"):
            return "json"
        return "int" if k in ("parent_id", "sort_order") else "text"
    vals = [dbmod.bind(v, kind=_kind(k)) for k, v in fields.items()]

    def _op(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            f"UPDATE albums SET {cols}, updated_at = ? WHERE id = ? AND kind <> 'system'",  # noqa: S608
            (*vals, dbmod.now_ms(), int(album_id)))
        n = cur.rowcount or 0
        conn.commit()
        return n

    if int(dbmod.writer().run(_op)) == 0:
        raise NotFoundError(f"Album {album_id} does not exist or is a system album.")
    return get_album(album_id) or {}


def delete_album(album_id: int) -> dict:
    def _op(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM albums WHERE id = ? AND kind <> 'system'",
                           (int(album_id),))
        n = cur.rowcount or 0
        conn.commit()
        return n

    if int(dbmod.writer().run(_op)) == 0:
        raise NotFoundError(f"Album {album_id} does not exist or is a system album.")
    return {"deleted": True, "id": int(album_id)}


def set_album_items(album_id: int, uids: list[str], mode: str = "add") -> dict:
    uids = [str(u) for u in (uids or []) if u]
    if not uids:
        return {"changed": 0}
    now = dbmod.now_ms()

    def _op(conn: sqlite3.Connection, _touch) -> int:
        if conn.execute("SELECT 1 FROM albums WHERE id = ?",
                        (int(album_id),)).fetchone() is None:
            raise NotFoundError(f"Album {album_id} does not exist.")
        if mode == "remove":
            conn.executemany("DELETE FROM album_items WHERE album_id = ? AND uid = ?",
                             [(int(album_id), u) for u in uids])
        else:
            conn.executemany(
                "INSERT OR IGNORE INTO album_items(album_id,uid,added_at) VALUES (?,?,?)",
                [(int(album_id), u, now) for u in uids])
        conn.execute("UPDATE albums SET item_count = (SELECT COUNT(*) FROM album_items "
                     "WHERE album_id = ?), updated_at = ? WHERE id = ?",
                     (int(album_id), now, int(album_id)))
        return len(uids)

    return {"changed": int(sync.write_synced(_op, uids)), "album_id": int(album_id)}


def refresh_counts() -> int:
    def _op(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE albums SET item_count = COALESCE((SELECT COUNT(*) "
                     "FROM album_items ai WHERE ai.album_id = albums.id), 0)")
        conn.commit()
        return 1

    return int(dbmod.writer().run(_op))
