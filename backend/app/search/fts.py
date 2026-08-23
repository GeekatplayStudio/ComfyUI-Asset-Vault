"""SQLite FTS5 lexical arm.

Maintenance is explicit ``DELETE`` + ``INSERT`` pairs inside the row's own
transaction - not triggers - because every write already funnels through one
writer thread (ARCHITECTURE 5.2).
"""

from __future__ import annotations

import re
import sqlite3

from ..core import db as dbmod
from ..core.errors import SearchSyntaxError
from .doc_builder import SearchDoc

BM25_WEIGHTS = "0, 0, 10.0, 4.0, 1.0, 6.0"
_TOKEN_RE = re.compile(r"[\w\-_.]+", re.UNICODE)
MAX_TERMS = 24


def sanitize(q: str, *, raw: bool = False) -> str:
    """Rebuild user input as a quoted prefix expression.  Raw input never reaches FTS."""
    if raw:
        return q
    tokens = _TOKEN_RE.findall(q or "")
    tokens = [t for t in tokens if t][:MAX_TERMS]
    if not tokens:
        return ""
    parts = []
    for t in tokens:
        esc = t.replace('"', "")
        if not esc:
            continue
        parts.append(f'"{esc}"*')
    return " ".join(parts)


def upsert(conn: sqlite3.Connection, doc: SearchDoc) -> None:
    conn.execute("DELETE FROM search_fts WHERE uid = ?", (doc.uid,))
    cur = conn.execute(
        "INSERT INTO search_fts(uid,kind,title,subtitle,body,tags) VALUES (?,?,?,?,?,?)",
        (doc.uid, doc.kind, doc.title, doc.subtitle, doc.body, doc.tags),
    )
    conn.execute(
        "INSERT INTO search_docs(uid,kind,text_hash,fts_rowid,updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET kind=excluded.kind, "
        "text_hash=excluded.text_hash, fts_rowid=excluded.fts_rowid, "
        "updated_at=excluded.updated_at",
        (doc.uid, doc.kind, doc.text_hash, int(cur.lastrowid), dbmod.now_ms()),
    )


def delete(conn: sqlite3.Connection, uid: str) -> None:
    conn.execute("DELETE FROM search_fts WHERE uid = ?", (uid,))
    conn.execute("DELETE FROM search_docs WHERE uid = ?", (uid,))
    conn.execute("DELETE FROM embeddings WHERE uid = ?", (uid,))


def search(conn: sqlite3.Connection, q: str, *, kinds: list[str] | None = None,
           limit: int = 200, raw: bool = False) -> list[tuple[str, str, float]]:
    """Return [(uid, kind, score)] ordered best-first.  Score is -bm25 (higher better)."""
    expr = sanitize(q, raw=raw)
    if not expr:
        return []
    sql = (
        f"SELECT uid, kind, -bm25(search_fts, {BM25_WEIGHTS}) AS score "  # noqa: S608
        "FROM search_fts WHERE search_fts MATCH ?"
    )
    params: list = [expr]
    if kinds:
        sql += " AND kind IN (" + ",".join("?" * len(kinds)) + ")"
        params.extend(kinds)
    sql += " ORDER BY score DESC LIMIT ?"
    params.append(int(limit))
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise SearchSyntaxError(f"Invalid search expression: {exc}",
                                details={"query": q}) from exc
    return [(r["uid"], r["kind"], float(r["score"])) for r in rows]


def suggest(conn: sqlite3.Connection, prefix: str, limit: int = 8,
            kinds: list[str] | None = None) -> list[dict]:
    expr = sanitize(prefix)
    if not expr:
        return []
    sql = (
        f"SELECT uid, kind, title, -bm25(search_fts, {BM25_WEIGHTS}) AS score "  # noqa: S608
        "FROM search_fts WHERE search_fts MATCH ?"
    )
    params: list = [expr]
    if kinds:
        sql += " AND kind IN (" + ",".join("?" * len(kinds)) + ")"
        params.extend(kinds)
    sql += " ORDER BY score DESC LIMIT ?"
    params.append(int(limit))
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"uid": r["uid"], "kind": r["kind"], "text": r["title"]} for r in rows]


def rebuild(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM search_fts")
    conn.execute("DELETE FROM search_docs")


def count(conn: sqlite3.Connection) -> int:
    return int(dbmod.scalar(conn, "SELECT COUNT(*) FROM search_docs") or 0)
