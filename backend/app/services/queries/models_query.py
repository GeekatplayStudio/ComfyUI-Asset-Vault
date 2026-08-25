"""Model list / detail / facets / groups / usage - contract-shaped (API_CONTRACT 3)."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from ...core import db as dbmod
from . import (
    ListResult,
    Where,
    apply_id_filter,
    attach_matches,
    clamp_page,
    date_bucket,
    first_letter,
    format_params,
    json_list,
    json_obj,
    meta_dict,
    order_by_search,
    page_dict,
    parse_sort,
    search_uids,
    tags_for,
    thumb_url,
)

SORTS = {
    "name": "name COLLATE NOCASE", "created": "created_at", "modified": "updated_at",
    "size": "total_size", "category": "category COLLATE NOCASE",
    "base_model": "base_model_family", "role": "model_role",
    "params": "param_count_primary", "rating": "user_rating",
    "hash_state": "hash_state", "relevance": "id",
}
GROUPS = ("none", "category", "base_model", "role", "folder", "precision", "root",
          "hash_state", "integrity", "first_letter", "date")
GROUP_COLUMNS = {
    "category": "category", "base_model": "base_model_family", "role": "model_role",
    "folder": "folder", "precision": "precision", "root": "root_id",
    "hash_state": "hash_state", "integrity": "integrity",
}

FACET_FIELDS = {
    "category": "category", "base_model": "base_model_family", "role": "model_role",
    "precision": "precision", "modality": "modality", "hash_state": "hash_state",
    "integrity": "integrity", "root": "root_id",
}


@dataclass
class ModelFilters:
    q: str | None = None
    smart: bool = False
    category: list[str] = field(default_factory=list)
    base_model: list[str] = field(default_factory=list)
    role: list[str] = field(default_factory=list)
    modality: list[str] = field(default_factory=list)
    precision: list[str] = field(default_factory=list)
    hash_state: list[str] = field(default_factory=list)
    integrity: list[str] = field(default_factory=list)
    root_id: list[int] = field(default_factory=list)
    folder: str | None = None
    album_id: int | None = None
    tag: list[str] = field(default_factory=list)
    favorite: bool | None = None
    min_rating: int | None = None
    has_update: bool | None = None
    is_adapter: bool | None = None
    size_min: int | None = None
    size_max: int | None = None
    date_from: int | None = None
    date_to: int | None = None
    include_missing: bool = False
    missing_files_only: bool | None = None
    integrity_not_ok: bool | None = None
    unused: bool | None = None
    untagged: bool | None = None

    @classmethod
    def from_dict(cls, data: dict | None) -> ModelFilters:
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


def _where(f: ModelFilters, *, skip: str | None = None) -> Where:
    w = Where()
    # ``include_missing`` widens the normal result set; it does not mean
    # "only missing".  System albums need the latter behaviour.
    if f.missing_files_only:
        w.add("missing_since IS NOT NULL")
    elif not f.include_missing:
        w.add("missing_since IS NULL")
    if skip != "category":
        w.any_of("category", f.category)
    if skip != "base_model":
        w.any_of("base_model_family", f.base_model)
    if skip != "role":
        w.any_of("model_role", f.role)
    if skip != "modality":
        w.any_of("modality", f.modality)
    if skip != "precision":
        w.any_of("precision", f.precision)
    if skip != "hash_state":
        w.any_of("hash_state", f.hash_state)
    if skip != "integrity":
        w.any_of("integrity", f.integrity)
    if f.integrity_not_ok:
        w.add("integrity <> 'ok'")
    if skip != "root":
        w.any_of("root_id", f.root_id)
    w.prefix("folder", f.folder)
    w.bool_eq("favorite", f.favorite)
    w.gte("user_rating", f.min_rating)
    w.bool_eq("has_update", f.has_update)
    w.bool_eq("is_adapter", f.is_adapter)
    if f.unused:
        w.add("workflow_count = 0 AND output_count = 0")
    if f.untagged:
        w.add("NOT EXISTS (SELECT 1 FROM asset_tags WHERE uid = ('model:' || id))")
    w.gte("total_size", f.size_min)
    w.lte("total_size", f.size_max)
    w.gte("updated_at", f.date_from)
    w.lte("updated_at", f.date_to)
    if f.album_id is not None:
        w.add("('model:' || id) IN (SELECT uid FROM album_items WHERE album_id = ?)",
              int(f.album_id))
    for tag in f.tag or []:
        w.add("('model:' || id) IN (SELECT at.uid FROM asset_tags at JOIN tags t "
              "ON t.id = at.tag_id WHERE t.name_key = ?)", str(tag).lower())
    return w


def _row_to_item(row: sqlite3.Row, tags: list[str]) -> dict:
    uid = f"model:{row['id']}"
    return {
        "uid": uid, "id": int(row["id"]), "name": row["name"],
        "filename": row["filename"], "ext": row["ext"],
        "category": row["category"], "role": row["model_role"],
        "base_model": {
            "family": row["base_model_family"], "variant": row["base_model_variant"],
            "confidence": round(float(row["arch_confidence"] or 0), 2),
            "source": row["arch_source"],
        },
        "modality": row["modality"], "architecture": row["architecture_label"],
        "precision": row["precision"], "quantization": row["quantization"],
        "params": {
            "primary": row["param_count_primary"], "total": row["param_count_total"],
            "display": format_params(row["param_count_primary"]),
        },
        "is_bundled": bool(row["is_bundled"]), "is_adapter": bool(row["is_adapter"]),
        "size": int(row["total_size"] or 0),
        "modified_at": int(row["mtime_ns"] or 0) // 1_000_000,
        "folder": row["folder"] or "", "root_id": row["root_id"],
        "rel_path": row["rel_path"], "abs_path": row["abs_path"],
        "hash": {"state": row["hash_state"] or "unhashed", "autov2": row["autov2"],
                 "sha256": row["sha256"]},
        "integrity": row["integrity"],
        "civitai": {"state": row["civitai_state"], "model_id": row["civitai_model_id"],
                    "url": row["civitai_url"], "has_update": bool(row["has_update"])},
        "thumbnail_url": thumb_url(uid),
        "has_preview": row["preview_path"] is not None,
        "community": {"rating": row["rating"], "downloads": row["download_count"]},
        "favorite": bool(row["favorite"]), "user_rating": row["user_rating"],
        "color_label": row["color_label"], "tags": tags,
        "counts": {"workflows": int(row["workflow_count"] or 0),
                   "outputs": int(row["output_count"] or 0)},
        "missing": row["missing_since"] is not None,
    }


def list_models(filters: ModelFilters | dict | None = None, sort: str = "name",
                group: str = "none", limit: int = 100, offset: int = 0,
                conn: sqlite3.Connection | None = None) -> ListResult:
    t0 = time.perf_counter()
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, ModelFilters) else ModelFilters.from_dict(filters)
    limit, offset = clamp_page(limit, offset)

    ids, search_meta, matches = search_uids(f.q, f.smart, ["model"], conn)
    w = _where(f)
    if not apply_id_filter(w, "id", ids):
        return ListResult(items=[], page=page_dict(limit, offset, 0, 0),
                          groups=[] if group != "none" else None,
                          meta=meta_dict(t0, sort=sort, **search_meta))

    where_sql, args = w.sql(), w.args()
    total = int(dbmod.scalar(
        conn, f"SELECT COUNT(*) FROM v_model_list WHERE {where_sql}", args) or 0)  # noqa: S608

    if sort == "relevance":
        order = order_by_search(ids, "id") or "name COLLATE NOCASE ASC, id ASC"
        order = f"{order}, id ASC"
    else:
        order = parse_sort(sort, SORTS, "name")
    rows = dbmod.rows(
        conn,
        f"SELECT * FROM v_model_list WHERE {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",  # noqa: S608
        (*args, limit, offset),
    )
    uids = [f"model:{r['id']}" for r in rows]
    tag_map = tags_for(conn, uids)
    items = attach_matches(
        [_row_to_item(r, tag_map.get(f"model:{r['id']}", [])) for r in rows], matches)

    groups = None
    if group and group != "none":
        groups = model_groups(f, group, conn)

    return ListResult(items=items, page=page_dict(limit, offset, total, len(items)),
                      groups=groups,
                      meta=meta_dict(t0, sort=f"{sort},id", **search_meta))


def model_groups(filters: ModelFilters | dict | None, group: str,
                 conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, ModelFilters) else ModelFilters.from_dict(filters)
    w = _where(f)
    where_sql, args = w.sql(), w.args()
    if group in GROUP_COLUMNS:
        col = GROUP_COLUMNS[group]
        rows = dbmod.rows(
            conn,
            f"SELECT {col} AS k, COUNT(*) n, COALESCE(SUM(total_size),0) b "  # noqa: S608
            f"FROM v_model_list WHERE {where_sql} GROUP BY k ORDER BY n DESC", args,
        )
        return [{"key": str(r["k"] if r["k"] is not None else ""),
                 "label": _label(group, r["k"]), "count": int(r["n"]),
                 "bytes": int(r["b"]), "offset": 0} for r in rows]
    rows = dbmod.rows(
        conn,
        f"SELECT name, updated_at, total_size FROM v_model_list WHERE {where_sql}",  # noqa: S608
        args,
    )
    buckets: dict[str, list[int]] = {}
    for r in rows:
        key = (first_letter(r["name"]) if group == "first_letter"
               else date_bucket(r["updated_at"]))
        b = buckets.setdefault(key, [0, 0])
        b[0] += 1
        b[1] += int(r["total_size"] or 0)
    return [{"key": k, "label": k, "count": v[0], "bytes": v[1], "offset": 0}
            for k, v in sorted(buckets.items())]


def _label(group: str, value) -> str:
    if value in (None, ""):
        return "Uncategorized"
    text = str(value)
    if group in ("category", "folder"):
        return text.replace("_", " ").title() if group == "category" else text
    return text


def model_facets(filters: ModelFilters | dict | None = None,
                 conn: sqlite3.Connection | None = None) -> dict:
    """Each facet honours every active filter except its own field."""
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, ModelFilters) else ModelFilters.from_dict(filters)
    out: dict = {}
    for name, column in FACET_FIELDS.items():
        w = _where(f, skip=name)
        rows = dbmod.rows(
            conn,
            f"SELECT {column} AS v, COUNT(*) n FROM v_model_list "  # noqa: S608
            f"WHERE {w.sql()} GROUP BY v ORDER BY n DESC", w.args(),
        )
        out[name] = [{"value": r["v"], "label": _label(name, r["v"]),
                      "count": int(r["n"])} for r in rows]
    w = _where(f)
    agg = dbmod.one(
        conn,
        "SELECT MIN(total_size) smin, MAX(total_size) smax, SUM(total_size) stot, "  # noqa: S608
        f"MIN(updated_at) dmin, MAX(updated_at) dmax FROM v_model_list WHERE {w.sql()}",
        w.args(),
    )
    out["size"] = {"min": agg["smin"] if agg else None, "max": agg["smax"] if agg else None,
                   "total": agg["stot"] if agg else 0}
    out["date"] = {"min": agg["dmin"] if agg else None, "max": agg["dmax"] if agg else None}
    out["tags"] = [
        {"value": r["name"], "count": int(r["n"])} for r in dbmod.rows(
            conn, "SELECT t.name, COUNT(*) n FROM asset_tags at JOIN tags t "
                  "ON t.id = at.tag_id WHERE at.uid LIKE 'model:%' "
                  "GROUP BY t.name ORDER BY n DESC LIMIT 40")
    ]
    return out


def get_model(model_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, "SELECT * FROM v_model_list WHERE id = ?", (int(model_id),))
    if row is None:
        return None
    full = dbmod.one(conn, "SELECT * FROM models WHERE id = ?", (int(model_id),))
    uid = f"model:{model_id}"
    tags = tags_for(conn, [uid]).get(uid, [])
    item = _row_to_item(row, tags)

    components = []
    comps = json_obj(full["components_json"]) or {}
    total = sum(int(v.get("params") or 0) for v in comps.values()) or 1
    for name, spec in sorted(comps.items(), key=lambda kv: -int(kv[1].get("params") or 0)):
        params = int(spec.get("params") or 0)
        components.append({"name": name, "params": params, "dtype": spec.get("dtype"),
                           "share": round(params / total, 3)})

    files = [
        {"id": int(r["id"]), "abs_path": r["abs_path"], "rel_path": r["rel_path"],
         "size": int(r["size"] or 0), "modified_at": int(r["mtime_ns"] or 0) // 1_000_000,
         "hash_state": r["hash_state"], "autov2": r["autov2"], "sha256": r["sha256"],
         "format": r["format"], "root_id": r["root_id"]}
        for r in dbmod.rows(conn, "SELECT * FROM model_files WHERE model_id = ? "
                                  "ORDER BY id", (int(model_id),))
    ]

    hashed = row["hash_state"] == "done"
    usage = model_usage(model_id, limit=5, offset=0, conn=conn)
    item.update({
        "technical": {
            "tensor_count": full["tensor_count"], "format": files[0]["format"] if files else None,
            "header_parsed": bool(files and dbmod.scalar(
                conn, "SELECT header_parsed FROM model_files WHERE id = ?",
                (files[0]["id"],))),
            "components": components,
            "prediction_type": full["prediction_type"],
            "resolution_hint": full["resolution_hint"],
            "detection": {"source": full["arch_source"],
                          "confidence": round(float(full["arch_confidence"] or 0), 2),
                          "signals": json_list(full["detection_signals_json"])},
            "header_metadata": json_obj(full["header_metadata_json"]) or {},
        },
        "build_spec": {
            "trained_by": None, "training_steps": None, "dataset_notes": None,
            "adapter": ({"format": full["adapter_format"], "rank": full["adapter_rank"],
                         "alpha": full["adapter_alpha"]} if full["is_adapter"] else None),
            "license": full["license_text"],
        },
        "files": files,
        "civitai": {
            "state": full["civitai_state"],
            "reason": None if hashed else "not_hashed",
            "hint": None if hashed else
            "Compute the SHA-256 hash to enable Civitai matching.",
            "model_id": full["civitai_model_id"], "version_id": full["civitai_version_id"],
            "url": full["civitai_url"], "checked_at": full["civitai_checked_at"],
        },
        "update": {"has_update": bool(full["has_update"]),
                   "latest_version_name": full["latest_version_name"],
                   "benefits": full["latest_version_benefits"],
                   "checked_at": full["civitai_checked_at"]},
        "description": {"text": full["description"], "source": full["description_source"]},
        "usage_notes": full["usage_notes"],
        "trigger_words": json_list(full["trigger_words_json"]),
        "recommended_settings": json_obj(full["recommended_settings_json"]),
        "download": {"url": full["download_url"], "source": "civitai" if full["download_url"] else None},
        "usage": {"workflow_count": int(full["workflow_count"] or 0),
                  "output_count": int(full["output_count"] or 0),
                  "top_workflows": usage["workflows"][:5]},
        "user_notes": full["user_notes"],
        "integrity": {"status": full["integrity"], "note": full["integrity_note"]},
        "actions": {
            "can_hash": row["hash_state"] != "done", "can_rename": True,
            "can_move": True, "can_delete": True,
            "can_refresh_metadata": hashed,
            "refresh_blocked_reason": None if hashed else "hash_required",
        },
    })
    return item


def model_usage(model_id: int, limit: int = 50, offset: int = 0,
                conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or dbmod.get_ro()
    limit, offset = clamp_page(limit, offset)
    rows = dbmod.rows(
        conn,
        "SELECT w.id, w.name, w.rel_path, d.occurrences, d.via_class, d.via_input, "
        "d.match_method FROM workflow_dependencies d JOIN workflows w "
        "ON w.id = d.workflow_id WHERE d.model_id = ? ORDER BY w.name LIMIT ? OFFSET ?",
        (int(model_id), limit, offset),
    )
    by_wf: dict[int, dict] = {}
    for r in rows:
        entry = by_wf.setdefault(int(r["id"]), {
            "uid": f"workflow:{r['id']}", "name": r["name"], "rel_path": r["rel_path"],
            "occurrences": 0, "via": [], "match_method": r["match_method"],
        })
        entry["occurrences"] += int(r["occurrences"] or 1)
        entry["via"].append({"class": r["via_class"], "input": r["via_input"]})
    total_wf = int(dbmod.scalar(
        conn, "SELECT COUNT(DISTINCT workflow_id) FROM workflow_dependencies "
              "WHERE model_id = ?", (int(model_id),)) or 0)

    out_count = int(dbmod.scalar(
        conn, "SELECT COUNT(*) FROM outputs WHERE model_id = ?", (int(model_id),)) or 0)
    recent = [
        {"uid": f"output:{r['id']}", "filename": r["filename"],
         "created_at": int(r["created_at_file"] or 0),
         "thumbnail_url": thumb_url(f"output:{r['id']}", 160)}
        for r in dbmod.rows(
            conn, "SELECT id, filename, created_at_file FROM outputs WHERE model_id = ? "
                  "ORDER BY created_at_file DESC LIMIT 12", (int(model_id),))
    ]
    return {
        "workflows": list(by_wf.values()),
        "outputs": {"count": out_count, "recent": recent},
        "page": page_dict(limit, offset, total_wf, len(by_wf)),
    }


def model_tree(filters: ModelFilters | dict | None = None,
               conn: sqlite3.Connection | None = None) -> dict:
    """The left-rail folder tree."""
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, ModelFilters) else ModelFilters.from_dict(filters)
    w = _where(f)
    rows = dbmod.rows(
        conn,
        f"SELECT category, folder, COUNT(*) n, COALESCE(SUM(total_size),0) b "  # noqa: S608
        f"FROM v_model_list WHERE {w.sql()} GROUP BY category, folder", w.args(),
    )
    tree: dict[str, dict] = {}
    for r in rows:
        cat = str(r["category"] or "")
        node = tree.setdefault(cat, {"key": cat, "label": _label("category", cat),
                                     "count": 0, "bytes": 0,
                                     "query": {"category": cat}, "children": []})
        node["count"] += int(r["n"])
        node["bytes"] += int(r["b"])
        folder = str(r["folder"] or "")
        if folder:
            node["children"].append({
                "key": f"{cat}/{folder}", "label": folder.rsplit("/", 1)[-1],
                "count": int(r["n"]), "bytes": int(r["b"]),
                "query": {"category": cat, "folder": folder}, "children": [],
            })
    return {"group": "folder", "nodes": sorted(tree.values(), key=lambda n: n["key"])}
