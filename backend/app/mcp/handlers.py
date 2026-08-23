"""One function per tool.

Every handler takes ``(args, ctx)`` and returns ``(text, structured)``.

**There is no SQL in this package.**  Handlers call exactly the same
``services/queries/*`` and ``services/file_ops.py`` entry points the REST
routers call, which is what keeps the MCP surface and the REST surface from
drifting apart.  Read handlers get the thread-local read-only connection;
mutating handlers go through ``file_ops`` / ``tags_query`` / the job services,
which own the single writer thread.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..core import config_service
from ..core import db as dbmod
from ..core import vault_stats as core_vault_stats
from ..core.errors import AppError
from ..services import file_ops, mcp_audit
from ..services.queries import (
    models_query,
    nodes_query,
    outputs_query,
    tags_query,
    workflows_query,
)
from . import registry

log = logging.getLogger("vault.mcp")

TEXT_CAP = 4_000
STRUCTURED_CAP = 512 * 1024
ALL_ROWS = 500  # services/queries MAX_LIMIT


class ToolFailure(Exception):
    """A tool-level failure: HTTP 200, JSON-RPC result, ``isError: true``."""

    def __init__(self, message: str, code: str | None = None,
                 data: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data or {}


@dataclass
class Ctx:
    """Everything a handler may need that is not an argument."""

    transport: str = "http"
    session_id: str | None = None
    read_only: bool = False
    request_id: Any = None
    progress: Any = None
    meta: dict = field(default_factory=dict)

    @property
    def conn(self):
        """Read-only, thread-local (MCP_SPEC 8)."""
        return dbmod.get_ro()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def human_bytes(n: Any) -> str:
    try:
        value = float(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024 or unit == "PB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"


def clip(text: Any, width: int = 90) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= width else s[:width - 1] + "…"


def render(headline: str, lines: list[str], *, shown: int = 0, total: int = 0) -> str:
    """Compact text block, hard-capped at 4,000 characters (MCP_SPEC 5.0)."""
    out = [headline]
    used = len(headline) + 1
    printed = 0
    for line in lines:
        if used + len(line) + 1 > TEXT_CAP - 60:
            break
        out.append(line)
        used += len(line) + 1
        printed += 1
    remaining = max(0, (total or shown) - (printed if lines else 0))
    if lines and printed < len(lines):
        remaining = max(remaining, len(lines) - printed)
    if remaining > 0 and lines:
        out.append(f"… ({remaining} more; increase offset)")
    text = "\n".join(out)
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP - 1] + "…"


def cap_structured(payload: dict, list_key: str | None = None) -> dict:
    """Keep ``structuredContent`` under 512 KB serialized (MCP_SPEC 8)."""
    try:
        size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return payload
    if size <= STRUCTURED_CAP:
        return payload
    items = payload.get(list_key) if list_key else None
    if not isinstance(items, list) or not items:
        payload["truncated"] = True
        return payload
    while items and size > STRUCTURED_CAP:
        drop = max(1, len(items) // 8)
        del items[-drop:]
        payload[list_key] = items
        payload["returned"] = len(items)
        try:
            size = len(json.dumps(payload, ensure_ascii=False,
                                  default=str).encode("utf-8"))
        except (TypeError, ValueError):
            break
    payload["truncated"] = True
    return payload


def _integrity_block(value) -> dict:
    """``get_model`` returns a dict here; the list row returns a plain string."""
    if isinstance(value, dict):
        return {"status": value.get("status"), "note": value.get("note")}
    return {"status": (str(value) if value else None), "note": None}


def _uid_id(value: str, kind: str) -> int:
    part, _sep, num = str(value).partition(":")
    if part != kind:
        raise ToolFailure(f"Expected a '{kind}:<id>' uid, got '{value}'.")
    try:
        return int(num)
    except (TypeError, ValueError) as exc:
        raise ToolFailure(f"Malformed uid '{value}'.") from exc


# ---------------------------------------------------------------------------
# Name resolution (uid XOR name) - deterministic, no FTS syntax surprises
# ---------------------------------------------------------------------------

def _one_of(args: dict) -> tuple[str | None, str | None]:
    uid, name = args.get("uid"), args.get("name")
    if bool(uid) == bool(name):
        raise ToolFailure("Supply exactly one of 'uid' or 'name'.")
    return uid, name


def _match_by_name(rows: list[dict], name: str, fields: tuple[str, ...],
                   label: str) -> dict:
    needle = str(name).strip().lower()
    exact = [r for r in rows
             if any(str(r.get(f) or "").lower() == needle for f in fields)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ToolFailure(
            f"'{name}' matches {len(exact)} {label}s. Call again with one of these uids:\n"
            + "\n".join(f"  {r['uid']}  {r.get(fields[0])}" for r in exact[:20]))
    scored = []
    for r in rows:
        best = max((difflib.SequenceMatcher(None, needle,
                                            str(r.get(f) or "").lower()).ratio()
                    for f in fields), default=0.0)
        if needle and any(needle in str(r.get(f) or "").lower() for f in fields):
            best = max(best, 0.9)
        if best >= 0.6:
            scored.append((best, r))
    scored.sort(key=lambda t: -t[0])
    if not scored:
        raise ToolFailure(f"No {label} matches '{name}'.")
    if len(scored) == 1 or scored[0][0] - scored[1][0] > 0.15:
        return scored[0][1]
    raise ToolFailure(
        f"'{name}' is ambiguous. Call again with one of these uids:\n"
        + "\n".join(f"  {r['uid']}  {r.get(fields[0])} ({s:.2f})"
                    for s, r in scored[:20]))


def _all_models(ctx: Ctx) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        page = models_query.list_models({}, sort="name", limit=ALL_ROWS,
                                        offset=offset, conn=ctx.conn)
        out.extend(page.items)
        if len(page.items) < ALL_ROWS:
            return out
        offset += ALL_ROWS
        if offset > 20_000:
            return out


def _all_workflows(ctx: Ctx) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        page = workflows_query.list_workflows({}, sort="name", limit=ALL_ROWS,
                                              offset=offset, conn=ctx.conn)
        out.extend(page.items)
        if len(page.items) < ALL_ROWS:
            return out
        offset += ALL_ROWS
        if offset > 20_000:
            return out


def _resolve_model_id(args: dict, ctx: Ctx) -> int:
    uid, name = _one_of(args)
    if uid:
        return _uid_id(uid, "model")
    row = _match_by_name(_all_models(ctx), str(name), ("name", "filename"), "model")
    return int(row["id"])


def _resolve_workflow_id(args: dict, ctx: Ctx) -> int:
    uid, name = _one_of(args)
    if uid:
        return _uid_id(uid, "workflow")
    row = _match_by_name(_all_workflows(ctx), str(name), ("name", "rel_path"), "workflow")
    return int(row["id"])


# ---------------------------------------------------------------------------
# 5.1  vault_search
# ---------------------------------------------------------------------------

def vault_search(args: dict, ctx: Ctx) -> tuple[str, dict]:
    from ..search import hybrid

    query = str(args["query"])
    kinds = list(args.get("kinds") or []) or None
    want_semantic = bool(args.get("semantic", True))
    lim = int(args.get("limit", 20))
    off = int(args.get("offset", 0))

    try:
        res = hybrid.search(query, smart=want_semantic, kinds=kinds, limit=lim,
                            offset=off, conn=ctx.conn)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    results = [
        {
            "uid": str(item.get("uid")),
            "kind": str(item.get("kind") or ""),
            "title": str(item.get("title") or item.get("uid")),
            "subtitle": item.get("subtitle"),
            "snippet": None,
            "score": float(item.get("score") or 0.0),
            "matched": list(item.get("matched") or []),
            "path": None,
        }
        for item in res.items
    ]
    payload = {
        "query": query,
        "mode": res.mode,
        "semantic_available": bool(res.smart_available),
        "semantic_unavailable_reason": res.smart_reason,
        "total": int(res.total),
        "results": results,
    }
    lines = [f"{off + i + 1}. [{r['kind']}] {r['title']}"
             + (f" - {clip(r['subtitle'], 50)}" if r.get("subtitle") else "")
             + f"  ({r['uid']}, score {r['score']:.3f})"
             for i, r in enumerate(results)]
    mode_note = res.mode
    if want_semantic and not res.smart_available:
        mode_note += f" (semantic unavailable: {res.smart_reason or 'not installed'})"
    head = (f"{res.total} match(es) for {query!r} - {mode_note}. "
            f"Showing {len(results)} from offset {off}.")
    return render(head, lines, shown=len(results), total=res.total), \
        cap_structured(payload, "results")


# ---------------------------------------------------------------------------
# 5.2  list_models
# ---------------------------------------------------------------------------

_MODEL_SORTS = {"name": "name", "-name": "-name", "size": "size", "-size": "-size",
                "modified": "modified", "-modified": "-modified",
                "params": "params", "-params": "-params"}

_INTEGRITY_BAD = ["invalid_header", "not_a_model", "truncated", "unreadable",
                  "unsupported_format"]


def _model_row(item: dict) -> dict:
    base = item.get("base_model") or {}
    return {
        "uid": item["uid"],
        "name": item.get("name") or "",
        "filename": item.get("filename") or "",
        "category": item.get("category") or "",
        "role": item.get("role") or "unknown",
        "base_model": base.get("family") or "Unknown",
        "base_model_confidence": float(base.get("confidence") or 0.0),
        "modality": item.get("modality") or "unknown",
        "precision": item.get("precision"),
        "params_display": (item.get("params") or {}).get("display"),
        "size_bytes": int(item.get("size") or 0),
        "hash_state": (item.get("hash") or {}).get("state") or "unhashed",
        "autov2": (item.get("hash") or {}).get("autov2"),
        "integrity": item.get("integrity") or "ok",
        "has_update": bool((item.get("civitai") or {}).get("has_update")),
        "workflow_count": int((item.get("counts") or {}).get("workflows") or 0),
        "output_count": int((item.get("counts") or {}).get("outputs") or 0),
        "rel_path": item.get("rel_path") or "",
    }


def _model_filters(args: dict) -> dict:
    filters: dict[str, Any] = {}
    for key in ("category", "base_model", "role", "modality", "precision",
                "hash_state"):
        if args.get(key):
            filters[key] = list(args[key])
    for key in ("is_adapter", "has_update"):
        if args.get(key) is not None:
            filters[key] = bool(args[key])
    if args.get("integrity_issues_only"):
        filters["integrity"] = list(_INTEGRITY_BAD)
    if args.get("name_contains"):
        filters["q"] = str(args["name_contains"])
    if args.get("min_size_bytes") is not None:
        filters["size_min"] = int(args["min_size_bytes"])
    if args.get("max_size_bytes") is not None:
        filters["size_max"] = int(args["max_size_bytes"])
    return filters


def list_models(args: dict, ctx: Ctx) -> tuple[str, dict]:
    filters = _model_filters(args)
    lim = int(args.get("limit", 50))
    off = int(args.get("offset", 0))
    sort = _MODEL_SORTS.get(str(args.get("sort") or "name"), "name")
    try:
        result = models_query.list_models(filters, sort=sort, limit=lim, offset=off,
                                          conn=ctx.conn)
        facets = models_query.model_facets(filters, conn=ctx.conn)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    models = [_model_row(i) for i in result.items]
    total = int(result.page.get("total") or 0)
    total_bytes = int((facets.get("size") or {}).get("total") or 0)
    payload = {"total": total, "returned": len(models), "offset": off,
               "total_bytes": total_bytes, "models": models}
    lines = [
        f"{off + i + 1}. {m['name']}  [{m['category']}/{m['base_model']}] "
        f"{human_bytes(m['size_bytes'])}  hash={m['hash_state']}  ({m['uid']})"
        for i, m in enumerate(models)
    ]
    head = (f"{total} model(s), {human_bytes(total_bytes)} total. "
            f"Showing {len(models)} from offset {off}.")
    return render(head, lines, shown=len(models), total=total), \
        cap_structured(payload, "models")


# ---------------------------------------------------------------------------
# 5.3  get_model
# ---------------------------------------------------------------------------

def get_model(args: dict, ctx: Ctx) -> tuple[str, dict]:
    model_id = _resolve_model_id(args, ctx)
    item = models_query.get_model(model_id, conn=ctx.conn)
    if item is None:
        raise ToolFailure(f"model:{model_id} does not exist.")

    tech = item.get("technical") or {}
    base = item.get("base_model") or {}
    hashes = item.get("hash") or {}
    civ = item.get("civitai") or {}
    usage = item.get("usage") or {}
    files = item.get("files") or []

    payload = {
        "identity": {
            "uid": item["uid"], "name": item.get("name") or "",
            "filename": item.get("filename"), "category": item.get("category"),
            "role": item.get("role"), "base_model": base.get("family"),
            "base_model_variant": base.get("variant"),
            "modality": item.get("modality"),
            "architecture": item.get("architecture"),
            "is_adapter": bool(item.get("is_adapter")),
            "size_bytes": int(item.get("size") or 0),
            "modified_at": item.get("modified_at"),
            "rel_path": item.get("rel_path"),
        },
        "technical": {
            "tensor_count": tech.get("tensor_count"),
            "format": tech.get("format"),
            "precision": item.get("precision"),
            "quantization": item.get("quantization"),
            "prediction_type": tech.get("prediction_type"),
            "resolution_hint": tech.get("resolution_hint"),
            "params_display": (item.get("params") or {}).get("display"),
            "components": [
                {"name": c.get("name"), "params": int(c.get("params") or 0),
                 "dtype": c.get("dtype"), "share": float(c.get("share") or 0.0)}
                for c in (tech.get("components") or [])
            ],
            "detection": {
                "source": (tech.get("detection") or {}).get("source"),
                "confidence": float((tech.get("detection") or {}).get("confidence")
                                    or 0.0),
                "signals": [str(s) for s in
                            ((tech.get("detection") or {}).get("signals") or [])],
            },
        },
        "files": [
            {"abs_path": f.get("abs_path"), "rel_path": f.get("rel_path"),
             "size_bytes": int(f.get("size") or 0),
             "modified_at": f.get("modified_at"), "format": f.get("format"),
             "hash_state": f.get("hash_state")}
            for f in files
        ],
        "hash": {"state": hashes.get("state") or "unhashed",
                 "sha256": hashes.get("sha256"), "autov2": hashes.get("autov2")},
        "civitai": {
            "state": civ.get("state"),
            "reason": civ.get("reason"),
            "note": (civ.get("hint") if civ.get("reason") != "not_hashed" else
                     "Civitai lookup requires the full-file SHA-256. Hashing is a "
                     "background job; start it with vault_reindex "
                     "{\"job\":\"hash\"}."),
            "url": civ.get("url"),
            "description": (item.get("description") or {}).get("text"),
            "trigger_words": [str(t) for t in (item.get("trigger_words") or [])],
            "recommended_settings": item.get("recommended_settings"),
            "rating": None,
        },
        "update": {
            "has_update": bool((item.get("update") or {}).get("has_update")),
            "latest_version_name": (item.get("update") or {}).get("latest_version_name"),
            "benefits": (item.get("update") or {}).get("benefits"),
        },
        "usage_notes": item.get("usage_notes"),
        "download": {"url": (item.get("download") or {}).get("url"),
                     "source": (item.get("download") or {}).get("source")},
        "usage": {
            "workflow_count": int(usage.get("workflow_count") or 0),
            "output_count": int(usage.get("output_count") or 0),
            "workflows": [
                {"uid": w.get("uid"), "name": w.get("name"),
                 "rel_path": w.get("rel_path"),
                 "occurrences": int(w.get("occurrences") or 1)}
                for w in (usage.get("top_workflows") or [])
            ],
        },
        "integrity": _integrity_block(item.get("integrity")),
        "tags": [str(t) for t in (item.get("tags") or [])],
        "abs_path": item.get("abs_path"),
    }
    ident = payload["identity"]
    lines = [
        f"  category   {ident['category']} / role {ident['role']}",
        f"  base model {ident['base_model']} "
        f"(confidence {base.get('confidence')}, source "
        f"{(tech.get('detection') or {}).get('source')})",
        f"  size       {human_bytes(ident['size_bytes'])}  precision "
        f"{payload['technical']['precision']}  params "
        f"{payload['technical']['params_display']}",
        f"  hash       {payload['hash']['state']}"
        + (f"  autov2 {payload['hash']['autov2']}" if payload["hash"]["autov2"] else ""),
        f"  integrity  {payload['integrity']['status']}",
        f"  used by    {payload['usage']['workflow_count']} workflow(s), "
        f"{payload['usage']['output_count']} output(s)",
        f"  path       {ident['rel_path']}",
    ]
    if payload["technical"]["components"]:
        lines.append("  components " + ", ".join(
            f"{c['name']}({c['share'] * 100:.0f}%)"
            for c in payload["technical"]["components"][:6]))
    if payload["civitai"]["reason"] == "not_hashed":
        lines.append("  civitai    unavailable - " + str(payload["civitai"]["note"]))
    return render(f"{ident['name']}  ({ident['uid']})", lines), \
        cap_structured(payload)


# ---------------------------------------------------------------------------
# 5.4  list_node_packages
# ---------------------------------------------------------------------------

_PKG_SORTS = {"name": "name", "-classes": "-classes", "-updated": "-updated"}


def _package_row(item: dict) -> dict:
    return {
        "uid": item["uid"], "folder_name": item.get("folder_name"),
        "display_name": item.get("display_name") or item.get("folder_name") or "",
        "author": item.get("author"), "description": item.get("description"),
        "is_official": bool(item.get("is_official")),
        "enabled": bool(item.get("enabled")),
        "class_count": int(item.get("class_count") or 0),
        "extraction": {
            "status": (item.get("extraction") or {}).get("status"),
            "strategies": [str(s) for s in
                           ((item.get("extraction") or {}).get("strategies") or [])],
            "confidence": (item.get("extraction") or {}).get("confidence"),
        },
        "repo": {
            "url": (item.get("repo") or {}).get("url"),
            "suspect": bool((item.get("repo") or {}).get("suspect")),
            "branch": (item.get("repo") or {}).get("branch"),
            "commit": (item.get("repo") or {}).get("commit"),
            "commit_at": (item.get("repo") or {}).get("commit_at"),
        },
        "update": {
            "state": (item.get("update") or {}).get("state"),
            "has_update": bool((item.get("update") or {}).get("has_update")),
            "commits_behind": (item.get("update") or {}).get("commits_behind"),
        },
        "version": item.get("version"),
        "deps": [str(d) for d in ((item.get("deps") or {}).get("missing") or [])],
        "workflow_count": int((item.get("counts") or {}).get("workflows") or 0),
    }


def list_node_packages(args: dict, ctx: Ctx) -> tuple[str, dict]:
    filters: dict[str, Any] = {}
    for key in ("official", "enabled", "has_update"):
        if args.get(key) is not None:
            filters[key] = bool(args[key])
    if args.get("author"):
        filters["author"] = list(args["author"])
    if args.get("name_contains"):
        filters["q"] = str(args["name_contains"])
    lim = int(args.get("limit", 50))
    off = int(args.get("offset", 0))
    sort = _PKG_SORTS.get(str(args.get("sort") or "name"), "name")
    wanted_status = [str(s) for s in (args.get("extraction_status") or [])]

    try:
        if wanted_status:
            # The query service has no extraction_status filter and there are only
            # a few dozen packages, so page them and filter in memory rather than
            # duplicating SQL here.
            full = nodes_query.list_node_packages(filters, sort=sort, limit=ALL_ROWS,
                                                  offset=0, conn=ctx.conn)
            rows = [i for i in full.items
                    if str((i.get("extraction") or {}).get("status")) in wanted_status]
            total = len(rows)
            items = rows[off:off + lim]
        else:
            page = nodes_query.list_node_packages(filters, sort=sort, limit=lim,
                                                  offset=off, conn=ctx.conn)
            items = page.items
            total = int(page.page.get("total") or 0)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    packages = [_package_row(i) for i in items]
    payload = {"total": total, "returned": len(packages), "offset": off,
               "packages": packages}
    lines = [
        f"{off + i + 1}. {p['display_name']}  {p['class_count']} class(es)"
        + ("  [official]" if p["is_official"] else f"  by {p['author'] or 'unknown'}")
        + ("" if p["enabled"] else "  [disabled]")
        + f"  ({p['uid']})"
        for i, p in enumerate(packages)
    ]
    head = f"{total} node package(s). Showing {len(packages)} from offset {off}."
    return render(head, lines, shown=len(packages), total=total), \
        cap_structured(payload, "packages")


# ---------------------------------------------------------------------------
# 5.5  list_node_classes
# ---------------------------------------------------------------------------

def _class_row(item: dict) -> dict:
    return {
        "uid": item["uid"], "node_id": item.get("node_id") or "",
        "display_name": item.get("display_name"), "class_name": item.get("class_name"),
        "category": item.get("category"), "description": item.get("description"),
        "package": {
            "uid": (item.get("package") or {}).get("uid"),
            "name": (item.get("package") or {}).get("name"),
            "official": bool((item.get("package") or {}).get("official")),
        },
        "inputs": {"required": (item.get("inputs") or {}).get("required") or {},
                   "optional": (item.get("inputs") or {}).get("optional") or {}},
        "outputs": {"types": (item.get("outputs") or {}).get("types"),
                    "names": (item.get("outputs") or {}).get("names")},
        "output_node": bool(item.get("output_node")),
        "flags": {
            "deprecated": bool((item.get("flags") or {}).get("deprecated")),
            "experimental": bool((item.get("flags") or {}).get("experimental")),
            "api_node": bool((item.get("flags") or {}).get("api_node")),
        },
        "confidence": item.get("confidence"),
        "source": {"file": (item.get("source") or {}).get("file"),
                   "lineno": (item.get("source") or {}).get("lineno"),
                   "strategy": (item.get("source") or {}).get("strategy")},
        "workflow_count": int((item.get("counts") or {}).get("workflows") or 0),
    }


def _class_filters(args: dict) -> dict:
    filters: dict[str, Any] = {}
    if args.get("package_uid"):
        filters["package_id"] = _uid_id(args["package_uid"], "node_package")
    if args.get("category"):
        filters["category"] = list(args["category"])
    if args.get("name_contains"):
        filters["q"] = str(args["name_contains"])
    if args.get("official") is not None:
        filters["official"] = bool(args["official"])
    if not args.get("include_deprecated", False):
        filters["deprecated"] = False
    if args.get("confidence"):
        filters["confidence"] = list(args["confidence"])
    return filters


def list_node_classes(args: dict, ctx: Ctx) -> tuple[str, dict]:
    filters = _class_filters(args)
    lim = int(args.get("limit", 50))
    off = int(args.get("offset", 0))
    try:
        page = nodes_query.list_node_classes(filters, sort="display_name", limit=lim,
                                             offset=off, conn=ctx.conn)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    classes = [_class_row(i) for i in page.items]
    total = int(page.page.get("total") or 0)
    payload = {"total": total, "returned": len(classes), "offset": off,
               "classes": classes}
    lines = []
    for i, c in enumerate(classes):
        ins = ", ".join(list((c["inputs"]["required"] or {}).keys())[:6])
        outs = ", ".join([str(t) for t in (c["outputs"]["types"] or [])][:6])
        lines.append(
            f"{off + i + 1}. {c['node_id']}  [{c['category'] or 'uncategorized'}] "
            f"pkg={c['package']['name']}\n     in: {ins or '-'}  ->  out: {outs or '-'}")
    head = f"{total} node class(es). Showing {len(classes)} from offset {off}."
    return render(head, lines, shown=len(classes), total=total), \
        cap_structured(payload, "classes")


# ---------------------------------------------------------------------------
# 5.6  get_node_class
# ---------------------------------------------------------------------------

def _find_classes_by_node_id(node_id: str, package_id: int | None,
                             ctx: Ctx) -> list[dict]:
    needle = str(node_id).strip().lower()
    base: dict[str, Any] = {"deprecated": None}
    if package_id is not None:
        base = {"package_id": package_id}
    # Cheap path first: the lexical index almost always ranks an exact class id.
    hits: list[dict] = []
    try:
        probe = nodes_query.list_node_classes({**base, "q": node_id}, limit=200,
                                              offset=0, conn=ctx.conn)
        hits = [i for i in probe.items
                if str(i.get("node_id") or "").lower() == needle]
    except AppError:
        hits = []
    if hits:
        return hits
    offset = 0
    while True:
        page = nodes_query.list_node_classes(base, sort="name", limit=ALL_ROWS,
                                             offset=offset, conn=ctx.conn)
        hits.extend(i for i in page.items
                    if str(i.get("node_id") or "").lower() == needle)
        if len(page.items) < ALL_ROWS:
            return hits
        offset += ALL_ROWS
        if offset > 40_000:
            return hits


def get_node_class(args: dict, ctx: Ctx) -> tuple[str, dict]:
    node_id = str(args["node_id"]).strip()
    package_id = (_uid_id(args["package_uid"], "node_package")
                  if args.get("package_uid") else None)
    matches = _find_classes_by_node_id(node_id, package_id, ctx)
    if not matches:
        raise ToolFailure(
            f"No installed node class has the id '{node_id}'. "
            "Use list_node_classes with name_contains to browse, or inspect_workflow "
            "to get an install_hint for a class that is missing.")

    disambiguation = [
        {"uid": m["uid"], "node_id": m.get("node_id") or "",
         "package": (m.get("package") or {}).get("name"),
         "official": bool((m.get("package") or {}).get("official"))}
        for m in matches
    ] if len(matches) > 1 else []

    chosen = matches[0]
    detail = nodes_query.get_node_class(int(chosen["id"]), conn=ctx.conn) or chosen
    row = _class_row(detail)
    workflows = [
        {"uid": w.get("uid"), "name": w.get("name"),
         "occurrences": int(w.get("occurrences") or 1)}
        for w in (detail.get("workflows_using") or [])
    ]
    similar: list[dict] = []
    if row["category"]:
        page = nodes_query.list_node_classes({"category": [row["category"]]},
                                             limit=25, offset=0, conn=ctx.conn)
        similar = [{"uid": i["uid"], "node_id": i.get("node_id") or "",
                    "display_name": i.get("display_name"),
                    "category": i.get("category")}
                   for i in page.items if i["uid"] != row["uid"]][:12]

    payload = {"node_class": row, "disambiguation": disambiguation,
               "workflows_using": workflows, "similar_classes": similar}
    req = (row["inputs"]["required"] or {})
    opt = (row["inputs"]["optional"] or {})
    lines = [
        f"  package    {row['package']['name']} "
        f"({'official' if row['package']['official'] else 'custom'})",
        f"  category   {row['category']}   confidence {row['confidence']}",
        f"  required   {', '.join(list(req.keys())) or '-'}",
        f"  optional   {', '.join(list(opt.keys())) or '-'}",
        f"  outputs    {', '.join(str(t) for t in (row['outputs']['types'] or [])) or '-'}",
        f"  used by    {len(workflows)} workflow(s)",
    ]
    if disambiguation:
        lines.append(f"  NOTE       {len(disambiguation)} packages register this id: "
                     + ", ".join(str(d["package"]) for d in disambiguation))
    return render(f"{row['node_id']}  ({row['uid']})", lines), cap_structured(payload)


# ---------------------------------------------------------------------------
# 5.7  list_workflows
# ---------------------------------------------------------------------------

_WF_SORTS = {"name": "name", "-name": "-name", "modified": "modified",
             "-modified": "-modified", "size": "size", "-size": "-size",
             "nodes": "nodes", "-nodes": "-nodes", "missing": "missing",
             "-missing": "-missing"}


def _workflow_row(item: dict) -> dict:
    counts = item.get("counts") or {}
    return {
        "uid": item["uid"], "name": item.get("name") or "",
        "rel_path": item.get("rel_path"), "folder": item.get("folder"),
        "format": item.get("format"), "title": item.get("title"),
        "description": item.get("description"),
        "capability_tags": [str(t) for t in (item.get("capability_tags") or [])],
        "base_model": item.get("base_model"), "modality": item.get("modality"),
        "node_count": int(counts.get("nodes") or 0),
        "missing_node_count": int(counts.get("missing_nodes") or 0),
        "missing_model_count": int(counts.get("missing_models") or 0),
        "is_runnable": bool(item.get("is_runnable")),
        "prompt_summary": item.get("prompt_summary"),
        "modified_at": item.get("modified_at"),
        "size_bytes": int(item.get("size") or 0),
    }


def list_workflows(args: dict, ctx: Ctx) -> tuple[str, dict]:
    filters: dict[str, Any] = {}
    if args.get("name_contains"):
        filters["q"] = str(args["name_contains"])
    if args.get("folder"):
        filters["folder"] = str(args["folder"])
    if args.get("base_model"):
        filters["base_model"] = list(args["base_model"])
    if args.get("runnable") is not None:
        filters["runnable"] = bool(args["runnable"])
    if args.get("uses_node_class"):
        filters["node_class"] = str(args["uses_node_class"])
    if args.get("uses_model_uid"):
        filters["model_id"] = _uid_id(args["uses_model_uid"], "model")
    lim = int(args.get("limit", 50))
    off = int(args.get("offset", 0))
    sort = _WF_SORTS.get(str(args.get("sort") or "-modified"), "-modified")
    tag = args.get("capability_tag")

    try:
        if tag:
            full = workflows_query.list_workflows(filters, sort=sort, limit=ALL_ROWS,
                                                  offset=0, conn=ctx.conn)
            rows = [i for i in full.items
                    if str(tag).lower() in
                    [str(t).lower() for t in (i.get("capability_tags") or [])]]
            total = len(rows)
            items = rows[off:off + lim]
        else:
            page = workflows_query.list_workflows(filters, sort=sort, limit=lim,
                                                  offset=off, conn=ctx.conn)
            items = page.items
            total = int(page.page.get("total") or 0)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    workflows = [_workflow_row(i) for i in items]
    payload = {"total": total, "returned": len(workflows), "offset": off,
               "workflows": workflows}
    lines = []
    for i, w in enumerate(workflows):
        if w["is_runnable"]:
            state = "runnable"
        else:
            state = (f"BLOCKED ({w['missing_node_count']} node(s) / "
                     f"{w['missing_model_count']} model(s) missing)")
        lines.append(f"{off + i + 1}. {w['name']}  {w['node_count']} nodes  {state}"
                     f"  ({w['uid']})")
    head = f"{total} workflow(s). Showing {len(workflows)} from offset {off}."
    return render(head, lines, shown=len(workflows), total=total), \
        cap_structured(payload, "workflows")


# ---------------------------------------------------------------------------
# 5.8  inspect_workflow
# ---------------------------------------------------------------------------

def inspect_workflow(args: dict, ctx: Ctx) -> tuple[str, dict]:
    workflow_id = _resolve_workflow_id(args, ctx)
    detail = workflows_query.get_workflow(workflow_id, conn=ctx.conn)
    if detail is None:
        raise ToolFailure(f"workflow:{workflow_id} does not exist.")
    deps = workflows_query.workflow_dependencies(workflow_id, conn=ctx.conn)

    counts = detail.get("counts") or {}
    wf = {
        "uid": detail["uid"], "name": detail.get("name") or "",
        "rel_path": detail.get("rel_path"), "format": detail.get("format"),
        "description": detail.get("description"),
        "capability_tags": [str(t) for t in (detail.get("capability_tags") or [])],
        "base_model": detail.get("base_model"), "modality": detail.get("modality"),
        "node_count": int(counts.get("nodes") or 0),
        "is_runnable": bool(detail.get("is_runnable")),
    }
    nodes = [
        {"class_type": b.get("class_type") or "", "count": int(b.get("count") or 1),
         "status": "satisfied" if b.get("resolved") else "missing",
         "package": b.get("package")}
        for b in (detail.get("node_breakdown") or [])
    ]

    summary = dict(deps.get("summary") or {})
    summary.setdefault("unknown", 0)
    dep_models = []
    for m in deps.get("models") or []:
        via = (m.get("via") or [{}])[0]
        dep_models.append({
            "ref_name": m.get("ref_name"), "category": m.get("category"),
            "via_class": via.get("class"), "via_input": via.get("input"),
            "status": str(m.get("status") or "unknown"), "uid": m.get("uid"),
            "occurrences": int(m.get("occurrences") or 1),
            "suggestions": [
                {"uid": s.get("uid"), "name": s.get("name"),
                 "similarity": float(s.get("score") or 0.0)}
                for s in (m.get("suggestions") or [])
            ],
        })
    dep_nodes = []
    for n in deps.get("nodes") or []:
        hint = n.get("registry_hint")
        dep_nodes.append({
            "class_type": n.get("class_type"), "status": str(n.get("status") or "unknown"),
            "uid": n.get("uid"), "occurrences": int(n.get("occurrences") or 1),
            "package": n.get("package"),
            "install_hint": ({"package": hint.get("package"),
                              "repo_url": hint.get("repo_url"),
                              "source": "comfyui_manager_registry"} if hint else None),
        })

    blockers: list[str] = []
    missing_models = [m for m in dep_models if m["status"] == "missing"]
    missing_nodes = [n for n in dep_nodes if n["status"] == "missing"]
    if missing_models:
        blockers.append(f"{len(missing_models)} model file(s) are missing")
    if missing_nodes:
        blockers.append(f"{len(missing_nodes)} node class(es) are not installed")

    graph = None
    if args.get("include_graph"):
        raw = workflows_query.workflow_graph(workflow_id, conn=ctx.conn)
        if isinstance(raw, dict):
            max_nodes = int(args.get("max_nodes", 200))
            graph = dict(raw)
            node_list = graph.get("nodes")
            if isinstance(node_list, list) and len(node_list) > max_nodes:
                graph["nodes"] = node_list[:max_nodes]
                graph["_truncated_nodes"] = len(node_list) - max_nodes

    payload = {
        "workflow": wf,
        "prompts": {"positive": detail.get("positive_prompt"),
                    "negative": detail.get("negative_prompt"),
                    "unresolved_count": int(detail.get("unresolved_inputs") or 0)},
        "nodes": nodes,
        "dependencies": {"summary": summary, "models": dep_models, "nodes": dep_nodes,
                         "embeddings": [
                             {"ref_name": e.get("ref_name"),
                              "status": str(e.get("status") or "unknown")}
                             for e in (deps.get("embeddings") or [])],
                         "input_files": [
                             {"ref_name": e.get("ref_name"),
                              "status": str(e.get("status") or "unknown")}
                             for e in (deps.get("input_files") or [])]},
        "can_run": bool(wf["is_runnable"]) and not blockers,
        "blockers": blockers,
        "graph": graph,
    }

    lines = [
        f"  path       {wf['rel_path']}  ({wf['format']})",
        f"  base model {wf['base_model']}   modality {wf['modality']}   "
        f"{wf['node_count']} nodes",
        f"  tags       {', '.join(wf['capability_tags']) or '-'}",
        f"  deps       {summary.get('total', 0)} total / "
        f"{summary.get('satisfied', 0)} satisfied / {summary.get('missing', 0)} missing",
        f"  runnable   {'yes' if payload['can_run'] else 'NO - ' + '; '.join(blockers)}",
    ]
    if detail.get("positive_prompt"):
        lines.append(f"  positive   {clip(detail.get('positive_prompt'), 160)}")
    if missing_models:
        lines.append("  MISSING MODELS:")
        for m in missing_models[:15]:
            sug = m["suggestions"][0]["name"] if m["suggestions"] else None
            lines.append(f"    - {m['ref_name']} ({m['category']}, via "
                         f"{m['via_class']}.{m['via_input']})"
                         + (f"  did you mean {sug}?" if sug else ""))
    if missing_nodes:
        lines.append("  MISSING NODE CLASSES:")
        for n in missing_nodes[:20]:
            hint = n.get("install_hint")
            lines.append(f"    - {n['class_type']}"
                         + (f"  -> install {hint['package']} ({hint['repo_url']})"
                            if hint else "  -> no registry match"))
    return render(f"{wf['name']}  ({wf['uid']})", lines), cap_structured(payload)


# ---------------------------------------------------------------------------
# 5.9  find_model_usage
# ---------------------------------------------------------------------------

def find_model_usage(args: dict, ctx: Ctx) -> tuple[str, dict]:
    model_id = _resolve_model_id(args, ctx)
    item = models_query.get_model(model_id, conn=ctx.conn)
    if item is None:
        raise ToolFailure(f"model:{model_id} does not exist.")
    lim = int(args.get("limit", 25))
    usage = models_query.model_usage(model_id, limit=lim, offset=0, conn=ctx.conn)

    workflows = [
        {"uid": w.get("uid"), "name": w.get("name"), "rel_path": w.get("rel_path"),
         "occurrences": int(w.get("occurrences") or 1),
         "via": [{"class": v.get("class"), "input": v.get("input")}
                 for v in (w.get("via") or [])],
         "match_method": w.get("match_method")}
        for w in (usage.get("workflows") or [])
    ]
    outputs_block = {"count": 0, "recent": []}
    if args.get("include_outputs", True):
        outputs_block = {
            "count": int((usage.get("outputs") or {}).get("count") or 0),
            "recent": [
                {"uid": o.get("uid"), "filename": o.get("filename"),
                 "created_at": o.get("created_at")}
                for o in ((usage.get("outputs") or {}).get("recent") or [])
            ],
        }

    # Co-occurrence is computed from the same dependency service the REST layer
    # uses - no second query path, no SQL here.
    tally: dict[str, dict] = {}
    for wf in workflows[:25]:
        try:
            wid = _uid_id(str(wf["uid"]), "workflow")
        except ToolFailure:
            continue
        deps = workflows_query.workflow_dependencies(wid, conn=ctx.conn)
        for dep in deps.get("models") or []:
            if dep.get("uid") == f"model:{model_id}":
                continue
            key = str(dep.get("uid") or dep.get("ref_name") or "")
            if not key:
                continue
            entry = tally.setdefault(key, {"uid": dep.get("uid"),
                                           "ref_name": str(dep.get("ref_name") or key),
                                           "shared_workflows": 0})
            entry["shared_workflows"] += 1
    co = sorted(tally.values(), key=lambda e: -e["shared_workflows"])[:25]

    payload = {
        "model": {"uid": item["uid"], "name": item.get("name") or ""},
        "workflows": workflows, "outputs": outputs_block,
        "co_occurring_models": co,
    }
    lines = [f"  {len(workflows)} workflow(s), {outputs_block['count']} output(s)"]
    for w in workflows[:25]:
        via = ", ".join(f"{v['class']}.{v['input']}" for v in w["via"][:3])
        lines.append(f"    - {w['name']}  x{w['occurrences']}  via {via}  ({w['uid']})")
    if co:
        lines.append("  co-occurring models:")
        lines.extend(f"    - {c['ref_name']}  in {c['shared_workflows']} shared "
                     "workflow(s)" for c in co[:15])
    return render(f"Usage of {item.get('name')} ({item['uid']})", lines), \
        cap_structured(payload, "workflows")


# ---------------------------------------------------------------------------
# 5.10  query_outputs
# ---------------------------------------------------------------------------

_OUT_SORTS = {"created": "created", "-created": "-created", "modified": "modified",
              "-modified": "-modified", "name": "name", "-name": "-name",
              "size": "size", "-size": "-size", "rating": "rating",
              "-rating": "-rating", "width": "width", "-width": "-width",
              "height": "height", "-height": "-height"}

_PROVENANCE_FIELDS = ("positive_prompt", "negative_prompt", "seed", "steps", "cfg",
                      "sampler", "scheduler", "denoise", "model_name", "width",
                      "height")


def _output_filters(args: dict) -> dict:
    filters: dict[str, Any] = {}
    terms = [str(args[k]) for k in ("prompt_contains", "negative_contains",
                                    "model_name_contains") if args.get(k)]
    if terms:
        filters["q"] = " ".join(terms)
    if args.get("model_uid"):
        filters["model_id"] = _uid_id(args["model_uid"], "model")
    if args.get("workflow_uid"):
        filters["workflow_id"] = _uid_id(args["workflow_uid"], "workflow")
    if args.get("media_kind"):
        filters["media_kind"] = list(args["media_kind"])
    if args.get("sampler"):
        filters["sampler"] = list(args["sampler"])
    for src, dst in (("seed", "seed"), ("steps_min", "steps_min"),
                     ("steps_max", "steps_max"), ("cfg_min", "cfg_min"),
                     ("cfg_max", "cfg_max"), ("width_min", "width_min"),
                     ("width_max", "width_max"), ("height_min", "height_min"),
                     ("height_max", "height_max"), ("folder", "folder"),
                     ("favorite", "favorite"), ("min_rating", "min_rating"),
                     ("has_metadata", "has_metadata"),
                     ("created_after", "date_from"), ("created_before", "date_to")):
        if args.get(src) is not None:
            filters[dst] = args[src]
    return filters


def _unresolved_fields(detail: dict) -> list[str]:
    prov = detail.get("provenance") or {}
    out = []
    for key in _PROVENANCE_FIELDS:
        spec = prov.get(key)
        if isinstance(spec, dict) and spec.get("resolved") is False:
            out.append(key)
    return out


def query_outputs(args: dict, ctx: Ctx) -> tuple[str, dict]:
    filters = _output_filters(args)
    lim = int(args.get("limit", 25))
    off = int(args.get("offset", 0))
    sort = _OUT_SORTS.get(str(args.get("sort") or "-created"), "-created")
    try:
        page = outputs_query.list_outputs(filters, sort=sort, limit=lim, offset=off,
                                          conn=ctx.conn)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    rows = []
    for item in page.items:
        detail = outputs_query.get_output(int(item["id"]), conn=ctx.conn) or {}
        rows.append({
            "uid": item["uid"], "filename": item.get("filename") or "",
            "rel_path": item.get("rel_path"), "media_kind": item.get("media_kind"),
            "width": item.get("width"), "height": item.get("height"),
            "duration_ms": item.get("duration_ms"),
            "size_bytes": int(item.get("size") or 0),
            "created_at": item.get("created_at"),
            "positive_prompt": item.get("positive_prompt"),
            "negative_prompt": detail.get("negative_prompt"),
            "seed": item.get("seed"), "steps": item.get("steps"),
            "cfg": item.get("cfg"), "sampler": item.get("sampler"),
            "scheduler": item.get("scheduler"), "model_name": item.get("model_name"),
            "model_uid": item.get("model_uid"),
            "workflow_uid": item.get("workflow_uid"),
            "loras": [{"name": lo.get("name"), "strength": lo.get("strength"),
                       "uid": lo.get("uid")} for lo in (detail.get("loras") or [])],
            "unresolved_fields": _unresolved_fields(detail),
        })

    total = int(page.page.get("total") or 0)
    payload = {"total": total, "returned": len(rows), "offset": off, "outputs": rows}
    lines = [
        f"{off + i + 1}. {r['filename']}  {r['width']}x{r['height']} "
        f"{r['media_kind']}  seed={r['seed']} steps={r['steps']} cfg={r['cfg']} "
        f"sampler={r['sampler']}  ({r['uid']})"
        + (f"\n     prompt: {clip(r['positive_prompt'], 120)}"
           if r.get("positive_prompt") else "")
        + (f"\n     unresolved: {', '.join(r['unresolved_fields'])}"
           if r["unresolved_fields"] else "")
        for i, r in enumerate(rows)
    ]
    head = f"{total} output(s). Showing {len(rows)} from offset {off}."
    return render(head, lines, shown=len(rows), total=total), \
        cap_structured(payload, "outputs")


# ---------------------------------------------------------------------------
# 5.11  vault_stats
# ---------------------------------------------------------------------------

def _capabilities(cfg) -> dict:
    from ..search import vec

    try:
        semantic_ok, reason = vec.available()
    except Exception as exc:  # noqa: BLE001 - a missing optional dep is not an error
        semantic_ok, reason = False, str(exc)[:200]
    return {
        "semantic_search": bool(semantic_ok and cfg.smart_search_enabled),
        "semantic_reason": reason,
        "civitai": bool(cfg.online_enabled and cfg.civitai_enabled),
        "ollama": bool(cfg.ollama_enabled),
        "hashing_available": True,
        "mutations_enabled": not bool(cfg.mcp_read_only),
    }


def vault_stats(args: dict, ctx: Ctx) -> tuple[str, dict]:
    stats = core_vault_stats(ctx.conn)
    cfg = config_service.get_config()

    thumb_bytes = 0
    try:
        from ..jobs.thumb_service import get_thumb_service

        thumb_bytes = int(get_thumb_service().stats().get("bytes") or 0)
    except Exception as exc:  # noqa: BLE001 - the thumbnail cache is optional
        log.debug("thumb cache size unavailable: %s", exc)

    breakdowns: dict[str, Any] = {}
    for name in (args.get("breakdown") or []):
        try:
            if name == "media_kind":
                facets = outputs_query.output_facets({}, conn=ctx.conn)
                breakdowns[name] = [
                    {"value": r.get("value"), "count": int(r.get("count") or 0)}
                    for r in (facets.get("media_kind") or [])]
            else:
                groups = models_query.model_groups({}, name, conn=ctx.conn)
                breakdowns[name] = [
                    {"value": g.get("key"), "count": int(g.get("count") or 0),
                     "bytes": int(g.get("bytes") or 0)} for g in groups]
        except AppError as exc:
            raise ToolFailure(exc.message, code=exc.code) from exc

    payload = {
        "counts": {
            "models": int(stats.get("models") or 0),
            "model_files": int(stats.get("model_files") or 0),
            "models_hashed": int(stats.get("models_hashed") or 0),
            "node_packages": int(stats.get("node_packages") or 0),
            "node_classes": int(stats.get("node_classes") or 0),
            "workflows": int(stats.get("workflows") or 0),
            "workflows_broken": int(stats.get("workflows_broken") or 0),
            "outputs": int(stats.get("outputs") or 0),
            "embedded": int(stats.get("embedded") or 0),
            "integrity_issues": int(stats.get("integrity_issues") or 0),
            "missing": int(stats.get("missing") or 0),
            "scan_errors": int(stats.get("scan_errors") or 0),
        },
        "disk": {
            "models_bytes": int(stats.get("models_bytes") or 0),
            "outputs_bytes": int(stats.get("outputs_bytes") or 0),
            "thumb_cache_bytes": thumb_bytes,
            "db_bytes": int((stats.get("db") or {}).get("size_bytes") or 0),
        },
        "roots": [
            {"id": int(r.id), "kind": r.kind, "path": r.path, "label": r.label,
             "available": os.path.isdir(r.path), "category": r.category}
            for r in cfg.roots
        ],
        "last_scan": stats.get("last_scan"),
        "capabilities": _capabilities(cfg),
        "breakdowns": breakdowns,
    }
    c, d = payload["counts"], payload["disk"]
    lines = [
        f"  models       {c['models']:>6}  ({human_bytes(d['models_bytes'])}, "
        f"{c['models_hashed']} hashed)",
        f"  node pkgs    {c['node_packages']:>6}  with {c['node_classes']} node classes",
        f"  workflows    {c['workflows']:>6}  ({c['workflows_broken']} with missing deps)",
        f"  outputs      {c['outputs']:>6}  ({human_bytes(d['outputs_bytes'])})",
        f"  integrity    {c['integrity_issues']} issue(s), {c['missing']} missing file(s), "
        f"{c['scan_errors']} scan error(s)",
        "  roots        " + "; ".join(str(r["path"]) for r in payload["roots"]),
        f"  capabilities semantic={payload['capabilities']['semantic_search']} "
        f"civitai={payload['capabilities']['civitai']} "
        f"ollama={payload['capabilities']['ollama']} "
        f"mutations={payload['capabilities']['mutations_enabled']}",
    ]
    for name, rows in breakdowns.items():
        lines.append(f"  {name}: " + ", ".join(
            f"{r.get('value') or 'unknown'}={r.get('count')}" for r in rows[:20]))
    return render("Vault statistics", lines), cap_structured(payload)


# ---------------------------------------------------------------------------
# 5.12  get_index_status
# ---------------------------------------------------------------------------

def get_index_status(args: dict, ctx: Ctx) -> tuple[str, dict]:
    from ..indexing.service import get_indexer
    from ..jobs.embed_service import get_embed_service
    from ..jobs.hash_service import get_hash_service

    cfg = config_service.get_config()
    scan = get_indexer().status()
    hashes = get_hash_service().status()
    embed = get_embed_service().status()
    stats = core_vault_stats(ctx.conn)
    last = stats.get("last_scan") or {}
    states = hashes.get("states") or {}

    hashed = int(states.get("done", 0))
    queued = int(states.get("queued", 0))
    running = int(states.get("running", 0))
    failed = int(states.get("failed", 0))
    denom = hashed + queued + running + failed

    issues: list[str] = []
    if not cfg.is_configured:
        issues.append("ComfyUI path is not configured; run the setup wizard.")
    if int(stats.get("integrity_issues") or 0):
        issues.append(f"{stats['integrity_issues']} model file(s) failed the "
                      "integrity check.")
    if int(stats.get("missing") or 0):
        issues.append(f"{stats['missing']} indexed file(s) are no longer on disk.")
    if int(stats.get("scan_errors") or 0):
        issues.append(f"{stats['scan_errors']} item(s) errored during the last scan.")
    if not int(stats.get("models") or 0):
        issues.append("No models are indexed yet - has a scan run?")
    status = "error" if not cfg.is_configured else ("degraded" if issues else "ok")

    payload = {
        "configured": bool(cfg.is_configured),
        "comfyui_path": str(cfg.comfyui_path) if cfg.comfyui_path else None,
        "scan": {
            "active": bool(scan.get("running")),
            "phase": scan.get("phase"),
            "done": int(scan.get("items_done") or 0),
            "total": int(scan.get("items_total") or 0),
            "last_completed_at": last.get("finished_at"),
            "error_count": int(scan.get("errors") or last.get("error_count") or 0),
        },
        "hash": {"queued": queued, "running": running, "done": hashed,
                 "failed": failed,
                 "percent": round(hashed / denom * 100, 1) if denom else 0.0},
        "embeddings": {"state": embed.get("state"),
                       "embedded": int(embed.get("embedded") or 0),
                       "pending": int(embed.get("queued") or 0),
                       "reason": embed.get("reason")},
        "health": {"status": status, "issues": issues},
    }
    lines = [
        f"  configured {payload['configured']}  ({payload['comfyui_path']})",
        f"  scan       active={payload['scan']['active']} phase={payload['scan']['phase']} "
        f"{payload['scan']['done']}/{payload['scan']['total']}",
        f"  hashing    queued={queued} running={running} done={hashed} failed={failed} "
        f"({payload['hash']['percent']}%)",
        f"  embeddings {payload['embeddings']['state']} "
        f"({payload['embeddings']['embedded']} embedded)"
        + (f" - {payload['embeddings']['reason']}"
           if payload["embeddings"].get("reason") else ""),
        f"  health     {status}",
    ]
    lines.extend(f"    ! {i}" for i in issues)
    return render("Index status", lines), cap_structured(payload)


# ---------------------------------------------------------------------------
# 5.13  vault_reindex  (scan job control)
# ---------------------------------------------------------------------------

def vault_reindex(args: dict, ctx: Ctx) -> tuple[str, dict]:
    from ..indexing.service import get_indexer

    indexer = get_indexer()
    mode = str(args.get("mode") or "incremental")
    phases = list(args.get("phases") or []) or None
    try:
        job_id = indexer.start(mode=mode, phases=phases, trigger="mcp")
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    payload: dict[str, Any] = {
        "job_id": int(job_id), "started": True, "mode": mode, "state": None,
        "message": f"Scan job {job_id} started ({mode}).",
    }
    if args.get("wait"):
        _wait_for_scan(indexer, int(job_id), ctx)
        payload["state"] = indexer.status().get("status")
        payload["message"] = f"Scan job {job_id} finished ({payload['state']})."
    return payload["message"], payload


def _wait_for_scan(indexer, job_id: int, ctx: Ctx, timeout_s: float = 900.0) -> None:
    """Poll the indexer and emit notifications/progress until it finishes."""
    deadline = time.monotonic() + timeout_s
    last = -1
    while time.monotonic() < deadline:
        state = indexer.status()
        if not state.get("running"):
            return
        done = int(state.get("items_done") or 0)
        total = int(state.get("items_total") or 0)
        if done != last and ctx.progress is not None:
            last = done
            try:
                ctx.progress(done, total, str(state.get("phase") or ""))
            except Exception as exc:  # noqa: BLE001 - progress is best effort
                log.debug("progress emit failed: %s", exc)
        time.sleep(0.4)


# ---------------------------------------------------------------------------
# DECISIONS C5.2 trailer - hash and embedding job control
# ---------------------------------------------------------------------------

#: Contract scope -> the vocabulary HashService._scope_where understands.  This
#: mirrors hash_router.SCOPE_MAP exactly; the enqueue call below is the same one
#: POST /api/v1/hash/enqueue makes.
_HASH_SCOPES = {"all": "all", "unhashed": "unhashed_only", "ids": "ids"}


def vault_hash_enqueue(args: dict, ctx: Ctx) -> tuple[str, dict]:
    from ..jobs.hash_service import get_hash_service

    scope_in = str(args.get("scope") or "unhashed")
    uids = [str(u) for u in (args.get("uids") or [])]
    if scope_in == "category":
        if not args.get("category"):
            raise ToolFailure("scope='category' needs a 'category' argument, "
                              "for example 'loras'.")
        scope = f"category:{args['category']}"
    elif scope_in == "folder":
        if not args.get("folder"):
            raise ToolFailure("scope='folder' needs a 'folder' argument.")
        scope = f"folder:{args['folder']}"
    elif scope_in == "ids":
        if not uids:
            raise ToolFailure("scope='ids' needs a 'uids' array of model uids.")
        _require_batch(uids)
        scope = "ids"
    else:
        scope = _HASH_SCOPES[scope_in]

    try:
        res = get_hash_service().enqueue(
            scope, uids=uids or None, priority=int(args.get("priority", 5)),
            root_id=args.get("root_id"))
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    queued = int(res.get("queued") or 0)
    total_bytes = int(res.get("bytes_total") or 0)
    payload = {
        "batch_id": res.get("batch_id"), "queued": queued,
        "bytes_total": total_bytes, "eta_ms": res.get("eta_ms"),
        "scope": scope_in, "audit_id": None,
        "message": (f"Queued {queued} file(s) ({human_bytes(total_bytes)}) for "
                    f"hashing as batch {res.get('batch_id')}."
                    if queued else
                    f"Nothing to queue for scope {scope_in!r}: everything in scope "
                    "is already hashed, or the scope matched no files."),
    }
    text = payload["message"]
    if queued and payload["eta_ms"]:
        text += (f" Rough ETA {int(payload['eta_ms']) // 60000} min. "
                 "Poll get_index_status for progress.")
    return text, payload


def vault_hash_cancel(args: dict, ctx: Ctx) -> tuple[str, dict]:
    from ..jobs.hash_service import get_hash_service

    uids = [str(u) for u in (args.get("uids") or [])]
    if uids:
        _require_batch(uids)
    batch_id = args.get("batch_id")
    try:
        res = get_hash_service().cancel(batch_id, uids or None)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    cancelled = int(res.get("cancelled") or 0)
    if batch_id:
        target = f"batch {batch_id}"
    elif uids:
        target = f"{len(uids)} model(s)"
    else:
        target = "the whole queue"
    payload = {"cancelled": cancelled, "batch_id": res.get("batch_id"),
               "audit_id": None,
               "message": f"Cancelled {cancelled} queued hash job(s) in {target}. "
                          "Hashes that were already computed are kept."}
    return payload["message"], payload


def vault_embeddings_rebuild(args: dict, ctx: Ctx) -> tuple[str, dict]:
    from ..jobs.embed_service import get_embed_service

    service = get_embed_service()
    kinds = list(args.get("kinds") or []) or None
    try:
        state = service.rebuild(kinds, bool(args.get("force", False)))
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    status = service.status()
    if state == "unavailable":
        raise ToolFailure(
            "The embedding model is not installed, so there is nothing to rebuild "
            f"({status.get('reason') or 'not installed'}). Enable smart search from "
            "Settings first; vault_search keeps working in lexical mode meanwhile.",
            code="FEATURE_UNAVAILABLE")

    payload = {
        "state": state, "started": state == "started",
        "embedded": int(status.get("embedded") or 0),
        "pending": int(status.get("queued") or 0),
        "audit_id": None,
        "message": ("Embedding rebuild started." if state == "started"
                    else "An embedding rebuild is already running."
                    if state == "running" else f"Embedding rebuild: {state}."),
    }
    text = (f"{payload['message']} {payload['embedded']} document(s) embedded, "
            f"{payload['pending']} pending. Poll get_index_status for progress.")
    return text, payload


# ---------------------------------------------------------------------------
# DECISIONS C5.2 - file operations
# ---------------------------------------------------------------------------

def _op_dict(res: file_ops.OpResult) -> dict:
    out = {"uid": res.uid, "ok": bool(res.ok), "path": None, "name": None,
           "mode": None, "trash_id": None, "trash_path": None, "uid_restored": None,
           "unchanged": False, "sidecars": None, "error_code": None,
           "error_message": None}
    if res.ok:
        details = res.details or {}
        out["path"] = details.get("path")
        out["name"] = details.get("name")
        out["mode"] = details.get("mode")
        out["trash_id"] = details.get("trash_id")
        out["trash_path"] = details.get("trash_path")
        out["uid_restored"] = details.get("uid_restored")
        out["unchanged"] = bool(details.get("unchanged"))
        out["sidecars"] = details.get("sidecars")
    else:
        out["error_code"] = res.code
        out["error_message"] = res.message
    return out


def _batch_payload(results: list[file_ops.OpResult]) -> dict:
    rows = [_op_dict(r) for r in results]
    ok = sum(1 for r in rows if r["ok"])
    return {"requested": len(rows), "affected": ok, "failed": len(rows) - ok,
            "results": rows, "audit_id": None}


def _batch_text(verb: str, payload: dict) -> str:
    lines = []
    for r in payload["results"][:60]:
        if r["ok"]:
            lines.append(f"  ok    {r['uid']}  {r.get('path') or r.get('mode') or ''}")
        else:
            lines.append(f"  FAIL  {r['uid']}  {r['error_code']}: "
                         f"{clip(r['error_message'], 120)}")
    head = (f"{verb}: {payload['affected']} succeeded, {payload['failed']} failed "
            f"of {payload['requested']}.")
    return render(head, lines, shown=len(lines), total=len(payload["results"]))


def _require_batch(uids: list) -> list[str]:
    if len(uids) > registry.MAX_BATCH:
        raise ToolFailure(
            f"A single mutating call may affect at most {registry.MAX_BATCH} items; "
            f"{len(uids)} were supplied. Page the call.",
            code="PAYLOAD_TOO_LARGE",
            data={"requested": len(uids), "max": registry.MAX_BATCH})
    return [str(u) for u in uids]


def vault_rename(args: dict, ctx: Ctx) -> tuple[str, dict]:
    res = file_ops.rename(
        str(args["uid"]), str(args["new_name"]),
        keep_extension=bool(args.get("keep_extension", True)),
        rename_sidecars=bool(args.get("rename_sidecars", True)))
    payload = _batch_payload([res])
    return _batch_text("Rename", payload), payload


def vault_move(args: dict, ctx: Ctx) -> tuple[str, dict]:
    uids = _require_batch(list(args["uids"]))
    try:
        results = file_ops.move(
            uids, int(args["target_root_id"]), str(args["target_folder"]),
            create_missing=bool(args.get("create_missing", True)),
            on_conflict=str(args.get("on_conflict") or "fail"))
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc
    payload = _batch_payload(results)
    return _batch_text("Move", payload), payload


def vault_delete(args: dict, ctx: Ctx) -> tuple[str, dict]:
    uids = _require_batch(list(args["uids"]))
    mode = str(args.get("mode") or "trash")
    confirm = bool(args.get("confirm", False))
    if mode == "permanent" and not confirm:
        raise ToolFailure(
            "Permanent deletion requires confirm=true. Nothing was deleted. "
            "Use mode='trash' (the default) to keep the files recoverable.",
            code="VALIDATION_ERROR",
            data={"mode": mode, "uids": len(uids)})
    try:
        results = file_ops.delete(uids, mode=mode, confirm=confirm)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc
    payload = _batch_payload(results)
    return _batch_text(f"Delete ({mode})", payload), payload


def vault_trash_list(args: dict, ctx: Ctx) -> tuple[str, dict]:
    lim = int(args.get("limit", 50))
    off = int(args.get("offset", 0))
    data = file_ops.trash_list(limit=lim, offset=off, conn=ctx.conn)
    items = [
        {"id": int(r.get("id")), "uid": r.get("uid"), "kind": r.get("kind"),
         "original_path": r.get("original_path"), "trash_path": r.get("trash_path"),
         "size_bytes": int(r.get("size") or 0), "deleted_at": r.get("deleted_at"),
         "purge_after": r.get("purge_after")}
        for r in data.get("items") or []
    ]
    total = int((data.get("page") or {}).get("total") or 0)
    payload = {"total": total, "returned": len(items), "offset": off, "items": items}
    lines = [f"  {i['id']}  {i['uid']}  {os.path.basename(i['original_path'] or '')}  "
             f"{human_bytes(i['size_bytes'])}" for i in items]
    return render(f"{total} item(s) in the trash.", lines, shown=len(items),
                  total=total), cap_structured(payload, "items")


def vault_trash_restore(args: dict, ctx: Ctx) -> tuple[str, dict]:
    ids = list(args["ids"])
    if len(ids) > registry.MAX_BATCH:
        raise ToolFailure(
            f"A single mutating call may affect at most {registry.MAX_BATCH} items.",
            code="PAYLOAD_TOO_LARGE")
    try:
        results = file_ops.trash_restore([int(i) for i in ids],
                                         on_conflict=str(args.get("on_conflict")
                                                         or "keep_both"))
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc
    payload = _batch_payload(results)
    return _batch_text("Restore", payload), payload


def vault_trash_empty(args: dict, ctx: Ctx) -> tuple[str, dict]:
    if not args.get("confirm"):
        raise ToolFailure(
            "Emptying the trash is irreversible and requires confirm=true. "
            "Nothing was removed.", code="VALIDATION_ERROR")
    ids = [int(i) for i in (args.get("ids") or [])]
    if len(ids) > registry.MAX_BATCH:
        raise ToolFailure(
            f"A single mutating call may affect at most {registry.MAX_BATCH} items.",
            code="PAYLOAD_TOO_LARGE")
    try:
        res = file_ops.trash_empty(ids or None, args.get("older_than_days"),
                                   confirm=True)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc
    payload = {"removed": int(res.get("removed") or 0),
               "files_removed": int(res.get("files_removed") or 0),
               "audit_id": None}
    return (f"Purged {payload['removed']} trash entry(ies); "
            f"{payload['files_removed']} folder(s) erased from disk."), payload


def vault_create_folder(args: dict, ctx: Ctx) -> tuple[str, dict]:
    try:
        res = file_ops.create_folder(int(args["root_id"]), str(args["folder"]))
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc
    payload = {"root_id": int(res["root_id"]), "folder": str(res["folder"]),
               "abs_path": str(res["abs_path"]), "audit_id": None}
    return f"Created {payload['abs_path']}", payload


def vault_assign_tags(args: dict, ctx: Ctx) -> tuple[str, dict]:
    uids = _require_batch(list(args["uids"]))
    add = [str(t) for t in (args.get("add") or [])]
    remove = [str(t) for t in (args.get("remove") or [])]
    if not add and not remove:
        raise ToolFailure("Supply at least one tag in 'add' or 'remove'.")
    try:
        res = tags_query.assign_tags(uids, add=add, remove=remove)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc
    payload = {"requested": len(uids), "updated": int(res.get("updated") or 0),
               "added": add, "removed": remove, "audit_id": None}
    return (f"Tagged {len(uids)} asset(s): +[{', '.join(add) or '-'}] "
            f"-[{', '.join(remove) or '-'}]"), payload


# ---------------------------------------------------------------------------
# REQUIREMENTS_R2 C9 - workflow "Enable"
# ---------------------------------------------------------------------------

def enable_workflow_plan(args: dict, ctx: Ctx) -> tuple[str, dict]:
    from ..enable import service as enable_service

    workflow_id = _uid_id(str(args["workflow_uid"]), "workflow")
    try:
        payload = enable_service.build_report(workflow_id)
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc

    summary = payload.get("summary") or {}
    space = payload.get("space") or {}
    lines: list[str] = []
    for item in payload.get("models") or []:
        dest = (item.get("destination") or {}).get("directory") or "?"
        source = item.get("source") or {}
        size = human_bytes(source.get("size")) if source.get("size") else "size unknown"
        marker = "fetch" if item["status"] == "fetchable" else item["status"]
        lines.append(f"  [{marker}] {clip(item['ref_name'], 60)}  -> {clip(dest, 60)}  "
                     f"{size}"
                     + (f"  ({source.get('host')})" if source.get("host") else ""))
    for item in payload.get("node_packages") or []:
        marker = "fetch" if item["status"] == "fetchable" else item["status"]
        lines.append(f"  [{marker}] node package {clip(item['ref_name'], 50)}  "
                     f"{clip(item.get('repo_url') or item.get('reason') or '', 70)}")
    if not space.get("sufficient"):
        lines.append(f"  ! not enough free space: short by "
                     f"{human_bytes(space.get('shortfall_bytes'))}")
    head = (f"{payload['workflow'].get('name')}: "
            f"{summary.get('missing_models', 0)} missing model(s), "
            f"{summary.get('missing_node_packages', 0)} missing node package(s); "
            f"{summary.get('fetchable', 0)} fetchable, "
            f"{human_bytes(summary.get('download_bytes'))} to download. "
            f"Nothing has been downloaded. Show this to the user, then call "
            f"enable_workflow_fetch with plan_token and the item_ids they chose.")
    return render(head, lines, shown=len(lines), total=len(lines)),         cap_structured(payload, "models")


def enable_workflow_fetch(args: dict, ctx: Ctx) -> tuple[str, dict]:
    from ..enable import service as enable_service

    workflow_id = _uid_id(str(args["workflow_uid"]), "workflow")
    item_ids = _require_batch(list(args.get("item_ids") or []))
    if not bool(args.get("confirm")):
        raise ToolFailure(
            "Fetching downloads files from the internet into the ComfyUI install "
            "and requires confirm=true. Nothing was downloaded.",
            code="VALIDATION_ERROR", data={"items": len(item_ids)})
    try:
        payload = enable_service.get_enable_service().fetch(
            workflow_id, plan_token=str(args.get("plan_token") or ""),
            item_ids=item_ids, confirm=True,
            on_conflict=str(args.get("on_conflict") or "fail"), trigger="mcp")
    except AppError as exc:
        raise ToolFailure(exc.message, code=exc.code, data=exc.details) from exc
    payload["audit_id"] = None
    lines = [f"  {i['kind']}  {clip(i['ref_name'], 60)}  {human_bytes(i.get('size'))}  "
             f"-> {clip(i.get('target_abs_path'), 70)}"
             for i in payload.get("items") or []]
    head = (f"Queued {payload['queued']} item(s), "
            f"{human_bytes(payload['bytes_total'])} to download. Node packages are "
            f"cloned only - no installer is ever run; the report lists the exact "
            f"command to run by hand.")
    return render(head, lines, shown=len(lines), total=len(lines)), payload


# ---------------------------------------------------------------------------
# Registry of callables
# ---------------------------------------------------------------------------

HANDLERS = {
    "vault_search": vault_search,
    "list_models": list_models,
    "get_model": get_model,
    "list_node_packages": list_node_packages,
    "list_node_classes": list_node_classes,
    "get_node_class": get_node_class,
    "list_workflows": list_workflows,
    "inspect_workflow": inspect_workflow,
    "find_model_usage": find_model_usage,
    "query_outputs": query_outputs,
    "vault_stats": vault_stats,
    "get_index_status": get_index_status,
    "vault_reindex": vault_reindex,
    "vault_hash_enqueue": vault_hash_enqueue,
    "vault_hash_cancel": vault_hash_cancel,
    "vault_embeddings_rebuild": vault_embeddings_rebuild,
    "vault_rename": vault_rename,
    "vault_move": vault_move,
    "vault_delete": vault_delete,
    "vault_trash_list": vault_trash_list,
    "vault_trash_restore": vault_trash_restore,
    "vault_trash_empty": vault_trash_empty,
    "vault_create_folder": vault_create_folder,
    "vault_assign_tags": vault_assign_tags,
    "enable_workflow_plan": enable_workflow_plan,
    "enable_workflow_fetch": enable_workflow_fetch,
}


def audited_uids(tool_name: str, args: dict, payload: dict) -> list[str]:
    """Which uids a mutating call actually touched - for the audit row."""
    if tool_name == "vault_rename":
        return [str(args.get("uid"))] if args.get("uid") else []
    if tool_name in ("vault_move", "vault_delete", "vault_assign_tags"):
        return [str(u) for u in (args.get("uids") or [])]
    if tool_name == "vault_trash_restore":
        return [f"trash:{i}" for i in (args.get("ids") or [])]
    if tool_name == "vault_trash_empty":
        return [f"trash:{i}" for i in (args.get("ids") or [])]
    if tool_name == "vault_create_folder":
        return [f"root:{args.get('root_id')}"]
    if tool_name in ("vault_hash_enqueue", "vault_hash_cancel"):
        return [str(u) for u in (args.get("uids") or [])]
    if tool_name == "enable_workflow_fetch":
        return [str(args.get("workflow_uid"))] if args.get("workflow_uid") else []
    return []


def affected_count(tool_name: str, payload: dict) -> int:
    if isinstance(payload, dict):
        for key in ("affected", "removed", "updated", "queued", "cancelled"):
            if isinstance(payload.get(key), int):
                return int(payload[key])
        if payload.get("started"):
            return 1
        if tool_name == "vault_create_folder":
            return 1
    return 0


def write_audit(tool: registry.ToolDef, args: dict, ctx: Ctx, *, outcome: str,
                payload: dict | None, error_code: str | None,
                elapsed_ms: int) -> int | None:
    """DECISIONS C5 rail 3 - argument VALUES are recorded for mutations."""
    payload = payload if isinstance(payload, dict) else {}
    audit_id = mcp_audit.record(
        transport=ctx.transport, session_id=ctx.session_id, tool=tool.name,
        arguments=args, uids=audited_uids(tool.name, args, payload),
        outcome=outcome, affected=affected_count(tool.name, payload),
        error_code=error_code, elapsed_ms=elapsed_ms)
    if audit_id is not None and "audit_id" in payload:
        payload["audit_id"] = audit_id
    return audit_id
