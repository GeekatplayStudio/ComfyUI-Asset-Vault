"""Vector arm - a thin facade over EmbedService so hybrid.py stays declarative."""

from __future__ import annotations

from ..jobs.embed_service import get_embed_service


def available() -> tuple[bool, str | None]:
    svc = get_embed_service()
    if not svc.available:
        return False, svc.status().get("reason") or "Smart search is not installed."
    m, uids, _kinds = svc.matrix()
    if m is None or not uids:
        return False, "No embeddings have been built yet."
    return True, None


def search(q: str, *, kinds: list[str] | None = None, limit: int = 200,
           min_score: float | None = None) -> list[tuple[str, str, float]]:
    """Nearest neighbours above the configured similarity floor.

    ``min_score`` defaults to the vault's ``smart_search_min_score`` setting:
    raise it for stricter, fewer semantic hits, lower it to cast wider.
    """
    if min_score is None:
        from ..core import config_service

        min_score = config_service.get_config().smart_search_min_score
    return get_embed_service().search(q, kinds=kinds, limit=limit,
                                      min_score=min_score)


def embed_query(text: str):
    return get_embed_service().embed_query(text)


def count() -> int:
    return int(get_embed_service().status().get("embedded") or 0)
