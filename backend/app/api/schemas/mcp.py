"""MCP activity log - the read side of ``mcp_audit`` (API_CONTRACT 21, C5 rail 3)."""

from __future__ import annotations

from typing import Any, Literal

from .common import Lenient, MetaInfo, PageInfo

Outcome = Literal["ok", "partial", "error"]
Transport = Literal["http", "stdio"]
#: What the tool *is*, resolved from the MCP tool catalogue rather than guessed
#: from the row: a delete and a tag assignment must not look alike.
ToolKind = Literal["destructive", "write", "read", "unknown"]


class AuditEntry(Lenient):
    id: int
    ts: int
    session_id: str | None = None
    transport: Transport
    tool: str
    title: str | None = None
    #: Full argument VALUES - the deliberate exception to MCP_SPEC 9 that makes
    #: this log worth keeping (DECISIONS C5 rail 3).
    arguments: dict[str, Any] = {}
    uids: list[str] = []
    outcome: Outcome
    affected: int = 0
    error_code: str | None = None
    elapsed_ms: int | None = None
    kind: ToolKind = "unknown"
    mutating: bool | None = None
    destructive: bool | None = None


class AuditToolStat(Lenient):
    tool: str
    title: str | None = None
    count: int
    errors: int = 0
    affected: int = 0
    last_ts: int | None = None
    kind: ToolKind = "unknown"
    mutating: bool | None = None
    destructive: bool | None = None


class AuditSummary(Lenient):
    """Measured under the same filters as the page beside it.

    ``vault_total`` is the one unfiltered figure, so the UI can say "12 of 1,167".
    """

    total: int
    vault_total: int
    filtered: bool = False
    sessions: int = 0
    affected: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    by_outcome: dict[str, int] = {}
    by_transport: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_tool: list[AuditToolStat] = []
    by_tool_truncated: bool = False


class AuditList(Lenient):
    items: list[AuditEntry]
    page: PageInfo
    summary: AuditSummary
    meta: MetaInfo | None = None
    #: Stated in the payload as well as the docs: this endpoint is the whole
    #: read surface, and there is no write surface at all.
    retention: str = "append-only; rows are never edited, pruned or deleted"
