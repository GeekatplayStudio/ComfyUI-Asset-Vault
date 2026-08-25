"""Node package / node class queries - contract-shaped (API_CONTRACT 4)."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from ...core import db as dbmod
from .. import file_ops
from . import (
    ListResult,
    Where,
    apply_id_filter,
    attach_matches,
    clamp_page,
    json_list,
    json_obj,
    meta_dict,
    order_by_search,
    page_dict,
    parse_sort,
    search_uids,
    thumb_url,
)

PACKAGE_SORTS = {
    "name": "display_name COLLATE NOCASE", "author": "author COLLATE NOCASE",
    "classes": "class_count", "created": "created_at", "updated": "updated_at", "size": "total_size",
    "relevance": "id",
}
CLASS_SORTS = {
    "name": "node_id COLLATE NOCASE", "display_name": "display_name COLLATE NOCASE",
    "category": "category COLLATE NOCASE", "package": "package_id", "relevance": "id",
}
PACKAGE_GROUPS = {"author": "author", "official": "is_official", "enabled": "enabled",
                  "update_state": "update_check_state"}
CLASS_GROUPS = {"category": "category", "package": "package_id"}


@dataclass
class NodeFilters:
    q: str | None = None
    smart: bool = False
    official: bool | None = None
    enabled: bool | None = None
    has_update: bool | None = None
    author: list[str] = field(default_factory=list)
    update_state: list[str] = field(default_factory=list)
    tag: list[str] = field(default_factory=list)
    package_id: int | None = None
    category: list[str] = field(default_factory=list)
    deprecated: bool | None = None
    experimental: bool | None = None
    confidence: list[str] = field(default_factory=list)
    include_missing: bool = False
    missing_files_only: bool | None = None
    untagged: bool | None = None

    @classmethod
    def from_dict(cls, data: dict | None) -> NodeFilters:
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


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

def _pkg_where(f: NodeFilters) -> Where:
    w = Where()
    if f.missing_files_only:
        w.add("missing_since IS NOT NULL")
    elif not f.include_missing:
        w.add("missing_since IS NULL")
    w.bool_eq("is_official", f.official)
    w.bool_eq("enabled", f.enabled)
    w.bool_eq("has_update", f.has_update)
    w.any_of("author", f.author)
    w.any_of("update_check_state", f.update_state)
    for tag in f.tag or []:
        w.add("('node_package:' || id) IN (SELECT at.uid FROM asset_tags at "
              "JOIN tags t ON t.id = at.tag_id WHERE t.name_key = ?)", str(tag).lower())
    if f.untagged:
        w.add("NOT EXISTS (SELECT 1 FROM asset_tags WHERE uid = ('node_package:' || id))")
    return w


def _pkg_item(row: sqlite3.Row) -> dict:
    uid = f"node_package:{row['id']}"
    strategies = json_list(row["extraction_strategies_json"])
    confidence = ("declared" if {"S1", "S2", "S3", "S4"} & set(strategies)
                  else "inferred" if "S5" in strategies else "registry")
    return {
        "uid": uid, "id": int(row["id"]), "folder_name": row["folder_name"],
        "display_name": row["display_name"], "author": row["author"],
        "publisher_id": row["publisher_id"], "description": row["description"],
        "is_official": bool(row["is_official"]), "enabled": bool(row["enabled"]),
        "is_single_file": bool(row["is_single_file"]),
        "class_count": int(row["class_count"] or 0),
        "extraction": {"status": row["extraction_status"], "strategies": strategies,
                       "confidence": confidence},
        "repo": {"url": row["repo_url"], "suspect": bool(row["repo_url_suspect"]),
                 "branch": row["git_branch"],
                 "commit": (row["git_commit"] or "")[:7] or None,
                 "commit_at": row["git_commit_at"]},
        "update": {"state": row["update_check_state"],
                   "has_update": bool(row["has_update"]),
                   "commits_behind": row["commits_behind"],
                   "checked_at": row["update_checked_at"]},
        "version": row["installed_version"],
        "deps": {"count": len(json_list(row["python_deps_json"])),
                 "satisfied": row["deps_satisfied"],
                 "missing": json_list(row["deps_missing_json"])},
        "size": int(row["total_size"] or 0), "file_count": row["file_count"],
        "counts": {"workflows": int(row["workflow_count"] or 0)},
        "thumbnail_url": thumb_url(uid, 160),
        "missing": row["missing_since"] is not None,
    }


def list_node_packages(filters: NodeFilters | dict | None = None, sort: str = "name",
                       group: str = "none", limit: int = 100, offset: int = 0,
                       conn: sqlite3.Connection | None = None) -> ListResult:
    t0 = time.perf_counter()
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, NodeFilters) else NodeFilters.from_dict(filters)
    limit, offset = clamp_page(limit, offset)

    ids, search_meta, matches = search_uids(f.q, f.smart, ["node_package"], conn)
    w = _pkg_where(f)
    if not apply_id_filter(w, "id", ids):
        return ListResult(items=[], page=page_dict(limit, offset, 0, 0),
                          meta=meta_dict(t0, sort=sort, **search_meta))
    where_sql, args = w.sql(), w.args()
    total = int(dbmod.scalar(
        conn, f"SELECT COUNT(*) FROM node_packages WHERE {where_sql}", args) or 0)  # noqa: S608
    order = (order_by_search(ids, "id") or "display_name COLLATE NOCASE ASC"
             if sort == "relevance" else parse_sort(sort, PACKAGE_SORTS, "name"))
    if sort == "relevance":
        order += ", id ASC"
    rows = dbmod.rows(
        conn,
        f"SELECT * FROM node_packages WHERE {where_sql} ORDER BY {order} "  # noqa: S608
        "LIMIT ? OFFSET ?", (*args, limit, offset),
    )
    groups = None
    if group and group in PACKAGE_GROUPS:
        col = PACKAGE_GROUPS[group]
        groups = [
            {"key": str(r["k"] if r["k"] is not None else ""), "label": str(r["k"]),
             "count": int(r["n"]), "offset": 0}
            for r in dbmod.rows(
                conn, f"SELECT {col} AS k, COUNT(*) n FROM node_packages "  # noqa: S608
                      f"WHERE {where_sql} GROUP BY k ORDER BY n DESC", args)
        ]
    return ListResult(items=attach_matches([_pkg_item(r) for r in rows], matches),
                      page=page_dict(limit, offset, total, len(rows)),
                      groups=groups,
                      meta=meta_dict(t0, sort=f"{sort},id", **search_meta))


def get_node_package(package_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, "SELECT * FROM node_packages WHERE id = ?", (int(package_id),))
    if row is None:
        return None
    item = _pkg_item(row)
    categories = [
        {"category": r["category"] or "", "count": int(r["n"])}
        for r in dbmod.rows(
            conn, "SELECT category, COUNT(*) n FROM node_classes WHERE package_id = ? "
                  "GROUP BY category ORDER BY n DESC", (int(package_id),))
    ]
    top = [
        {"uid": f"node_class:{r['id']}", "node_id": r["node_id"],
         "display_name": r["display_name"], "category": r["category"]}
        for r in dbmod.rows(
            conn, "SELECT id, node_id, display_name, category FROM node_classes "
                  "WHERE package_id = ? ORDER BY display_name COLLATE NOCASE LIMIT 24",
            (int(package_id),))
    ]
    item.update({
        "long_description": row["long_description"], "license": row["license"],
        "homepage_url": row["homepage_url"], "icon_url": row["icon_url"],
        "abs_path": row["abs_path"],
        "python_deps": json_list(row["python_deps_json"]),
        "has_web_directory": bool(row["has_web_directory"]),
        "class_categories": categories, "top_classes": top,
        "source_breakdown": json_obj(row["source_breakdown_json"]),
        "disabled_reason": row["disabled_reason"],
        "actions": {
            "can_check_update": bool(row["repo_url"]) and not row["repo_url_suspect"],
            # Deleting a package is supported and trash-backed; renaming/moving
            # it is deliberately withheld - the UI shows `rename_blocked_reason`
            # rather than a dead button (API_CONTRACT 11).
            "can_delete": True,
            "can_rename": False,
            "can_move": False,
            "rename_blocked_reason": file_ops.NODE_PACKAGE_RENAME_REASON,
            "move_blocked_reason": file_ops.NODE_PACKAGE_RENAME_REASON,
        },
    })
    if row["is_official"]:
        item["comfyui_version"] = row["installed_version"]
    return item


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

def _class_where(f: NodeFilters) -> Where:
    w = Where()
    w.eq("nc.package_id", f.package_id)
    w.any_of("nc.category", f.category)
    w.bool_eq("nc.is_deprecated", f.deprecated)
    w.bool_eq("nc.is_experimental", f.experimental)
    w.any_of("nc.confidence", f.confidence)
    w.bool_eq("p.is_official", f.official)
    if f.missing_files_only:
        w.add("p.missing_since IS NOT NULL")
    if f.untagged:
        w.add("NOT EXISTS (SELECT 1 FROM asset_tags WHERE uid = ('node_class:' || nc.id))")
    return w


def _class_item(row: sqlite3.Row) -> dict:
    uid = f"node_class:{row['id']}"
    inputs = json_obj(row["input_types_json"]) or {}
    return {
        "uid": uid, "id": int(row["id"]), "node_id": row["node_id"],
        "display_name": row["display_name"], "class_name": row["class_name"],
        "category": row["category"], "description": row["description"],
        "package": {"uid": f"node_package:{row['package_id']}",
                    "name": row["package_name"],
                    "official": bool(row["is_official"])},
        "inputs": {"required": inputs.get("required") or {},
                   "optional": inputs.get("optional") or {}},
        "outputs": {"types": json_list(row["return_types_json"]) or None,
                    "names": json_list(row["return_names_json"]) or None},
        "output_node": bool(row["output_node"]),
        "flags": {"deprecated": bool(row["is_deprecated"]),
                  "experimental": bool(row["is_experimental"]),
                  "api_node": bool(row["is_api_node"])},
        "confidence": row["confidence"],
        "source": {"strategy": row["source_strategy"], "file": row["source_file"],
                   "lineno": row["source_lineno"]},
        # Where the class comes from at runtime: python / javascript /
        # frontend.  The last two have no Python definition anywhere.
        "registration": row["registration"],
        "counts": {"workflows": int(row["workflow_count"] or 0)},
    }


_CLASS_SELECT = (
    "SELECT nc.*, p.display_name AS package_name, p.is_official "
    "FROM node_classes nc JOIN node_packages p ON p.id = nc.package_id"
)


def list_node_classes(filters: NodeFilters | dict | None = None,
                      sort: str = "display_name", group: str = "none",
                      limit: int = 100, offset: int = 0,
                      conn: sqlite3.Connection | None = None) -> ListResult:
    t0 = time.perf_counter()
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, NodeFilters) else NodeFilters.from_dict(filters)
    limit, offset = clamp_page(limit, offset)

    ids, search_meta, matches = search_uids(f.q, f.smart, ["node_class"], conn)
    w = _class_where(f)
    if not apply_id_filter(w, "nc.id", ids):
        return ListResult(items=[], page=page_dict(limit, offset, 0, 0),
                          meta=meta_dict(t0, sort=sort, **search_meta))
    where_sql, args = w.sql(), w.args()
    total = int(dbmod.scalar(
        conn, "SELECT COUNT(*) FROM node_classes nc JOIN node_packages p "  # noqa: S608
              f"ON p.id = nc.package_id WHERE {where_sql}", args) or 0)
    if sort == "relevance":
        order = (order_by_search(ids, "nc.id") or "nc.display_name COLLATE NOCASE ASC") \
            + ", nc.id ASC"
    else:
        order = parse_sort(sort, CLASS_SORTS, "display_name").replace(
            " id ASC", " nc.id ASC")
        order = ", ".join(
            part if part.strip().startswith(("nc.", "p.")) else "nc." + part.strip()
            for part in order.split(","))
    rows = dbmod.rows(
        conn, f"{_CLASS_SELECT} WHERE {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
        (*args, limit, offset),
    )
    groups = None
    if group and group in CLASS_GROUPS:
        col = "nc." + CLASS_GROUPS[group]
        groups = [
            {"key": str(r["k"] if r["k"] is not None else ""), "label": str(r["k"] or ""),
             "count": int(r["n"]), "offset": 0}
            for r in dbmod.rows(
                conn, f"SELECT {col} AS k, COUNT(*) n FROM node_classes nc "  # noqa: S608
                      f"JOIN node_packages p ON p.id = nc.package_id WHERE {where_sql} "
                      "GROUP BY k ORDER BY n DESC", args)
        ]
    return ListResult(items=attach_matches([_class_item(r) for r in rows], matches),
                      page=page_dict(limit, offset, total, len(rows)),
                      groups=groups,
                      meta=meta_dict(t0, sort=f"{sort},id", **search_meta))


def get_node_class(class_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, f"{_CLASS_SELECT} WHERE nc.id = ?", (int(class_id),))
    if row is None:
        return None
    item = _class_item(row)
    item["workflows_using"] = [
        {"uid": f"workflow:{r['id']}", "name": r["name"], "occurrences": int(r["c"] or 1)}
        for r in dbmod.rows(
            conn, "SELECT w.id, w.name, d.occurrences c FROM workflow_dependencies d "
                  "JOIN workflows w ON w.id = d.workflow_id WHERE d.node_class_id = ? "
                  "ORDER BY w.name LIMIT 20", (int(class_id),))
    ]
    item["input_types_json"] = row["input_types_json"]
    return item


def node_facets(filters: NodeFilters | dict | None = None,
                conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or dbmod.get_ro()
    f = filters if isinstance(filters, NodeFilters) else NodeFilters.from_dict(filters)
    w = _pkg_where(f)
    out: dict = {}
    for name, col in (("author", "author"), ("official", "is_official"),
                      ("enabled", "enabled"), ("update_state", "update_check_state"),
                      ("extraction_status", "extraction_status")):
        rows = dbmod.rows(
            conn, f"SELECT {col} AS v, COUNT(*) n FROM node_packages "  # noqa: S608
                  f"WHERE {w.sql()} GROUP BY v ORDER BY n DESC", w.args())
        out[name] = [{"value": r["v"], "count": int(r["n"])} for r in rows]
    out["category"] = [
        {"value": r["category"], "count": int(r["n"])} for r in dbmod.rows(
            conn, "SELECT category, COUNT(*) n FROM node_classes GROUP BY category "
                  "ORDER BY n DESC LIMIT 60")
    ]
    return out
