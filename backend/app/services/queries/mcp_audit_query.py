"""Read side of the ``mcp_audit`` table - DECISIONS C5 rail 3, "Settings -> Activity".

The writer lives in ``services/mcp_audit.py`` and is the only code that appends a
row.  This module is the only code that reads one, and it reads with a frozen
allowlist for every sort and filter token, exactly like the other query services:
a client supplies a *key*, never a fragment of SQL.

Nothing here can write.  There is deliberately no update, no delete and no purge
function in this module or anywhere above it - an audit trail the application can
erase is not an audit trail.  The table is append-only for the life of the vault;
see API_CONTRACT 21 for the retention note.

Each row is enriched from ``app/mcp/registry`` with what the tool *is* -
destructive, a plain write, or a read - so the UI can tell a delete from a
tag assignment without re-deriving the tool catalogue.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from typing import Any

from ...core import db as dbmod
from ...core.errors import ValidationError
from . import ListResult, Where, clamp_page, meta_dict, page_dict

#: Frozen vocabularies (API_CONTRACT 16 / 21).
OUTCOMES = ("ok", "partial", "error")
TRANSPORTS = ("http", "stdio")
KINDS = ("destructive", "write", "read", "unknown")

#: ``sort`` key -> column.  The value is never taken from the request.
SORT_COLUMNS = {
    "ts": "ts",
    "tool": "tool",
    "outcome": "outcome",
    "affected": "affected",
    "elapsed": "elapsed_ms",
}
DEFAULT_SORT = "-ts"

#: How many distinct tools the summary names before it stops listing them.
MAX_SUMMARY_TOOLS = 24

#: Columns selected explicitly - ``SELECT *`` would silently start shipping any
#: column a later migration adds to the table.
COLUMNS = ("id", "ts", "session_id", "transport", "tool", "arguments", "uids",
           "outcome", "affected", "error_code", "elapsed_ms")
#: The column list, built here from COLUMNS and never from a request.
_AUDIT_COLUMNS = ", ".join(COLUMNS)


def _catalogue() -> dict[str, dict]:
    """What each tool is, from the MCP registry.

    Imported lazily and defensively: the Activity view must still render if the
    MCP server module is unavailable, because the rows it is showing are the
    record of what already happened.
    """
    try:
        from ...mcp import registry
    except ImportError:  # pragma: no cover - the MCP module is always present
        return {}
    out = {}
    for tool in registry.TOOLS:
        destructive = bool(tool.annotations.get("destructiveHint"))
        out[tool.name] = {
            "title": tool.title,
            "mutating": bool(tool.mutating),
            "destructive": destructive,
            "kind": ("destructive" if destructive
                     else ("write" if tool.mutating else "read")),
        }
    return out


def _classify(tool: str, catalogue: dict[str, dict]) -> dict:
    """A tool the registry no longer carries is reported, never guessed at."""
    known = catalogue.get(tool)
    if known is not None:
        return known
    return {"title": tool, "mutating": None, "destructive": None, "kind": "unknown"}


def _json(value: Any) -> Any:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        return value
    with contextlib.suppress(TypeError, ValueError):
        return json.loads(value)
    # A row whose JSON failed to parse still has to be visible; hiding it would
    # be the audit log losing an entry.
    return {"_unparsed": value[:2000]}


def _order_by(sort: str | None) -> str:
    """Translate the sort vocabulary into ORDER BY.

    The tiebreak is ``id DESC``, not the ``id ASC`` the other list endpoints use:
    this is a log, several rows can share a millisecond, and within that
    millisecond the newest call must still read as the newest.
    """
    spec = (sort or DEFAULT_SORT).strip() or DEFAULT_SORT
    parts: list[str] = []
    for token in spec.split(","):
        token = token.strip()
        if not token or token in ("id", "-id"):
            continue
        desc = token.startswith("-")
        column = SORT_COLUMNS.get(token[1:] if desc else token)
        if column is None:
            raise ValidationError(f"Unsupported sort field '{token}'.",
                                  details={"allowed": sorted(SORT_COLUMNS)})
        parts.append(f"{column} {'DESC' if desc else 'ASC'}")
    parts.append("id DESC")
    return ", ".join(parts)


def _check_enum(param: str, values, allowed: tuple[str, ...]) -> list[str] | None:
    clean = [str(v) for v in (values or []) if str(v).strip()]
    if not clean:
        return None
    for value in clean:
        if value not in allowed:
            raise ValidationError(f"Unsupported {param} '{value}'.",
                                  details={"allowed": list(allowed)})
    return clean


def _where(filters: dict | None) -> Where:
    f = filters or {}
    where = Where()
    where.any_of("tool", [str(t) for t in (f.get("tool") or []) if str(t).strip()])
    where.any_of("outcome", _check_enum("outcome", f.get("outcome"), OUTCOMES))
    where.any_of("transport", _check_enum("transport", f.get("transport"), TRANSPORTS))
    session = f.get("session_id")
    if session:
        where.eq("session_id", str(session))
    where.gte("ts", f.get("since"))
    where.lte("ts", f.get("until"))
    q = str(f.get("q") or "").strip()
    if q:
        # Free text covers the tool name and the affected uids - the two things
        # you already know when you are looking for "what happened to model:41".
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.add("(tool LIKE ? ESCAPE '\\' OR COALESCE(uids,'') LIKE ? ESCAPE '\\')",
                  like, like)
    return where


def search(filters: dict | None = None, sort: str | None = None,
           limit: int | None = None, offset: int | None = None,
           conn: sqlite3.Connection | None = None) -> ListResult:
    """One page of audit rows, newest first by default (API_CONTRACT 21)."""
    t0 = time.perf_counter()
    conn = conn or dbmod.get_ro()
    limit, offset = clamp_page(limit, offset)
    where = _where(filters)
    order = _order_by(sort)
    clause, args = where.sql(), where.args()

    total = int(dbmod.scalar(
        conn, f"SELECT COUNT(*) FROM mcp_audit WHERE {clause}", args) or 0)  # noqa: S608
    rows = dbmod.rows(
        conn,
        f"SELECT {_AUDIT_COLUMNS} FROM mcp_audit WHERE {clause} "  # noqa: S608
        f"ORDER BY {order} LIMIT ? OFFSET ?", (*args, limit, offset))

    catalogue = _catalogue()
    items = []
    for row in rows:
        item = dict(row)
        item["arguments"] = _json(item.get("arguments")) or {}
        item["uids"] = _json(item.get("uids")) or []
        item.update(_classify(str(item.get("tool") or ""), catalogue))
        items.append(item)

    return ListResult(items=items,
                      page=page_dict(limit, offset, total, len(items)),
                      meta=meta_dict(t0, sort=(sort or DEFAULT_SORT)))


def summary(filters: dict | None = None,
            conn: sqlite3.Connection | None = None) -> dict:
    """The headline layer for Settings -> Activity (C11: summary first).

    Every figure is measured under the *same* filters as the page beside it, so
    the two never disagree; ``vault_total`` is the only unfiltered number and it
    is labelled as such.
    """
    conn = conn or dbmod.get_ro()
    where = _where(filters)
    clause, args = where.sql(), where.args()
    catalogue = _catalogue()

    head = dbmod.one(
        conn,
        "SELECT COUNT(*) n, COUNT(DISTINCT session_id) sessions, "  # noqa: S608
        "MIN(ts) first_ts, MAX(ts) last_ts, "
        f"COALESCE(SUM(affected),0) affected FROM mcp_audit WHERE {clause}", args)

    by_outcome = {name: 0 for name in OUTCOMES}
    for row in dbmod.rows(
        conn, f"SELECT outcome, COUNT(*) n FROM mcp_audit WHERE {clause} "  # noqa: S608
              "GROUP BY outcome", args):
        by_outcome[str(row["outcome"])] = int(row["n"] or 0)

    by_transport = {name: 0 for name in TRANSPORTS}
    for row in dbmod.rows(
        conn, f"SELECT transport, COUNT(*) n FROM mcp_audit WHERE {clause} "  # noqa: S608
              "GROUP BY transport", args):
        by_transport[str(row["transport"])] = int(row["n"] or 0)

    tool_rows = dbmod.rows(
        conn,
        "SELECT tool, COUNT(*) n, MAX(ts) last_ts, "  # noqa: S608
        "COALESCE(SUM(affected),0) affected, "
        "SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END) errors "
        f"FROM mcp_audit WHERE {clause} GROUP BY tool ORDER BY n DESC, tool "
        "LIMIT ?", (*args, MAX_SUMMARY_TOOLS + 1))
    by_tool = []
    for row in tool_rows[:MAX_SUMMARY_TOOLS]:
        entry = {"tool": str(row["tool"]), "count": int(row["n"] or 0),
                 "errors": int(row["errors"] or 0),
                 "affected": int(row["affected"] or 0),
                 "last_ts": int(row["last_ts"] or 0)}
        entry.update(_classify(entry["tool"], catalogue))
        by_tool.append(entry)

    by_kind = {name: 0 for name in KINDS}
    for entry in by_tool:
        by_kind[str(entry.get("kind") or "unknown")] += entry["count"]

    return {
        "total": int(head["n"] or 0) if head else 0,
        "vault_total": int(dbmod.scalar(conn, "SELECT COUNT(*) FROM mcp_audit") or 0),
        "filtered": bool(where.clauses),
        "sessions": int(head["sessions"] or 0) if head else 0,
        "affected": int(head["affected"] or 0) if head else 0,
        "first_ts": (int(head["first_ts"]) if head and head["first_ts"] else None),
        "last_ts": (int(head["last_ts"]) if head and head["last_ts"] else None),
        "by_outcome": by_outcome,
        "by_transport": by_transport,
        "by_kind": by_kind,
        "by_tool": by_tool,
        "by_tool_truncated": len(tool_rows) > MAX_SUMMARY_TOOLS,
    }


def recent(limit: int = 100, offset: int = 0, tool: str | None = None,
           conn: sqlite3.Connection | None = None) -> dict:
    """The narrow form ``services/mcp_audit.recent`` has always exposed."""
    result = search({"tool": [tool] if tool else None}, limit=limit, offset=offset,
                    conn=conn)
    return {"items": result.items, "page": result.page}


def count(conn: sqlite3.Connection | None = None) -> int:
    conn = conn or dbmod.get_ro()
    return int(dbmod.scalar(conn, "SELECT COUNT(*) FROM mcp_audit") or 0)
