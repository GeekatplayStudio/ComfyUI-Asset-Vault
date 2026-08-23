"""Output list / detail / graph - contract-shaped (API_CONTRACT 6)."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from ...core import db as dbmod
from . import (
    ListResult,
    Where,
    apply_id_filter,
    clamp_page,
    date_bucket,
    download_url,
    first_letter,
    json_obj,
    meta_dict,
    order_by_search,
    page_dict,
    parse_sort,
    raw_url,
    search_uids,
    tags_for,
    thumb_url,
)

SORTS = {
    "created": "created_at_file", "modified": "mtime_ns",
    "name": "filename COLLATE NOCASE", "size": "size", "rating": "user_rating",
    "width": "width", "height": "height", "duration": "duration_ms", "relevance": "id",
}
GROUPS = {"folder": "folder", "model": "model_name", "media_kind": "media_kind",
          "album": "album_id"}


@dataclass
class OutputFilters:
    q: str | None = None
    smart: bool = False
    folder: str | None = None
    media_kind: list[str] = field(default_factory=list)
    model_id: int | None = None
    workflow_id: int | None = None
    album_id: int | None = None
    root_id: list[int] = field(default_factory=list)
    tag: list[str] = field(default_factory=list)
    favorite: bool | None = None
    min_rating: int | None = None
    has_metadata: bool | None = None
    sampler: list[str] = field(default_factory=list)
    seed: str | None = None
    steps_min: int | None = None
    steps_max: int | None = None
    cfg_min: float | None = None
    cfg_max: float | None = None
    width_min: int | None = None
    width_max: int | None = None
    height_min: int | None = None
    height_max: int | None = None
    size_min: int | None = None
    size_max: int | None = None
    date_from: int | None = None
    date_to: int | None = None
    include_missing: bool = False

    @classmethod
    def from_dict(cls, data: dict | None) -> OutputFilters:
        data = data or {}
        kwargs = {}
        for f in cls.__dataclass_fields__.values():  # type: ignore[attr-defined]
            if f.name not in data:
                continue
            value = data[f.name]
            if f.type.startswith("list") and not isinstance(value, list):
                value = [value] if value not in (None, "") else []
            kwargs[f.name] = value
        return cls(**kwargs)


def _where(f: OutputFilters) -> Where:
    w = Where()
    if not f.include_missing:
        w.add("missing_since IS NULL")
    w.prefix("folder", f.folder)
    w.any_of("media_kind", f.media_kind)
    w.eq("model_id", f.model_id)
    w.eq("workflow_id", f.workflow_id)
    w.eq("album_id", f.album_id)
    w.any_of("root_id", f.root_id)
    w.bool_eq("favorite", f.favorite)
    w.gte("user_rating", f.min_rating)
    w.bool_eq("has_metadata", f.has_metadata)
    w.any_of("sampler", f.sampler)
    w.eq("seed", f.seed)
    w.gte("steps", f.steps_min)
    w.lte("steps", f.steps_max)
    w.gte("cfg", f.cfg_min)
    w.lte("cfg", f.cfg_max)
    w.gte("width", f.width_min)
    w.lte("width", f.width_max)
    w.gte("height", f.height_min)
    w.lte("height", f.height_max)
    w.gte("size", f.size_min)
    w.lte("size", f.size_max)
    w.gte("created_at_file", f.date_from)
    w.lte("created_at_file", f.date_to)
    for tag in f.tag or []:
        w.add("('output:' || id) IN (SELECT at.uid FROM asset_tags at JOIN tags t "
              "ON t.id = at.tag_id WHERE t.name_key = ?)", str(tag).lower())
    return w


def _item(row: sqlite3.Row, tags: list[str]) -> dict:
    uid = f"output:{row['id']}"
    return {
        "uid": uid, "id": int(row["id"]), "filename": row["filename"], "ext": row["ext"],
        "media_kind": row["media_kind"], "mime": row["mime"],
        "width": row["width"], "height": row["height"], "duration_ms": row["duration_ms"],
        "size": int(row["size"] or 0), "created_at": int(row["created_at_file"] or 0),
        "modified_at": int(row["mtime_ns"] or 0) // 1_000_000,
        "folder": row["folder"] or "", "rel_path": row["rel_path"],
        "root_id": row["root_id"],
        "has_metadata": bool(row["has_metadata"]),
        "metadata_format": row["metadata_format"],
        "positive_prompt": row["positive_prompt"],
        "model_name": row["model_name"],
        "model_uid": f"model:{row['model_id']}" if row["model_id"] else None,
        "workflow_uid": f"workflow:{row['workflow_id']}" if row["workflow_id"] else None,
        "seed": row["seed"], "steps": row["steps"], "cfg": row["cfg"],
        "sampler": row["sampler"], "scheduler": row["scheduler"],
        "favorite": bool(row["favorite"]), "user_rating": row["user_rating"],
        "album_id": row["album_id"], "color_label": row["color_label"], "tags": tags,
        "thumbnail_url": thumb_url(uid), "raw_url": raw_url(uid),
        "download_url": download_url(uid),
        "missing": row["missing_since"] is not None,
    }


def list_outputs(filters: OutputFilters | dict | None = None, sort: str = "-created",
                 group: str = "none", limit: int = 100, offset: int = 0,
                 conn: sqlite3.Connection | None = None) -> ListResult:
    t0 = time.perf_counter()
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, OutputFilters) else OutputFilters.from_dict(filters)
    limit, offset = clamp_page(limit, offset)

    ids, search_meta = search_uids(f.q, f.smart, ["output"], conn)
    w = _where(f)
    if not apply_id_filter(w, "id", ids):
        return ListResult(items=[], page=page_dict(limit, offset, 0, 0),
                          meta=meta_dict(t0, sort=sort, **search_meta))
    where_sql, args = w.sql(), w.args()
    total = int(dbmod.scalar(
        conn, f"SELECT COUNT(*) FROM outputs WHERE {where_sql}", args) or 0)  # noqa: S608
    if sort == "relevance":
        order = (order_by_search(ids, "id") or "created_at_file DESC") + ", id DESC"
    else:
        order = parse_sort(sort, SORTS, "-created")
    rows = dbmod.rows(
        conn, f"SELECT * FROM outputs WHERE {where_sql} ORDER BY {order} "  # noqa: S608
              "LIMIT ? OFFSET ?", (*args, limit, offset),
    )
    uids = [f"output:{r['id']}" for r in rows]
    tag_map = tags_for(conn, uids)
    items = [_item(r, tag_map.get(f"output:{r['id']}", [])) for r in rows]

    groups = None
    if group in ("date", "first_letter"):
        buckets: dict[str, int] = {}
        # Date buckets carry their own bounds so the caller can filter by one
        # without re-deriving it from the label.  The labels are a mix of
        # relative ("Today", "This week") and absolute ("June 2026"), and
        # parsing those back into a range in the client would be guesswork that
        # breaks on the first locale or wording change.
        spans: dict[str, tuple[int, int]] = {}
        for r in dbmod.rows(
            conn, f"SELECT created_at_file, filename FROM outputs WHERE {where_sql}",  # noqa: S608
            args,
        ):
            ts = int(r["created_at_file"] or 0)
            key = (first_letter(r["filename"]) if group == "first_letter"
                   else date_bucket(ts))
            buckets[key] = buckets.get(key, 0) + 1
            if group == "date" and ts:
                lo, hi = spans.get(key, (ts, ts))
                spans[key] = (min(lo, ts), max(hi, ts))
        groups = []
        for k, v in sorted(buckets.items()):
            entry = {"key": k, "label": k, "count": v, "offset": 0}
            span = spans.get(k)
            if span:
                # `date_to` is inclusive of the newest item in the bucket.
                entry["date_from"], entry["date_to"] = span[0], span[1]
            groups.append(entry)
    elif group in GROUPS:
        col = GROUPS[group]
        groups = [
            {"key": str(r["k"] if r["k"] is not None else ""),
             "label": str(r["k"] or "Ungrouped"), "count": int(r["n"]), "offset": 0}
            for r in dbmod.rows(
                conn, f"SELECT {col} AS k, COUNT(*) n FROM outputs "  # noqa: S608
                      f"WHERE {where_sql} GROUP BY k ORDER BY n DESC LIMIT 200", args)
        ]
    return ListResult(items=items, page=page_dict(limit, offset, total, len(items)),
                      groups=groups,
                      meta=meta_dict(t0, sort=f"{sort},id", **search_meta))


def get_output(output_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, "SELECT * FROM outputs WHERE id = ?", (int(output_id),))
    if row is None:
        return None
    uid = f"output:{output_id}"
    item = _item(row, tags_for(conn, [uid]).get(uid, []))
    models = dbmod.rows(
        conn, "SELECT ref_name, role, strength, model_id FROM output_models "
              "WHERE output_id = ? ORDER BY role, ref_name", (int(output_id),))
    item.update({
        "negative_prompt": row["negative_prompt"],
        "denoise": row["denoise"],
        "provenance": json_obj(row["provenance_json"]) or {},
        "node_count": row["node_count"],
        "unresolved_inputs": int(row["unresolved_inputs"] or 0),
        "loras": [{"name": r["ref_name"], "strength": r["strength"],
                   "uid": f"model:{r['model_id']}" if r["model_id"] else None}
                  for r in models if r["role"] == "lora"],
        "all_models": [{"name": r["ref_name"], "role": r["role"],
                        "uid": f"model:{r['model_id']}" if r["model_id"] else None}
                       for r in models],
        "workflow_hash": row["workflow_hash"],
        "abs_path": row["abs_path"],
        "color_mode": row["color_mode"], "has_alpha": row["has_alpha"],
        "frame_count": row["frame_count"],
        "graph_available": bool(row["prompt_graph_json"]),
        "user_notes": row["user_notes"],
        "siblings": [
            {"uid": f"output:{r['id']}", "filename": r["filename"],
             "thumbnail_url": thumb_url(f"output:{r['id']}", 160)}
            for r in dbmod.rows(
                conn, "SELECT id, filename FROM outputs WHERE workflow_hash = ? "
                      "AND id <> ? ORDER BY created_at_file DESC LIMIT 12",
                (row["workflow_hash"], int(output_id))) if row["workflow_hash"]
        ],
        "exif": {},
        "actions": {"can_rename": True, "can_move": True, "can_delete": True,
                    "can_extract_workflow": bool(row["prompt_graph_json"])},
    })
    return item


def output_graph(output_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    import json

    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, "SELECT prompt_graph_json, abs_path FROM outputs WHERE id = ?",
                    (int(output_id),))
    if row is None:
        return None
    blob = row["prompt_graph_json"]
    if not blob and row["abs_path"]:
        import os

        from ...parsers import image_meta
        ext = os.path.splitext(str(row["abs_path"]))[1]
        meta = image_meta.read_output(str(row["abs_path"]), ext)
        blob = meta.prompt_graph_json
    if not blob:
        return None
    try:
        return json.loads(blob)
    except ValueError:
        return None


def output_facets(filters: OutputFilters | dict | None = None,
                  conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, OutputFilters) else OutputFilters.from_dict(filters)
    w = _where(f)
    out: dict = {}
    for name, col in (("media_kind", "media_kind"), ("folder", "folder"),
                      ("sampler", "sampler"), ("model", "model_name"),
                      ("metadata_format", "metadata_format")):
        out[name] = [
            {"value": r["v"], "count": int(r["n"])} for r in dbmod.rows(
                conn, f"SELECT {col} AS v, COUNT(*) n FROM outputs "  # noqa: S608
                      f"WHERE {w.sql()} GROUP BY v ORDER BY n DESC LIMIT 80", w.args())
        ]
    agg = dbmod.one(
        conn, "SELECT MIN(size) smin, MAX(size) smax, SUM(size) stot, "  # noqa: S608
              f"MIN(created_at_file) dmin, MAX(created_at_file) dmax FROM outputs "
              f"WHERE {w.sql()}", w.args())
    out["size"] = {"min": agg["smin"] if agg else None, "max": agg["smax"] if agg else None,
                   "total": agg["stot"] if agg else 0}
    out["date"] = {"min": agg["dmin"] if agg else None, "max": agg["dmax"] if agg else None}
    return out


def output_tree(filters: OutputFilters | dict | None = None,
                conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, OutputFilters) else OutputFilters.from_dict(filters)
    w = _where(f)
    rows = dbmod.rows(
        conn, f"SELECT folder, COUNT(*) n, COALESCE(SUM(size),0) b FROM outputs "  # noqa: S608
              f"WHERE {w.sql()} GROUP BY folder ORDER BY folder", w.args())
    nodes: dict[str, dict] = {}
    for r in rows:
        folder = str(r["folder"] or "")
        top = folder.split("/", 1)[0] if folder else ""
        node = nodes.setdefault(top, {"key": top, "label": top or "Root", "count": 0,
                                      "bytes": 0, "children": []})
        node["count"] += int(r["n"])
        node["bytes"] += int(r["b"])
        if folder and folder != top:
            node["children"].append({"key": folder, "label": folder.split("/", 1)[1],
                                     "count": int(r["n"]), "bytes": int(r["b"]),
                                     "children": []})
    return {"group": "folder", "nodes": sorted(nodes.values(), key=lambda n: n["key"])}
