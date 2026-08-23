"""``/api/v1/mcp/audit`` - the MCP activity log (API_CONTRACT 21, DECISIONS C5 rail 3).

C5 grants an external MCP client the full file-operation set, delete included,
over a library that takes hours to re-download.  The audit row is what makes that
grant reviewable, and a log nobody can read is worth almost nothing - so this
router is the reading half of rail 3 and the Settings -> Activity view sits on it.

**Read-only, structurally.**  This module declares exactly one route and it is a
``GET``.  There is no endpoint here or anywhere else that edits, prunes or
deletes an audit row, and the query service it calls has no write path to offer.
Retention is documented rather than implemented: the table is append-only.

``/api/v1/mcp`` itself (POST/GET/DELETE, the JSON-RPC transport) belongs to
``app/mcp/http.py`` and is mounted separately; only the ``/audit`` sub-path is
served from here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...core.errors import AppError
from ...services.queries import mcp_audit_query
from ..deps import Page, multi_csv_param, page_params
from ..middleware import ApiError, normalize_code
from ..schemas.common import BASE_ERRORS
from ..schemas.mcp import AuditList

router = APIRouter(prefix="/mcp", tags=["MCP"])


@router.get("/audit", response_model=AuditList, responses=BASE_ERRORS,
            summary="Every mutating MCP tool call, newest first, with a summary")
def audit(
    page: Page = Depends(page_params),
    sort: str = Query(mcp_audit_query.DEFAULT_SORT,
                      description="ts | tool | outcome | affected | elapsed, "
                                  "'-' for descending. Default '-ts'."),
    # Every multi-value filter accepts BOTH `?outcome=ok,error` and
    # `?outcome=ok&outcome=error`; a bare `str` would bind only the last
    # repetition and drop the rest without saying so.
    tool: list[str] | None = Query(None,
                                   description="MCP tool names (CSV or repeated)"),
    outcome: list[str] | None = Query(None, description="ok,partial,error"),
    transport: list[str] | None = Query(None, description="http,stdio"),
    session_id: str | None = Query(None, max_length=80,
                                   description="One MCP session's calls."),
    since: int | None = Query(None, ge=0, description="Epoch ms, inclusive."),
    until: int | None = Query(None, ge=0, description="Epoch ms, inclusive."),
    q: str | None = Query(None, max_length=120,
                          description="Free text over the tool name and the "
                                      "affected uids."),
) -> dict:
    filters = {
        "tool": multi_csv_param("tool", tool),
        "outcome": multi_csv_param("outcome", outcome, mcp_audit_query.OUTCOMES),
        "transport": multi_csv_param("transport", transport,
                                     mcp_audit_query.TRANSPORTS),
        "session_id": session_id,
        "since": since,
        "until": until,
        "q": q,
    }
    if since is not None and until is not None and until < since:
        raise ApiError("VALIDATION_ERROR",
                       "'until' is earlier than 'since'.",
                       field_errors=[{"field": "until",
                                      "message": "must be >= 'since'"}])
    try:
        result = mcp_audit_query.search(filters, sort=sort, limit=page.limit,
                                        offset=page.offset)
        summary = mcp_audit_query.summary(filters)
    except AppError as exc:
        raise ApiError(normalize_code(exc.code), exc.message,
                       details=exc.details) from exc
    payload = result.as_dict()
    payload["summary"] = summary
    payload["retention"] = ("append-only; rows are never edited, pruned or "
                            "deleted")
    return payload
