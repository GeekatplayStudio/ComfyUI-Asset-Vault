"""Hybrid search: FTS5 + vector cosine fused with Reciprocal Rank Fusion (k=60).

RRF fuses rankings, not scores, so no normalization between BM25 (unbounded)
and cosine (-1..1) is needed and a missing arm degrades gracefully.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from ..core import db as dbmod
from . import fts, vec

RRF_K = 60
W_LEXICAL = 1.0
W_VECTOR = 0.8
EXACT_BONUS = 0.35
ARM_LIMIT = 200

KINDS = ("model", "node_package", "node_class", "workflow", "output")


@dataclass
class SearchResult:
    items: list[dict] = field(default_factory=list)
    total: int = 0
    mode: str = "lexical"
    smart_available: bool = False
    smart_reason: str | None = None
    elapsed_ms: int = 0
    arms: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "items": self.items, "total": self.total, "mode": self.mode,
            "smart_available": self.smart_available, "smart_reason": self.smart_reason,
            "elapsed_ms": self.elapsed_ms, "arms": self.arms,
        }


def _rrf(rankings: list[tuple[list[tuple[str, str, float]], float]]) -> dict[str, float]:
    fused: dict[str, float] = {}
    for hits, weight in rankings:
        for rank, (uid, _kind, _score) in enumerate(hits):
            fused[uid] = fused.get(uid, 0.0) + weight / (RRF_K + rank + 1)
    return fused


def _hydrate(conn: sqlite3.Connection, uids: list[str]) -> dict[str, dict]:
    """Fetch title/subtitle for the fused uids in one query per kind."""
    by_kind: dict[str, list[int]] = {}
    for uid in uids:
        kind, _sep, num = uid.partition(":")
        try:
            by_kind.setdefault(kind, []).append(int(num))
        except (TypeError, ValueError):
            continue
    out: dict[str, dict] = {}
    specs = {
        "model": ("SELECT id, name AS title, category AS subtitle, base_model_family, "
                  "model_role FROM models WHERE id IN "),
        "node_package": ("SELECT id, display_name AS title, author AS subtitle "
                         "FROM node_packages WHERE id IN "),
        "node_class": ("SELECT id, display_name AS title, category AS subtitle, node_id "
                       "FROM node_classes WHERE id IN "),
        "workflow": ("SELECT id, name AS title, folder AS subtitle, base_model_family "
                     "FROM workflows WHERE id IN "),
        "output": ("SELECT id, filename AS title, folder AS subtitle, media_kind, "
                   "model_name FROM outputs WHERE id IN "),
    }
    for kind, ids in by_kind.items():
        sql = specs.get(kind)
        if not sql or not ids:
            continue
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            ph = ",".join("?" * len(chunk))
            try:
                rows = conn.execute(sql + f"({ph})", chunk).fetchall()
            except sqlite3.DatabaseError:
                continue
            for r in rows:
                d = dict(r)
                d["uid"] = f"{kind}:{r['id']}"
                d["kind"] = kind
                out[d["uid"]] = d
    return out


def search(q: str, *, smart: bool = False, kinds: list[str] | None = None,
           filters: dict | None = None, limit: int = 50, offset: int = 0,
           raw: bool = False, conn: sqlite3.Connection | None = None) -> SearchResult:
    t0 = time.perf_counter()
    res = SearchResult()
    conn = conn or dbmod.get_ro()
    filters = filters or {}
    kinds = [k for k in (kinds or []) if k in KINDS] or None

    smart_ok, reason = vec.available()
    res.smart_available = smart_ok
    res.smart_reason = reason

    if not (q or "").strip():
        res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return res

    lexical = fts.search(conn, q, kinds=kinds, limit=ARM_LIMIT, raw=raw)
    rankings = [(lexical, W_LEXICAL)]
    res.arms["lexical"] = len(lexical)

    vector: list = []
    if smart and smart_ok:
        vector = vec.search(q, kinds=kinds, limit=ARM_LIMIT)
        if vector:
            rankings.append((vector, W_VECTOR))
            res.arms["vector"] = len(vector)
            res.mode = "hybrid"
        else:
            res.mode = "lexical"
    else:
        res.mode = "lexical"

    fused = _rrf(rankings)
    if not fused:
        res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return res

    hydrated = _hydrate(conn, list(fused))
    needle = q.strip().lower()
    lex_uids = {u for u, _k, _s in lexical}
    vec_uids = {u for u, _k, _s in vector}
    name_uids: set[str] = set()
    scored: list[tuple[float, str]] = []
    for uid, score in fused.items():
        meta = hydrated.get(uid)
        if meta is None:
            continue
        if kinds and meta["kind"] not in kinds:
            continue
        if not _passes(meta, filters):
            continue
        title = str(meta.get("title") or "").lower()
        if needle and needle in title:
            score += EXACT_BONUS
            name_uids.add(uid)
            if title == needle:
                score += EXACT_BONUS
        scored.append((score, uid))

    scored.sort(key=lambda t: (-t[0], t[1]))
    res.total = len(scored)
    page = scored[offset:offset + limit]
    items = []
    for score, uid in page:
        meta = dict(hydrated[uid])
        meta["score"] = round(score, 6)
        matched = []
        if uid in name_uids:
            matched.append("name")
        if uid in lex_uids:
            matched.append("lexical")
        if uid in vec_uids:
            matched.append("semantic")
        meta["matched"] = matched
        items.append(meta)
    res.items = items
    res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return res


def _passes(meta: dict, filters: dict) -> bool:
    for key, allowed in (filters or {}).items():
        if allowed in (None, [], ()):
            continue
        value = meta.get(key)
        if isinstance(allowed, (list, tuple, set)):
            if value not in allowed:
                return False
        elif value != allowed:
            return False
    return True


def suggest(q: str, limit: int = 8, kinds: list[str] | None = None,
            conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or dbmod.get_ro()
    return fts.suggest(conn, q, limit=limit, kinds=kinds)


def status(conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or dbmod.get_ro()
    smart_ok, reason = vec.available()
    return {
        "documents": fts.count(conn),
        "embedded": vec.count(),
        "smart_available": smart_ok,
        "smart_reason": reason,
        "mode_default": "lexical",
    }
