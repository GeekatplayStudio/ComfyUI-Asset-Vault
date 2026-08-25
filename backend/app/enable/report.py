"""The dependency report - what the user sees before anything is downloaded.

REQUIREMENTS_R2 C9.1: *nothing downloads before the user sees this.*  Every
missing model appears with its resolved category and therefore its exact target
folder, every missing node class with the package and repository the
ComfyUI-Manager registry names for it, and the total download size.  Items with
no declared source are shown too, with the reason - a dependency the app cannot
fetch is information the owner needs, not something to hide.

The report also issues the ``plan_token`` that ``service.fetch`` demands, so the
set of items that runs is exactly the set of items that was displayed (R9).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from ..core import config_service
from ..core import db as dbmod
from ..core.errors import AppError, NotFoundError
from ..core.pathsafe import long_path
from ..parsers import node_registry
from ..services.queries import workflows_query
from . import download, git_fetch, hosts, placement, plan, sources

log = logging.getLogger(__name__)

STATUS_FETCHABLE = "fetchable"
STATUS_PRESENT = "already_present"
STATUS_NO_SOURCE = "no_source"
STATUS_BLOCKED = "blocked"

#: Stated in the report itself so the promise travels with the payload rather
#: than living only in a document nobody reads at 2 a.m.
NEVER_RUNS = (
    "No pip install is ever run.",
    "No requirements.txt is ever processed.",
    "No install.py, setup.py or post-clone hook is ever executed.",
    "Git submodules are never fetched.",
    "Nothing is written outside a configured ComfyUI root.",
    "No existing file is ever overwritten.",
)


def build(workflow_id: int, *, conn: sqlite3.Connection | None = None,
          on_conflict: str = "fail") -> dict:
    """Resolve everything this workflow is missing.  Reads only."""
    conn = conn or dbmod.get_ro()
    workflow = workflows_query.get_workflow(int(workflow_id))
    if workflow is None:
        raise NotFoundError(f"Workflow {workflow_id} does not exist.",
                            details={"uid": f"workflow:{workflow_id}"})
    cfg = config_service.get_config()
    deps = workflows_query.workflow_dependencies(int(workflow_id), conn=conn)
    graph = sources.workflow_graph_json(int(workflow_id), conn=conn)
    manifest = sources.manifest_index(graph)

    raw = dbmod.rows(
        conn,
        "SELECT ref_name, ref_category, via_class, via_input, occurrences, status "
        "FROM workflow_dependencies WHERE workflow_id = ? AND dep_kind = 'model'",
        (int(workflow_id),))
    via_by_ref = {str(r["ref_name"]): dict(r) for r in raw}

    models = [_model_item(entry, via_by_ref, manifest, conn, cfg, on_conflict)
              for entry in deps.get("models") or []
              if str(entry.get("status")) == "missing"]
    nodes = _node_items(deps.get("nodes") or [], cfg)

    items = [plan.PlanItem(item_id=i["item_id"], kind=i["kind"],
                           ref_name=i["ref_name"], payload=i["_payload"])
             for i in [*models, *nodes] if i.get("_payload")]
    issued = plan.issue(int(workflow_id), items)
    for i in (*models, *nodes):
        i.pop("_payload", None)

    fetchable_models = [m for m in models if m["status"] == STATUS_FETCHABLE]
    fetchable_nodes = [n for n in nodes if n["status"] == STATUS_FETCHABLE]
    download_bytes = sum(int(m["source"]["size"] or 0) for m in fetchable_models)
    unknown_size = sum(1 for m in fetchable_models if not m["source"]["size"])

    return {
        "workflow": {
            "uid": f"workflow:{int(workflow_id)}", "id": int(workflow_id),
            "name": workflow.get("name"), "is_runnable": bool(workflow.get("is_runnable")),
            "origin": workflow.get("origin"),
        },
        "summary": {
            "total": int((deps.get("summary") or {}).get("total") or 0),
            "satisfied": int((deps.get("summary") or {}).get("satisfied") or 0),
            "missing_models": len(models),
            "missing_node_packages": len(nodes),
            "fetchable": len(fetchable_models) + len(fetchable_nodes),
            "not_fetchable": (len(models) - len(fetchable_models)
                              + len(nodes) - len(fetchable_nodes)),
            "download_bytes": download_bytes,
            "items_with_unknown_size": unknown_size,
        },
        "space": _space(fetchable_models, download_bytes, cfg),
        "models": models,
        "node_packages": nodes,
        "plan_token": issued.token,
        "plan_expires_in_ms": issued.ttl_ms(),
        "plan_items": len(items),
        "policy": {**hosts.describe(), "never_runs": list(NEVER_RUNS),
                   "on_conflict_allowed": list(download.ON_CONFLICT),
                   "git_available": git_fetch.available()},
        "generated_at": dbmod.now_ms(),
    }


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def _model_item(entry: dict, via_by_ref: dict, manifest: dict, conn, cfg,
                on_conflict: str) -> dict:
    ref_name = str(entry.get("ref_name") or "")
    raw = via_by_ref.get(ref_name) or {}
    via_class = raw.get("via_class")
    via_input = raw.get("via_input")
    item: dict[str, Any] = {
        "item_id": plan.item_id("model", ref_name),
        "kind": "model",
        "ref_name": ref_name,
        "occurrences": int(entry.get("occurrences") or 1),
        "via": [{"class": via_class, "input": via_input}],
        "category": None,
        "destination": None,
        "source": None,
        "status": STATUS_NO_SOURCE,
        "reason": None,
        "suggestions": entry.get("suggestions") or [],
    }

    category = placement.category_for(via_class, via_input,
                                      entry.get("category") or raw.get("ref_category"))
    item["category"] = category
    if not category:
        item["status"] = STATUS_BLOCKED
        item["reason"] = ("The node input that references this file is not one of "
                          "the known ComfyUI model inputs, so the target folder "
                          "cannot be derived. Place it by hand.")
        return item

    try:
        dest = placement.resolve_destination(category, ref_name, cfg=cfg)
    except AppError as exc:
        item["status"] = STATUS_BLOCKED
        item["reason"] = exc.message
        return item
    item["destination"] = dest.as_dict()

    if os.path.exists(long_path(dest.abs_path)):
        item["status"] = STATUS_PRESENT
        item["reason"] = ("A file with this name is already in the target folder. "
                          "Re-scan the vault so it is indexed.")
        return item

    source = manifest.get(_key(ref_name)) or sources.vault_cached_source(ref_name, conn)
    if source is None or not source.url:
        item["status"] = STATUS_NO_SOURCE
        item["source"] = source.as_dict() if source else None
        item["reason"] = ((source.notes[0] if source and source.notes else None)
                          or "This workflow does not declare where the file came "
                             "from, and the vault has no cached source for it. "
                             "Download it yourself and drop it in the folder above.")
        return item

    item["source"] = source.as_dict()
    item["status"] = STATUS_FETCHABLE
    item["_payload"] = {
        "kind": "model",
        "ref_name": ref_name,
        "category": category,
        "provider": source.provider,
        "source_url": source.url,
        "source_host": source.host,
        "expected_size": int(source.expected_size or 0),
        "expected_sha256": source.expected_sha256,
        "root_id": dest.root.id,
        "root_path": dest.root.path,
        "target_abs_path": dest.abs_path,
        "on_conflict": on_conflict,
    }
    return item


def _key(ref_name: str) -> str:
    return str(ref_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


# ---------------------------------------------------------------------------
# Node packages
# ---------------------------------------------------------------------------

def _node_items(nodes: list[dict], cfg) -> list[dict]:
    """Group missing node classes by the package the registry names for them."""
    missing = [n for n in nodes if str(n.get("status")) == "missing"]
    if not missing:
        return []
    grouped: dict[str, dict] = {}
    unresolved: list[dict] = []
    for node in missing:
        class_type = str(node.get("class_type") or "")
        hint = sources.registry_source(class_type, cfg.comfyui_path)
        if not hint:
            unresolved.append({"class_type": class_type,
                               "occurrences": int(node.get("occurrences") or 1)})
            continue
        bucket = grouped.setdefault(hint["repo_url"], {
            "package": hint["package"], "repo_url": hint["repo_url"],
            "host": hint["host"], "allowed": hint["allowed"],
            "reason": hint.get("reason"), "classes": []})
        bucket["classes"].append(class_type)

    out: list[dict] = []
    for repo_url, bucket in sorted(grouped.items()):
        folder = node_registry.repo_basename(repo_url) or bucket["package"] or "package"
        item: dict[str, Any] = {
            "item_id": plan.item_id("node_package", repo_url),
            "kind": "node_package",
            "ref_name": bucket["package"] or folder,
            "repo_url": repo_url,
            "host": bucket["host"],
            "class_types": sorted(bucket["classes"])[:60],
            "class_count": len(bucket["classes"]),
            "destination": None,
            "status": STATUS_BLOCKED,
            "reason": None,
            "manual_steps": [],
            "never_runs": list(NEVER_RUNS[:3]),
            "revision": None,
            "safety": [],
        }
        try:
            dest = placement.custom_nodes_destination(str(folder), cfg=cfg)
        except AppError as exc:
            item["reason"] = exc.message
            out.append(item)
            continue
        item["destination"] = dest.as_dict()
        item["manual_steps"] = git_fetch.manual_steps(repo_url, dest.abs_path)
        item["safety"] = [
            {"level": "yellow", "code": "legacy_manager_mapping",
             "message": "This class-to-package match comes from ComfyUI-Manager's legacy map, not a signed package manifest."},
            {"level": "yellow", "code": "no_archive_checksum",
             "message": "Git repositories do not provide a registry archive checksum; a fetchable plan pins the exact commit instead."},
        ]
        if not bucket["allowed"]:
            item["reason"] = (
                f"The registry names a repository this app will not clone: "
                f"{bucket.get('reason') or 'host not allowlisted'}.")
        elif os.path.exists(long_path(dest.abs_path)):
            item["status"] = STATUS_PRESENT
            item["reason"] = ("That folder already exists in custom_nodes. "
                              "Re-scan the vault, or check whether it is disabled.")
        elif not git_fetch.available():
            item["reason"] = ("git was not found on PATH, so this package can only "
                              "be reported. Run the command above yourself.")
        else:
            revision, revision_error = git_fetch.resolve_revision(repo_url)
            if not revision:
                item["reason"] = ("The repository could not be resolved to an immutable commit, "
                                  "so the vault will not install a moving branch tip. "
                                  f"{revision_error or ''}".strip())
                item["safety"].append({"level": "red", "code": "revision_unresolved",
                                       "message": item["reason"]})
                out.append(item)
                continue
            item["revision"] = revision
            item["safety"].append({"level": "green", "code": "commit_pinned",
                                   "message": f"Plan pins commit {revision[:12]}."})
            item["status"] = STATUS_FETCHABLE
            item["_payload"] = {
                "kind": "node_package",
                "ref_name": bucket["package"] or folder,
                "category": "custom_nodes",
                "provider": "comfyui_manager_registry",
                "source_url": repo_url,
                "source_host": bucket["host"],
                "expected_size": 0,
                "expected_sha256": None,
                "expected_commit": revision,
                "root_id": dest.root.id,
                "root_path": dest.root.path,
                "target_abs_path": dest.abs_path,
                "on_conflict": "fail",
            }
        out.append(item)

    if unresolved:
        out.append({
            "item_id": plan.item_id("node_package", "__unresolved__"),
            "kind": "node_package",
            "ref_name": "unidentified node classes",
            "repo_url": None, "host": None,
            "class_types": sorted(u["class_type"] for u in unresolved)[:60],
            "class_count": len(unresolved),
            "destination": None,
            "status": STATUS_BLOCKED,
            "reason": ("The ComfyUI-Manager registry does not name a package for "
                       "these classes. Search for them by name; a package that is "
                       "not in the registry is not one this app will fetch."),
            "manual_steps": [], "never_runs": list(NEVER_RUNS[:3]),
        })
    return out


# ---------------------------------------------------------------------------
# Free space (R6, surfaced before anything starts)
# ---------------------------------------------------------------------------

def _space(fetchable: list[dict], download_bytes: int, cfg) -> dict:
    by_dir: dict[str, int] = {}
    root_of: dict[str, dict] = {}
    for item in fetchable:
        dest = item.get("destination") or {}
        directory = str(dest.get("directory") or "")
        if not directory:
            continue
        by_dir[directory] = by_dir.get(directory, 0) + int(
            (item.get("source") or {}).get("size") or 0)
        root_of[directory] = {"root_id": dest.get("root_id"),
                              "root_label": dest.get("root_label")}
    volumes = []
    sufficient = True
    shortfall = 0
    for directory, needed in sorted(by_dir.items()):
        free, total = download.disk_free(directory)
        required = download.required_with_margin(needed)
        ok = required <= free
        sufficient = sufficient and ok
        shortfall += max(0, required - free)
        volumes.append({
            "directory": directory, **root_of[directory],
            "download_bytes": needed, "required_bytes": required,
            "free_bytes": free, "total_bytes": total, "sufficient": ok,
            "used_pct": round((total - free) / total * 100, 1) if total else None,
        })
    if not volumes:
        for root in cfg.roots:
            if root.kind == "comfyui":
                free, total = download.disk_free(root.path)
                volumes.append({
                    "directory": root.path, "root_id": root.id,
                    "root_label": root.label, "download_bytes": 0,
                    "required_bytes": 0, "free_bytes": free, "total_bytes": total,
                    "sufficient": True,
                    "used_pct": round((total - free) / total * 100, 1) if total else None,
                })
                break
    return {"sufficient": sufficient, "shortfall_bytes": shortfall,
            "download_bytes": int(download_bytes), "volumes": volumes,
            "margin_pct": int((download.SPACE_MARGIN - 1) * 100)}


# ---------------------------------------------------------------------------
# After the fetch (C9.8)
# ---------------------------------------------------------------------------

def recheck(workflow_id: int, conn: sqlite3.Connection | None = None) -> dict:
    """Is this workflow runnable now?  Read-only, and honest about staleness."""
    conn = conn or dbmod.get_ro()
    workflow = workflows_query.get_workflow(int(workflow_id))
    if workflow is None:
        raise NotFoundError(f"Workflow {workflow_id} does not exist.",
                            details={"uid": f"workflow:{workflow_id}"})
    deps = workflows_query.workflow_dependencies(int(workflow_id), conn=conn)
    missing_models = [d for d in deps.get("models") or []
                      if str(d.get("status")) == "missing"]
    missing_nodes = [d for d in deps.get("nodes") or []
                     if str(d.get("status")) == "missing"]
    scan = _scan_state()
    runnable = not missing_models and not missing_nodes
    return {
        "workflow": {"uid": f"workflow:{int(workflow_id)}", "id": int(workflow_id),
                     "name": workflow.get("name")},
        "is_runnable": runnable,
        "is_runnable_recorded": bool(workflow.get("is_runnable")),
        "missing_models": [d.get("ref_name") for d in missing_models][:200],
        "missing_node_classes": [d.get("class_type") for d in missing_nodes][:200],
        "counts": {"missing_models": len(missing_models),
                   "missing_node_classes": len(missing_nodes)},
        "scan": scan,
        "stale": bool(scan.get("running")),
        "message": _recheck_message(runnable, missing_models, missing_nodes, scan),
        "checked_at": dbmod.now_ms(),
    }


def _scan_state() -> dict:
    try:
        from ..indexing.service import get_indexer

        status = get_indexer().status()
    except Exception as exc:  # noqa: BLE001 - a status probe never breaks a report
        log.debug("indexer status unavailable: %s", exc)
        return {"running": False, "phase": None}
    return {"running": bool(status.get("running")), "phase": status.get("phase"),
            "job_id": status.get("job_id")}


def _recheck_message(runnable: bool, missing_models: list, missing_nodes: list,
                     scan: dict) -> str:
    if scan.get("running"):
        return ("A scan is still running, so this answer may be out of date. "
                "Check again when it finishes.")
    if runnable:
        return "Every model and node class this workflow needs is present."
    parts = []
    if missing_nodes:
        parts.append(f"{len(missing_nodes)} node class(es) still missing")
    if missing_models:
        parts.append(f"{len(missing_models)} model(s) still missing")
    return "Not runnable yet: " + ", ".join(parts) + "."
