"""``/api/v1/search`` - API_CONTRACT 7.

Smart search that is requested but unavailable is **not** an error: the response
is a 200 with ``mode: "lexical"`` and a ``smart_reason`` from the frozen
vocabulary (DECISIONS C2).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query

from ...core import config_service
from ...core.errors import SearchSyntaxError
from ...indexing.service import get_indexer
from ...jobs.embed_service import get_embed_service
from ...search import hybrid
from ...services.queries import (
    models_query,
    nodes_query,
    outputs_query,
    thumb_url,
    workflows_query,
)
from ..deps import SEARCH_KINDS, Page, csv_param, search_page_params
from ..middleware import ApiError
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.search import (
    SearchRebuildRequest,
    SearchRebuildResponse,
    SearchResponse,
    SearchStatus,
    SuggestResponse,
)
from .embeddings_router import contract_reason

log = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["Search"])

#: One list call per kind hydrates the ``entity`` blocks; both sides run the
#: same relevance ordering, so this is a join rather than a second search.
ENTITY_LOADERS = {
    "model": models_query.list_models,
    "node_package": nodes_query.list_node_packages,
    "node_class": nodes_query.list_node_classes,
    "workflow": workflows_query.list_workflows,
    "output": outputs_query.list_outputs,
}
FUSED_CAP = 500


def _smart_reason(smart_requested: bool) -> str | None:
    cfg = config_service.get_config()
    if smart_requested and not cfg.smart_search_enabled:
        return "disabled_by_user"
    return contract_reason(get_embed_service().status())


def _hydrate_entities(uids: list[str], q: str, smart: bool, limit: int) -> dict[str, dict]:
    wanted: dict[str, set[str]] = {}
    for uid in uids:
        wanted.setdefault(uid.split(":", 1)[0], set()).add(uid)
    out: dict[str, dict] = {}
    for kind, ids in wanted.items():
        loader = ENTITY_LOADERS.get(kind)
        if loader is None:
            continue
        try:
            result = loader({"q": q, "smart": smart}, sort="relevance", group="none",
                            limit=min(500, max(limit, len(ids))), offset=0)
        except Exception as exc:  # noqa: BLE001 - a miss degrades to entity: null
            log.info("search: could not hydrate %s entities (%s)", kind, exc)
            continue
        for item in result.items:
            if item.get("uid") in ids:
                out[str(item["uid"])] = item
    return out


@router.get("", response_model=SearchResponse,
            responses={**error_responses("SEARCH_SYNTAX"), **BASE_ERRORS},
            summary="Unified lexical / hybrid search across every asset kind")
def search(page: Page = Depends(search_page_params),
           q: str = Query(..., min_length=1, max_length=512),
           smart: bool = Query(False),
           kinds: str | None = Query(None),
           per_kind_limit: int | None = Query(None, ge=1, le=200),
           raw: bool = Query(False)) -> dict:
    kind_list = csv_param("kinds", kinds, SEARCH_KINDS)
    try:
        result = hybrid.search(q, smart=smart, kinds=kind_list, filters={},
                               limit=FUSED_CAP, offset=0, raw=raw)
    except SearchSyntaxError as exc:
        raise ApiError("SEARCH_SYNTAX", exc.message, details=exc.details) from exc

    items = list(result.items)
    if per_kind_limit:
        seen: dict[str, int] = {}
        balanced = []
        for item in items:
            kind = str(item.get("kind"))
            seen[kind] = seen.get(kind, 0) + 1
            if seen[kind] <= per_kind_limit:
                balanced.append(item)
        items = balanced

    facets = {"kind": []}
    counts: dict[str, int] = {}
    for item in items:
        kind = str(item.get("kind"))
        counts[kind] = counts.get(kind, 0) + 1
    facets["kind"] = [{"value": k, "count": v}
                      for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]

    total = len(items)
    window = items[page.offset:page.offset + page.limit]
    entities = _hydrate_entities([str(i["uid"]) for i in window], q, smart,
                                 page.offset + page.limit)
    hits = []
    for item in window:
        uid = str(item["uid"])
        hits.append({
            "uid": uid, "kind": item.get("kind"), "score": item.get("score"),
            "title": item.get("title"), "subtitle": item.get("subtitle"),
            "snippet": None,
            "thumbnail_url": thumb_url(uid, 160),
            "matched": item.get("matched") or [],
            "ranks": None,
            "entity": entities.get(uid),
        })

    smart_available = bool(result.smart_available)
    return {
        "query": q,
        "mode": result.mode,
        "smart_available": smart_available,
        "smart_reason": None if (smart_available and result.mode == "hybrid")
        else _smart_reason(smart),
        "items": hits,
        "facets": facets,
        "page": {"limit": page.limit, "offset": page.offset, "total": total,
                 "returned": len(hits),
                 "has_more": page.offset + len(hits) < total},
        "meta": {"elapsed_ms": result.elapsed_ms, "lexical_ms": None,
                 "vector_ms": None, "fusion_ms": None},
    }


@router.get("/suggest", response_model=SuggestResponse, responses=BASE_ERRORS,
            summary="Prefix-index type-ahead")
def suggest(q: str = Query(..., min_length=1, max_length=200),
            limit: int = Query(8, ge=1, le=50),
            kinds: str | None = Query(None)) -> dict:
    kind_list = csv_param("kinds", kinds, SEARCH_KINDS)
    return {"suggestions": hybrid.suggest(q, limit=limit, kinds=kind_list)}


@router.get("/status", response_model=SearchStatus, responses=BASE_ERRORS,
            summary="Lexical and semantic index readiness")
def search_status() -> dict:
    stats = hybrid.status()
    embed = get_embed_service().status()
    documents = int(stats.get("documents") or 0)
    embedded = int(embed.get("embedded") or 0)
    return {
        "lexical": {"available": documents > 0, "documents": documents,
                    "last_built_at": None},
        "semantic": {
            "available": bool(stats.get("smart_available")),
            "state": str(embed.get("state") or "not_installed"),
            "model_id": str(embed.get("model_id") or ""),
            "dim": int(embed.get("dim") or 0),
            "embedded": embedded,
            "pending": max(0, documents - embedded),
            "reason": contract_reason(embed),
        },
    }


@router.post("/rebuild", status_code=202, response_model=SearchRebuildResponse,
             responses=MUTATION_ERRORS,
             summary="Drop and repopulate the derived search indexes")
def rebuild_search(body: SearchRebuildRequest) -> dict:
    if body.lexical:
        try:
            get_indexer().start(mode="targeted", phases=["index"], force=True,
                                trigger="search-rebuild")
        except Exception as exc:
            raise ApiError("JOB_ALREADY_RUNNING", str(exc),
                           details=getattr(exc, "details", {}) or {}) from exc
    if body.semantic:
        get_embed_service().rebuild(None, True)
    return {"job_id": f"search-{uuid.uuid4().hex[:6]}"}
