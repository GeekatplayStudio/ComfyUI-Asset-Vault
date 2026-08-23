"""Shared request plumbing: pagination, sort/group/filter allowlists, CSRF, SSE.

Every user-supplied sort/group/filter token is checked against a frozen
allowlist here and then handed to ``services/queries`` as a *key*, never as SQL.
That is the structural fix for the old ``models_api.list_models`` defect, which
interpolated ``sort_column`` straight into the statement.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import Query, Request
from fastapi.responses import StreamingResponse

from ..core import db as dbmod
from .middleware import ApiError, field_error

# ---------------------------------------------------------------------------
# Frozen vocabularies (API_CONTRACT 0.4 / 16)
# ---------------------------------------------------------------------------

SORT_FIELDS: dict[str, tuple[str, ...]] = {
    "models": ("name", "created", "modified", "size", "category", "base_model", "role",
               "params", "rating", "hash_state", "relevance"),
    "node_packages": ("name", "author", "classes", "updated", "size", "relevance"),
    "node_classes": ("name", "display_name", "category", "package", "relevance"),
    "workflows": ("name", "modified", "size", "nodes", "missing", "relevance"),
    "outputs": ("created", "modified", "name", "size", "rating", "width", "height",
                "duration", "relevance"),
}

GROUP_VALUES: dict[str, tuple[str, ...]] = {
    "models": ("none", "category", "base_model", "role", "folder", "precision", "root",
               "hash_state", "integrity", "first_letter", "date"),
    "node_packages": ("none", "author", "official", "enabled", "update_state"),
    "node_classes": ("none", "category", "package"),
    "workflows": ("none", "folder", "base_model", "runnable", "date"),
    "outputs": ("none", "folder", "date", "model", "media_kind", "album", "first_letter"),
}

DEFAULT_SORT: dict[str, str] = {
    "models": "name",
    "node_packages": "name",
    "node_classes": "display_name",
    "workflows": "-modified",
    "outputs": "-created",
}

SEARCH_KINDS = ("model", "node_package", "node_class", "workflow", "output")
MEDIA_KINDS = ("image", "video", "audio", "model3d", "text", "other")
HASH_STATES = ("unhashed", "queued", "hashing", "done", "failed", "stale")
INTEGRITY_STATES = ("ok", "invalid_header", "not_a_model", "truncated", "unreadable",
                    "unsupported_format")
THUMB_SIZES = (160, 320, 640)

UID_RE = re.compile(r"^(model|node_package|node_class|workflow|output|input):[0-9]{1,18}$")

MAX_BULK_UIDS = 200


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Page:
    limit: int
    offset: int


def page_params(limit: int = Query(100, ge=1, le=500,
                                   description="Rows per page (1-500)."),
                offset: int = Query(0, ge=0)) -> Page:
    return Page(limit=limit, offset=offset)


def search_page_params(limit: int = Query(50, ge=1, le=200),
                       offset: int = Query(0, ge=0)) -> Page:
    return Page(limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Sort / group / enum validation
# ---------------------------------------------------------------------------

def check_sort(scope: str, value: str | None, *, has_q: bool) -> str:
    allowed = SORT_FIELDS[scope]
    spec = (value or DEFAULT_SORT[scope]).strip() or DEFAULT_SORT[scope]
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        name = token[1:] if token.startswith("-") else token
        if name not in allowed:
            raise field_error("sort", f"must be one of {'|'.join(allowed)}")
        if name == "relevance" and not has_q:
            raise field_error("sort", "'relevance' requires a 'q' query parameter")
    return spec


def check_group(scope: str, value: str | None) -> str:
    allowed = GROUP_VALUES[scope]
    group = (value or "none").strip() or "none"
    if group not in allowed:
        raise field_error("group", f"must be one of {'|'.join(allowed)}")
    return group


def check_enum(param: str, values: Sequence[str] | None,
               allowed: Iterable[str]) -> list[str]:
    allowed = tuple(allowed)
    out = []
    for value in values or []:
        if value not in allowed:
            raise field_error(param, f"must be one of {'|'.join(allowed)}")
        out.append(value)
    return out


def csv_param(param: str, value: str | None,
              allowed: Iterable[str] | None = None) -> list[str] | None:
    if value is None:
        return None
    items = [p.strip() for p in str(value).split(",") if p.strip()]
    if allowed is not None:
        allowed = tuple(allowed)
        for item in items:
            if item not in allowed:
                raise field_error(param, f"must be one of {'|'.join(allowed)}")
    return items


def multi_csv_param(param: str, value: str | list[str] | None,
                    allowed: Iterable[str] | None = None) -> list[str] | None:
    """Accept a CSV **or** repeated query params, and never drop a value silently.

    ``?kind=model,output`` and ``?kind=model&kind=output`` mean the same thing.
    Declaring the parameter as ``str`` binds only the LAST repetition, so a client
    that repeats params - which the frontend's shared query builder does - had its
    other filter values discarded with no error at all.  Declare the parameter as
    ``list[str] | None`` and route it through here instead: both forms are read,
    order is preserved, and duplicates collapse.
    """
    if value is None:
        return None
    raw = [value] if isinstance(value, str) else list(value)
    items: list[str] = []
    for chunk in raw:
        for part in str(chunk).split(","):
            part = part.strip()
            if part and part not in items:
                items.append(part)
    if allowed is not None:
        allowed = tuple(allowed)
        for item in items:
            if item not in allowed:
                raise field_error(param, f"must be one of {'|'.join(allowed)}")
    return items


def check_uid(uid: str, *, param: str = "uid",
              kinds: Iterable[str] | None = None) -> str:
    if not UID_RE.match(str(uid or "")):
        raise field_error(param, "must look like '<kind>:<id>', e.g. 'model:41'")
    if kinds is not None and str(uid).split(":", 1)[0] not in tuple(kinds):
        raise field_error(param, f"must be one of {'|'.join(kinds)}")
    return str(uid)


def check_uids(uids: Sequence[str] | None, *, param: str = "uids",
               kinds: Iterable[str] | None = None) -> list[str]:
    values = list(uids or [])
    if not values:
        raise field_error(param, "must contain at least one uid")
    if len(values) > MAX_BULK_UIDS:
        raise ApiError("PAYLOAD_TOO_LARGE",
                       f"A single call may affect at most {MAX_BULK_UIDS} items.",
                       details={"requested": len(values), "max": MAX_BULK_UIDS})
    return [check_uid(u, param=param, kinds=kinds) for u in values]


def uid_parts(uid: str) -> tuple[str, int]:
    kind, _sep, num = str(uid).partition(":")
    return kind, int(num)


def apply_fields(items: list[dict], fields: list[str] | None) -> list[dict]:
    """Sparse fieldsets (``?fields=uid,name``).  ``uid`` is always retained."""
    if not fields:
        return items
    keep = {"uid", *fields}
    return [{k: v for k, v in item.items() if k in keep} for item in items]


# ---------------------------------------------------------------------------
# CSRF (API_CONTRACT 0 / ARCHITECTURE 8.4)
# ---------------------------------------------------------------------------

MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


def require_vault_request(request: Request) -> None:
    """Enforced on every mutating method; a cross-origin simple request cannot
    set this header, which is exactly the CSRF property we want."""
    if request.method.upper() not in MUTATING_METHODS:
        return
    if request.headers.get("x-vault-request") != "1":
        raise ApiError("CSRF_HEADER_MISSING",
                       "This request must send the header 'X-Vault-Request: 1'.",
                       details={"header": "X-Vault-Request"})


def require_vault_request_always(request: Request) -> None:
    """For the one idempotent GET that still has a local side effect (reveal)."""
    if request.headers.get("x-vault-request") != "1":
        raise ApiError("CSRF_HEADER_MISSING",
                       "This request must send the header 'X-Vault-Request: 1'.",
                       details={"header": "X-Vault-Request"})


# ---------------------------------------------------------------------------
# Read-only connection
# ---------------------------------------------------------------------------

def ro_conn():
    """Thread-local read-only connection.  Handlers that touch the DB are sync
    ``def`` handlers, so FastAPI runs them in the threadpool and each worker
    thread keeps its own connection - a running scan never blocks them (WAL)."""
    return dbmod.get_ro()


# ---------------------------------------------------------------------------
# SSE (ARCHITECTURE 2.3)
# ---------------------------------------------------------------------------

SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}
HEARTBEAT_S = 15.0


def sse_frame(event: str, payload: Any) -> bytes:
    data = json.dumps(payload, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n".encode()


async def sse_stream(source: AsyncIterator[tuple[str, dict]], request: Request, *,
                     job_id: int | None = None,
                     close_on_done: bool = False) -> AsyncIterator[bytes]:
    """Relay a service ``subscribe()`` iterator as SSE.

    The bus already emits ``heartbeat`` every 15 s and drops a subscriber with
    ``overflow`` if it falls more than 1000 events behind, so this wrapper only
    has to filter by job, honour client disconnects and close cleanly.
    """
    yield sse_frame("open", {"t": dbmod.now_ms(), "job_id": job_id})
    try:
        async for event, payload in source:
            if await request.is_disconnected():
                break
            if job_id is not None and event != "heartbeat":
                payload_job = payload.get("job_id") if isinstance(payload, dict) else None
                if payload_job is not None and int(payload_job) != int(job_id):
                    continue
            yield sse_frame(event, payload)
            if event == "overflow":
                break
            if event == "done" and close_on_done:
                break
    except asyncio.CancelledError:  # client went away mid-frame
        raise
    finally:
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            await aclose()


def sse_response(generator: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(generator, media_type="text/event-stream",
                             headers=dict(SSE_HEADERS))
