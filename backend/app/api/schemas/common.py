"""Envelope primitives shared by every endpoint (API_CONTRACT 0.1-0.3)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..middleware import ERROR_STATUS


class Lenient(BaseModel):
    """Base for response objects: documented fields plus forward-compatible extras."""

    model_config = ConfigDict(extra="allow")


class Strict(BaseModel):
    """Base for request bodies: unknown fields are rejected so typos surface."""

    model_config = ConfigDict(extra="forbid")


class FieldErrorItem(BaseModel):
    field: str
    message: str


class ErrorBody(BaseModel):
    code: str = Field(examples=["NOT_FOUND"])
    message: str
    details: dict[str, Any] | None = None
    field_errors: list[FieldErrorItem] | None = None
    request_id: str
    retryable: bool = False
    docs: str


class ErrorEnvelope(BaseModel):
    """The one and only non-2xx body shape."""

    error: ErrorBody


class PageInfo(BaseModel):
    limit: int
    offset: int
    total: int | None
    returned: int
    has_more: bool
    total_is_estimate: bool | None = None


class MetaInfo(Lenient):
    elapsed_ms: int | None = None
    query_id: str | None = None
    sort: str | None = None
    smart_available: bool | None = None
    mode: str | None = None


class GroupInfo(Lenient):
    key: str
    label: str | None = None
    count: int
    bytes: int | None = None
    offset: int | None = None
    #: Date buckets carry their own bounds (epoch ms, both inclusive) so a
    #: caller can filter by one without parsing the label.  The labels mix
    #: relative ("Today") and absolute ("June 2026") forms, so deriving a range
    #: from the text would break on the first wording or locale change.
    date_from: int | None = None
    date_to: int | None = None


class TreeNode(Lenient):
    key: str
    label: str | None = None
    count: int = 0
    bytes: int | None = None
    children: list[dict[str, Any]] = []


class ListEnvelope(Lenient):
    items: list[dict[str, Any]]
    page: PageInfo
    groups: list[GroupInfo] | None = None
    meta: MetaInfo | None = None


class OkResult(Lenient):
    ok: bool = True


def error_responses(*codes: str) -> dict[int, dict[str, Any]]:
    """Build the ``responses=`` map for the documented failures of a route."""
    out: dict[int, dict[str, Any]] = {}
    for code in codes:
        status = ERROR_STATUS[code]
        entry = out.setdefault(status, {"model": ErrorEnvelope, "description": ""})
        desc = entry["description"]
        entry["description"] = f"{desc} / {code}" if desc else code
    return out


#: Attached to every route: the envelope is universal, so document it once.
BASE_ERRORS = error_responses("VALIDATION_ERROR", "INTERNAL")
MUTATION_ERRORS = error_responses("VALIDATION_ERROR", "CSRF_HEADER_MISSING", "INTERNAL")
