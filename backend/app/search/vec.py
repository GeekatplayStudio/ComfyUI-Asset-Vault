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


def search(q: str, *, kinds: list[str] | None = None,
           limit: int = 200) -> list[tuple[str, str, float]]:
    return get_embed_service().search(q, kinds=kinds, limit=limit)


def embed_query(text: str):
    return get_embed_service().embed_query(text)


def count() -> int:
    return int(get_embed_service().status().get("embedded") or 0)
