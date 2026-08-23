"""Workflow list / detail / graph / dependencies - contract-shaped (API_CONTRACT 5)."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from ...core import config_service
from ...core import db as dbmod
from ...parsers import workflow_origin
from . import (
    ListResult,
    Where,
    apply_id_filter,
    clamp_page,
    date_bucket,
    json_list,
    meta_dict,
    order_by_search,
    page_dict,
    parse_sort,
    search_uids,
    thumb_url,
)

SORTS = {
    "name": "name COLLATE NOCASE", "modified": "mtime_ns", "size": "size",
    "nodes": "node_count", "missing": "missing_node_count", "relevance": "id",
}
GROUPS = {"folder": "folder", "base_model": "base_model_family",
          "runnable": "is_runnable"}


@dataclass
class WorkflowFilters:
    q: str | None = None
    smart: bool = False
    folder: str | None = None
    base_model: list[str] = field(default_factory=list)
    runnable: bool | None = None
    missing_only: bool | None = None
    node_class: str | None = None
    model_id: int | None = None
    root_id: list[int] = field(default_factory=list)
    album_id: int | None = None
    tag: list[str] = field(default_factory=list)
    size_min: int | None = None
    size_max: int | None = None
    date_from: int | None = None
    date_to: int | None = None
    include_missing: bool = False

    @classmethod
    def from_dict(cls, data: dict | None) -> WorkflowFilters:
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


def _where(f: WorkflowFilters) -> Where:
    w = Where()
    if not f.include_missing:
        w.add("missing_since IS NULL")
    w.prefix("folder", f.folder)
    w.any_of("base_model_family", f.base_model)
    w.bool_eq("is_runnable", f.runnable)
    w.any_of("root_id", f.root_id)
    w.gte("size", f.size_min)
    w.lte("size", f.size_max)
    w.gte("mtime_ns", None if f.date_from is None else int(f.date_from) * 1_000_000)
    w.lte("mtime_ns", None if f.date_to is None else int(f.date_to) * 1_000_000)
    if f.missing_only:
        w.add("(missing_node_count > 0 OR missing_model_count > 0)")
    if f.node_class:
        w.add("id IN (SELECT workflow_id FROM workflow_nodes WHERE class_type = ?)",
              str(f.node_class))
    if f.model_id is not None:
        w.add("id IN (SELECT workflow_id FROM workflow_dependencies WHERE model_id = ?)",
              int(f.model_id))
    if f.album_id is not None:
        w.add("('workflow:' || id) IN (SELECT uid FROM album_items WHERE album_id = ?)",
              int(f.album_id))
    for tag in f.tag or []:
        w.add("('workflow:' || id) IN (SELECT at.uid FROM asset_tags at JOIN tags t "
              "ON t.id = at.tag_id WHERE t.name_key = ?)", str(tag).lower())
    return w


def _origin_of(row: sqlite3.Row) -> str:
    """``origin`` arrived in schema v4; a row read through an older path can
    still lack it, so fall back to classifying the stored relative path."""
    try:
        value = row["origin"]
    except (IndexError, KeyError):
        value = None
    if value:
        return str(value)
    return workflow_origin.classify(str(row["rel_path"] or ""))[0]


def _origin_package_of(row: sqlite3.Row) -> str | None:
    try:
        value = row["origin_package"]
    except (IndexError, KeyError):
        value = None
    if value:
        return str(value)
    return workflow_origin.classify(str(row["rel_path"] or ""))[1]


def origin_groups(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Workflow counts per origin, and per package for the bundled ones (C8.4)."""
    conn = conn or dbmod.get_ro()
    rows = dbmod.rows(
        conn,
        "SELECT origin, origin_package, COUNT(*) n, "
        "SUM(CASE WHEN is_runnable = 1 THEN 1 ELSE 0 END) runnable "
        "FROM workflows WHERE missing_since IS NULL "
        "GROUP BY origin, origin_package ORDER BY n DESC",
    )
    out: list[dict] = []
    for r in rows:
        origin = str(r["origin"] or "user")
        package = r["origin_package"]
        count = int(r["n"] or 0)
        runnable = int(r["runnable"] or 0)
        out.append({
            "origin": origin,
            "label": workflow_origin.label(origin, package),
            "package": package,
            "count": count, "runnable": runnable, "broken": count - runnable,
        })
    return out


def _item(row: sqlite3.Row, output_count: int = 0) -> dict:
    uid = f"workflow:{row['id']}"
    return {
        "uid": uid, "id": int(row["id"]), "name": row["name"],
        "rel_path": row["rel_path"], "folder": row["folder"] or "",
        "root_id": row["root_id"], "source": row["source"], "format": row["format"],
        # C8.4 - where this graph came from: 'user', 'bundled', 'official template'.
        "origin": _origin_of(row),
        "origin_package": _origin_package_of(row),
        "origin_label": workflow_origin.label(_origin_of(row),
                                              _origin_package_of(row)),
        "title": row["title"], "description": row["description"],
        "description_source": row["description_source"],
        "capability_tags": json_list(row["capability_tags_json"]),
        "base_model": row["base_model_family"], "modality": row["modality"],
        "counts": {
            "nodes": int(row["node_count"] or 0), "links": int(row["link_count"] or 0),
            "groups": int(row["group_count"] or 0),
            "missing_nodes": int(row["missing_node_count"] or 0),
            "missing_models": int(row["missing_model_count"] or 0),
        },
        "is_runnable": bool(row["is_runnable"]),
        "has_subgraphs": bool(row["has_subgraphs"]),
        "prompt_summary": row["prompt_summary"],
        "size": int(row["size"] or 0),
        "modified_at": int(row["mtime_ns"] or 0) // 1_000_000,
        "thumbnail_url": thumb_url(uid),
        "counts_outputs": output_count,
        "missing": row["missing_since"] is not None,
    }


def list_workflows(filters: WorkflowFilters | dict | None = None, sort: str = "-modified",
                   group: str = "none", limit: int = 100, offset: int = 0,
                   conn: sqlite3.Connection | None = None) -> ListResult:
    t0 = time.perf_counter()
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, WorkflowFilters) else WorkflowFilters.from_dict(filters)
    limit, offset = clamp_page(limit, offset)

    ids, search_meta = search_uids(f.q, f.smart, ["workflow"], conn)
    w = _where(f)
    if not apply_id_filter(w, "id", ids):
        return ListResult(items=[], page=page_dict(limit, offset, 0, 0),
                          meta=meta_dict(t0, sort=sort, **search_meta))
    where_sql, args = w.sql(), w.args()
    total = int(dbmod.scalar(
        conn, f"SELECT COUNT(*) FROM workflows WHERE {where_sql}", args) or 0)  # noqa: S608
    if sort == "relevance":
        order = (order_by_search(ids, "id") or "mtime_ns DESC") + ", id ASC"
    else:
        order = parse_sort(sort, SORTS, "-modified")
    rows = dbmod.rows(
        conn, f"SELECT * FROM workflows WHERE {where_sql} ORDER BY {order} "  # noqa: S608
              "LIMIT ? OFFSET ?", (*args, limit, offset),
    )
    groups = None
    if group and group == "date":
        buckets: dict[str, int] = {}
        for r in dbmod.rows(conn, f"SELECT mtime_ns FROM workflows WHERE {where_sql}",  # noqa: S608
                            args):
            key = date_bucket(int(r["mtime_ns"] or 0) // 1_000_000)
            buckets[key] = buckets.get(key, 0) + 1
        groups = [{"key": k, "label": k, "count": v, "offset": 0}
                  for k, v in sorted(buckets.items())]
    elif group and group in GROUPS:
        col = GROUPS[group]
        groups = [
            {"key": str(r["k"] if r["k"] is not None else ""), "label": str(r["k"] or ""),
             "count": int(r["n"]), "offset": 0}
            for r in dbmod.rows(
                conn, f"SELECT {col} AS k, COUNT(*) n FROM workflows "  # noqa: S608
                      f"WHERE {where_sql} GROUP BY k ORDER BY n DESC", args)
        ]
    return ListResult(items=[_item(r) for r in rows],
                      page=page_dict(limit, offset, total, len(rows)),
                      groups=groups,
                      meta=meta_dict(t0, sort=f"{sort},id", **search_meta))


def get_workflow(workflow_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, "SELECT * FROM workflows WHERE id = ?", (int(workflow_id),))
    if row is None:
        return None
    item = _item(row)
    breakdown = [
        {"class_type": r["class_type"], "count": int(r["count"] or 1),
         "resolved": bool(r["resolved"]),
         "uid": f"node_class:{r['node_class_id']}" if r["node_class_id"] else None,
         "package": ({"uid": f"node_package:{r['package_id']}", "name": r["package_name"]}
                     if r["package_id"] else None)}
        for r in dbmod.rows(
            conn, "SELECT wn.class_type, wn.count, wn.resolved, wn.node_class_id, "
                  "nc.package_id, p.display_name AS package_name FROM workflow_nodes wn "
                  "LEFT JOIN node_classes nc ON nc.id = wn.node_class_id "
                  "LEFT JOIN node_packages p ON p.id = nc.package_id "
                  "WHERE wn.workflow_id = ? ORDER BY wn.count DESC, wn.class_type",
            (int(workflow_id),))
    ]
    item.update({
        "node_breakdown": breakdown,
        "positive_prompt": row["positive_prompt"],
        "negative_prompt": row["negative_prompt"],
        "unresolved_inputs": int(row["unresolved_inputs"] or 0),
        "graph_available": bool(row["graph_json"]) or bool(row["abs_path"]),
        "graph_truncated": bool(row["graph_truncated"]),
        "abs_path": row["abs_path"],
        "schema_version": row["schema_version"],
        "author": row["author"],
        "outputs_recent": [],
        "actions": {"can_rename": True, "can_move": True, "can_delete": True,
                    "can_describe": True},
    })
    return item


def workflow_graph(workflow_id: int, fmt: str = "raw",
                   conn: sqlite3.Connection | None = None) -> dict | None:
    """Return the stored graph, re-reading from disk when it exceeded the cap."""
    import json

    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, "SELECT graph_json, abs_path, graph_truncated, format "
                          "FROM workflows WHERE id = ?", (int(workflow_id),))
    if row is None:
        return None
    blob = row["graph_json"]
    if not blob and row["abs_path"]:
        from ...core.pathsafe import long_path

        try:
            with open(long_path(str(row["abs_path"])), "rb") as fh:
                blob = fh.read(64 * 1024 * 1024).decode("utf-8-sig", "replace")
        except OSError:
            blob = None
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    if fmt == "api":
        from ...parsers import graph_utils

        api = graph_utils.normalize_prompt_graph(data.get("prompt")) \
            or graph_utils.normalize_prompt_graph(data) \
            or graph_utils.ui_graph_to_api(data)
        return api
    return data


def workflow_dependencies(workflow_id: int,
                          conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or dbmod.get_ro()
    rows = dbmod.rows(
        conn, "SELECT * FROM workflow_dependencies WHERE workflow_id = ? "
              "ORDER BY dep_kind, ref_name", (int(workflow_id),))
    models: list[dict] = []
    nodes: list[dict] = []
    embeddings: list[dict] = []
    inputs: list[dict] = []
    summary = {"total": 0, "satisfied": 0, "missing": 0, "ambiguous": 0}
    comfy_root = config_service.get_config().comfyui_path

    for r in rows:
        summary["total"] += 1
        status = str(r["status"] or "unknown")
        if status in summary:
            summary[status] += 1
        if r["dep_kind"] == "model":
            entry = {
                "ref_name": r["ref_name"], "category": r["ref_category"],
                "via": [{"class": r["via_class"], "input": r["via_input"]}],
                "occurrences": int(r["occurrences"] or 1), "status": status,
                "match_method": r["match_method"],
                "uid": f"model:{r['model_id']}" if r["model_id"] else None,
            }
            if status == "missing":
                entry["suggestions"] = _suggest_models(conn, str(r["ref_name"]))
            models.append(entry)
        elif r["dep_kind"] == "node":
            entry = {
                "class_type": r["ref_name"], "status": status,
                "uid": f"node_class:{r['node_class_id']}" if r["node_class_id"] else None,
                "occurrences": int(r["occurrences"] or 1),
            }
            if r["node_class_id"]:
                pkg = dbmod.one(
                    conn, "SELECT p.id, p.display_name FROM node_classes nc "
                          "JOIN node_packages p ON p.id = nc.package_id WHERE nc.id = ?",
                    (int(r["node_class_id"]),))
                if pkg:
                    entry["package"] = {"uid": f"node_package:{pkg['id']}",
                                        "name": pkg["display_name"]}
            else:
                hint = _registry_hint(str(r["ref_name"]), comfy_root)
                if hint:
                    entry["registry_hint"] = hint
            nodes.append(entry)
        elif r["dep_kind"] == "embedding":
            embeddings.append({"ref_name": r["ref_name"], "status": status})
        else:
            inputs.append({"ref_name": r["ref_name"], "status": status})
    return {"summary": summary, "models": models, "nodes": nodes,
            "embeddings": embeddings, "input_files": inputs}


def _suggest_models(conn: sqlite3.Connection, ref_name: str, limit: int = 3) -> list[dict]:
    import difflib

    base = ref_name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    rows = dbmod.rows(
        conn, "SELECT m.id, f.filename FROM model_files f JOIN models m "
              "ON m.id = f.model_id WHERE f.missing_since IS NULL LIMIT 4000")
    scored = []
    for r in rows:
        name = str(r["filename"] or "").lower()
        ratio = difflib.SequenceMatcher(None, base, name).ratio()
        if ratio >= 0.6:
            scored.append((ratio, int(r["id"]), r["filename"]))
    scored.sort(reverse=True)
    return [{"uid": f"model:{mid}", "name": name, "score": round(ratio, 2)}
            for ratio, mid, name in scored[:limit]]


def _registry_hint(node_id: str, comfy_root) -> dict | None:
    from ...parsers import node_registry

    return node_registry.get_registry(comfy_root).package_for_node(node_id)


def workflow_facets(filters: WorkflowFilters | dict | None = None,
                    conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, WorkflowFilters) else WorkflowFilters.from_dict(filters)
    w = _where(f)
    out: dict = {}
    for name, col in (("base_model", "base_model_family"), ("modality", "modality"),
                      ("runnable", "is_runnable"), ("folder", "folder"),
                      ("format", "format")):
        out[name] = [
            {"value": r["v"], "count": int(r["n"])} for r in dbmod.rows(
                conn, f"SELECT {col} AS v, COUNT(*) n FROM workflows "  # noqa: S608
                      f"WHERE {w.sql()} GROUP BY v ORDER BY n DESC", w.args())
        ]
    return out
