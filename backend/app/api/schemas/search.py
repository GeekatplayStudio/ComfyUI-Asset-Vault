"""Search, suggest, status, rebuild (API_CONTRACT 7)."""

from __future__ import annotations

from typing import Any

from .common import Lenient, PageInfo, Strict


class SearchHit(Lenient):
    uid: str
    kind: str
    score: float | None = None
    title: str | None = None
    subtitle: str | None = None
    snippet: str | None = None
    thumbnail_url: str | None = None
    matched: list[str] = []
    ranks: dict[str, int] | None = None
    entity: dict[str, Any] | None = None


class KindFacet(Lenient):
    value: str
    count: int


class SearchMeta(Lenient):
    elapsed_ms: int | None = None
    lexical_ms: int | None = None
    vector_ms: int | None = None
    fusion_ms: int | None = None


class SearchResponse(Lenient):
    query: str
    mode: str
    smart_available: bool
    smart_reason: str | None = None
    items: list[SearchHit]
    facets: dict[str, list[KindFacet]]
    page: PageInfo
    meta: SearchMeta


class Suggestion(Lenient):
    text: str | None = None
    kind: str | None = None
    uid: str | None = None
    field: str | None = None
    count: int | None = None


class SuggestResponse(Lenient):
    suggestions: list[Suggestion]


class LexicalStatus(Lenient):
    available: bool
    documents: int
    last_built_at: int | None = None


class SemanticStatus(Lenient):
    available: bool
    state: str
    model_id: str
    dim: int
    embedded: int
    pending: int
    reason: str | None = None


class SearchStatus(Lenient):
    lexical: LexicalStatus
    semantic: SemanticStatus


class SearchRebuildRequest(Strict):
    lexical: bool = True
    semantic: bool = True


class SearchRebuildResponse(Lenient):
    job_id: str
