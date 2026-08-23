"""The ``mcp_audit`` table (DECISIONS C5.1) - the only module that WRITES it.

Every mutating MCP tool call appends one row here **including its argument
values**.  That is the one deliberate exception to MCP_SPEC 9's
"argument values are not logged" rule, which continues to apply to read tools.

This lives under ``services/`` rather than ``app/mcp/`` on purpose: BUILD_PLAN 4
forbids SQL anywhere under ``app/mcp/``, and the MCP handlers must reach the
database only through the shared service layer.

The only statement in this module is an ``INSERT``.  There is no update, no
delete and no purge here or anywhere else in the app: an audit log the
application can erase is not an audit log (DECISIONS C5 rail 3).  Reading it -
for ``GET /api/v1/mcp/audit`` and the Settings -> Activity view - belongs to
``services/queries/mcp_audit_query.py``, where every other read query lives; the
two functions at the bottom are thin forwards kept for existing callers.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from ..core import db as dbmod

log = logging.getLogger(__name__)

OUTCOMES = ("ok", "partial", "error")
MAX_ARGUMENT_BYTES = 16_000


def _dump(value: object, cap: int = MAX_ARGUMENT_BYTES) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value)[:cap])
    if len(text) > cap:
        text = text[:cap - 20] + '..."<truncated>"'
    return text


def record(*, transport: str, tool: str, arguments: object,
           outcome: str = "ok", session_id: str | None = None,
           uids: list[str] | None = None, affected: int = 0,
           error_code: str | None = None, elapsed_ms: int | None = None) -> int | None:
    """Append one audit row.  Never raises - auditing must not break a tool."""
    if outcome not in OUTCOMES:
        outcome = "error"
    row = (
        dbmod.now_ms(), (str(session_id) if session_id else None),
        str(transport)[:16], str(tool)[:80], _dump(arguments),
        _dump(list(uids or []), cap=8_000) if uids is not None else None,
        outcome, int(affected or 0),
        (str(error_code)[:80] if error_code else None),
        (int(elapsed_ms) if elapsed_ms is not None else None),
    )

    def _op(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "INSERT INTO mcp_audit(ts,session_id,transport,tool,arguments,uids,"
            "outcome,affected,error_code,elapsed_ms) VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        conn.commit()
        return int(cur.lastrowid or 0)

    try:
        return int(dbmod.writer().run(_op, timeout=30.0))
    except Exception as exc:  # noqa: BLE001 - an audit failure is logged, not raised
        log.error("mcp_audit write failed for tool=%s: %s", tool, exc)
        return None


def recent(limit: int = 100, offset: int = 0, tool: str | None = None,
           conn: sqlite3.Connection | None = None) -> dict:
    """Settings -> Activity feed (DECISIONS C5 rail 3).

    Forwards to the query service; the full filter set lives there.
    """
    from .queries import mcp_audit_query

    return mcp_audit_query.recent(limit=limit, offset=offset, tool=tool, conn=conn)


def count(conn: sqlite3.Connection | None = None) -> int:
    from .queries import mcp_audit_query

    return mcp_audit_query.count(conn=conn)
