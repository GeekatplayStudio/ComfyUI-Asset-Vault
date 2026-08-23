"""MCP resources and resource templates (MCP_SPEC 6).

Resources give an agent bulk context without a tool round-trip.  Like the
handlers, they read exclusively through ``services/queries`` - no SQL here.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from ..core import config_service
from ..services.queries import models_query, nodes_query, workflows_query
from . import handlers

log = logging.getLogger("vault.mcp")

MIME_JSON = "application/json"
ALL_ROWS = handlers.ALL_ROWS

RESOURCES: tuple[dict, ...] = (
    {"uri": "vault://stats", "name": "vault_stats", "title": "Vault statistics",
     "description": "Counts, bytes, roots, last scan and capability flags for the "
                    "whole installation.",
     "mimeType": MIME_JSON},
    {"uri": "vault://models/index", "name": "model_index", "title": "Model index",
     "description": "Compact [uid, name, category, base_model, size_bytes] row for "
                    "every indexed model.",
     "mimeType": MIME_JSON},
    {"uri": "vault://nodes/index", "name": "node_class_index",
     "title": "Node class index",
     "description": "Compact [node_id, display_name, category, package] row for every "
                    "installed node class.",
     "mimeType": MIME_JSON},
    {"uri": "vault://workflows/index", "name": "workflow_index",
     "title": "Workflow index",
     "description": "Compact [uid, name, rel_path, base_model, node_count, runnable] "
                    "row for every indexed workflow.",
     "mimeType": MIME_JSON},
    {"uri": "vault://health", "name": "health", "title": "Health report",
     "description": "Per-check health status for the vault: roots, database, "
                    "embeddings, integrity, node remotes.",
     "mimeType": MIME_JSON},
)

RESOURCE_TEMPLATES: tuple[dict, ...] = (
    {"uriTemplate": "vault://model/{id}", "name": "model_detail",
     "title": "Model detail", "description": "The full get_model payload for one model "
                                             "id (the number in 'model:41').",
     "mimeType": MIME_JSON},
    {"uriTemplate": "vault://workflow/{id}", "name": "workflow_detail",
     "title": "Workflow detail",
     "description": "The full inspect_workflow payload for one workflow id.",
     "mimeType": MIME_JSON},
    {"uriTemplate": "vault://workflow/{id}/graph", "name": "workflow_graph",
     "title": "Workflow graph", "description": "The raw workflow graph JSON.",
     "mimeType": MIME_JSON},
    {"uriTemplate": "vault://node-class/{node_id}", "name": "node_class_detail",
     "title": "Node class detail",
     "description": "The full class record for one ComfyUI class id.",
     "mimeType": MIME_JSON},
)

_MODEL_URI = re.compile(r"^vault://model/([0-9]+)$")
_WORKFLOW_URI = re.compile(r"^vault://workflow/([0-9]+)$")
_WORKFLOW_GRAPH_URI = re.compile(r"^vault://workflow/([0-9]+)/graph$")
_NODE_CLASS_URI = re.compile(r"^vault://node-class/(.+)$")


class ResourceNotFound(Exception):
    """No resource is registered at that URI."""


def _models_index(ctx: handlers.Ctx) -> list[list[Any]]:
    rows: list[list[Any]] = []
    offset = 0
    while True:
        page = models_query.list_models({}, sort="name", limit=ALL_ROWS,
                                        offset=offset, conn=ctx.conn)
        rows.extend([item["uid"], item.get("name"), item.get("category"),
                     (item.get("base_model") or {}).get("family"),
                     int(item.get("size") or 0)] for item in page.items)
        if len(page.items) < ALL_ROWS:
            return rows
        offset += ALL_ROWS


def _nodes_index(ctx: handlers.Ctx) -> list[list[Any]]:
    rows: list[list[Any]] = []
    offset = 0
    while True:
        page = nodes_query.list_node_classes({}, sort="name", limit=ALL_ROWS,
                                             offset=offset, conn=ctx.conn)
        rows.extend([item.get("node_id"), item.get("display_name"),
                     item.get("category"),
                     (item.get("package") or {}).get("name")] for item in page.items)
        if len(page.items) < ALL_ROWS:
            return rows
        offset += ALL_ROWS


def _workflows_index(ctx: handlers.Ctx) -> list[list[Any]]:
    rows: list[list[Any]] = []
    offset = 0
    while True:
        page = workflows_query.list_workflows({}, sort="name", limit=ALL_ROWS,
                                              offset=offset, conn=ctx.conn)
        rows.extend([item["uid"], item.get("name"), item.get("rel_path"),
                     item.get("base_model"),
                     int((item.get("counts") or {}).get("nodes") or 0),
                     bool(item.get("is_runnable"))] for item in page.items)
        if len(page.items) < ALL_ROWS:
            return rows
        offset += ALL_ROWS


def _health(ctx: handlers.Ctx) -> dict:
    cfg = config_service.get_config()
    _text, index_status = handlers.get_index_status({}, ctx)
    facets = models_query.model_facets({}, conn=ctx.conn)
    bad = [r for r in (facets.get("integrity") or [])
           if r.get("value") not in (None, "ok")]
    packages = nodes_query.list_node_packages({}, limit=ALL_ROWS, conn=ctx.conn)
    suspect = [p.get("folder_name") for p in packages.items
               if (p.get("repo") or {}).get("suspect")]

    checks = [
        {"id": "comfyui_root",
         "status": "ok" if (cfg.comfyui_path and os.path.isdir(str(cfg.comfyui_path)))
         else "error",
         "message": str(cfg.comfyui_path or "not configured")},
        {"id": "scan", "status": "ok" if index_status["scan"]["last_completed_at"]
         else "warn",
         "message": f"last completed at {index_status['scan']['last_completed_at']}"},
        {"id": "embeddings",
         "status": "ok" if index_status["embeddings"]["state"] == "ready" else "warn",
         "message": index_status["embeddings"].get("reason") or "Ready"},
        {"id": "integrity", "status": "error" if bad else "ok",
         "count": sum(int(r.get("count") or 0) for r in bad),
         "message": ", ".join(f"{r.get('value')}={r.get('count')}" for r in bad)
         or "no integrity failures"},
        {"id": "suspect_remotes", "status": "warn" if suspect else "ok",
         "count": len(suspect), "message": ", ".join(str(s) for s in suspect[:20])},
        {"id": "mcp_mutations",
         "status": "ok",
         "message": ("read-only (mcp_read_only=true / --read-only)" if ctx.read_only
                     else "enabled (DECISIONS C5)")},
    ]
    status = "ok"
    if any(c["status"] == "error" for c in checks):
        status = "error"
    elif any(c["status"] == "warn" for c in checks):
        status = "degraded"
    return {"status": status, "checks": checks}


def read(uri: str, ctx: handlers.Ctx) -> dict:
    """Return one ``contents`` entry for ``resources/read``."""
    uri = str(uri or "")

    if uri == "vault://stats":
        _text, payload = handlers.vault_stats({}, ctx)
        return _entry(uri, payload)
    if uri == "vault://models/index":
        return _entry(uri, {"columns": ["uid", "name", "category", "base_model",
                                        "size_bytes"],
                            "rows": _models_index(ctx)})
    if uri == "vault://nodes/index":
        return _entry(uri, {"columns": ["node_id", "display_name", "category",
                                        "package"],
                            "rows": _nodes_index(ctx)})
    if uri == "vault://workflows/index":
        return _entry(uri, {"columns": ["uid", "name", "rel_path", "base_model",
                                        "node_count", "is_runnable"],
                            "rows": _workflows_index(ctx)})
    if uri == "vault://health":
        return _entry(uri, _health(ctx))

    m = _MODEL_URI.match(uri)
    if m:
        _text, payload = handlers.get_model({"uid": f"model:{m.group(1)}"}, ctx)
        return _entry(uri, payload)
    m = _WORKFLOW_GRAPH_URI.match(uri)
    if m:
        graph = workflows_query.workflow_graph(int(m.group(1)), conn=ctx.conn)
        if graph is None:
            raise ResourceNotFound(f"No graph is stored for workflow {m.group(1)}.")
        return _entry(uri, graph)
    m = _WORKFLOW_URI.match(uri)
    if m:
        _text, payload = handlers.inspect_workflow(
            {"uid": f"workflow:{m.group(1)}"}, ctx)
        return _entry(uri, payload)
    m = _NODE_CLASS_URI.match(uri)
    if m:
        _text, payload = handlers.get_node_class({"node_id": m.group(1)}, ctx)
        return _entry(uri, payload)

    raise ResourceNotFound(f"Unknown resource URI '{uri}'.")


def _entry(uri: str, payload: Any) -> dict:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) > handlers.STRUCTURED_CAP:
        text = json.dumps({"truncated": True,
                           "note": "Resource exceeded the 512 KB cap; use the "
                                   "equivalent tool with limit/offset instead.",
                           "uri": uri}, ensure_ascii=False)
    return {"uri": uri, "mimeType": MIME_JSON, "text": text}
