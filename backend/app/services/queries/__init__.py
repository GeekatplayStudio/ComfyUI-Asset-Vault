"""Per-domain query services returning contract-shaped dicts (API_CONTRACT 3-6).

Both the REST routers and the MCP server consume these directly - that shared
design is what prevents the two surfaces from diverging.  No SQL lives anywhere
else.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ...core import db as dbmod
from ...core.errors import ValidationError

MAX_LIMIT = 500
DEFAULT_LIMIT = 100

DATE_BUCKETS = ("Today", "Yesterday", "This week", "This month", "Older")


@dataclass
class ListResult:
    items: list[dict] = field(default_factory=list)
    page: dict = field(default_factory=dict)
    groups: list[dict] | None = None
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"items": self.items, "page": self.page, "meta": self.meta}
        if self.groups is not None:
            out["groups"] = self.groups
        return out


def clamp_page(limit: int | None, offset: int | None) -> tuple[int, int]:
    lim = DEFAULT_LIMIT if limit is None else int(limit)
    off = 0 if offset is None else int(offset)
    return max(1, min(MAX_LIMIT, lim)), max(0, off)


def page_dict(limit: int, offset: int, total: int | None, returned: int) -> dict:
    return {
        "limit": limit, "offset": offset, "total": total, "returned": returned,
        "has_more": bool(total is not None and offset + returned < total),
    }


def meta_dict(started: float, **extra) -> dict:
    d = {"elapsed_ms": int((time.perf_counter() - started) * 1000),
         "query_id": uuid.uuid4().hex[:12]}
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


def parse_sort(sort: str | None, allowed: dict[str, str], default: str) -> str:
    """Translate the API sort vocabulary into an ORDER BY clause.

    Every sort implicitly appends ``,id`` for deterministic pagination.
    """
    spec = (sort or default).strip() or default
    parts: list[str] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        desc = token.startswith("-")
        field_name = token[1:] if desc else token
        if field_name == "id":
            continue
        column = allowed.get(field_name)
        if column is None:
            raise ValidationError(
                f"Unsupported sort field '{field_name}'.",
                details={"allowed": sorted(allowed)},
            )
        parts.append(f"{column} {'DESC' if desc else 'ASC'}")
    parts.append("id ASC")
    return ", ".join(parts)


class Where:
    """Small AND-of-ORs builder; every value is bound, never interpolated."""

    def __init__(self) -> None:
        self.clauses: list[str] = []
        self.params: list[Any] = []

    def add(self, sql: str, *params: Any) -> Where:
        self.clauses.append(sql)
        self.params.extend(params)
        return self

    def any_of(self, column: str, values) -> Where:
        vals = [v for v in (values or []) if v not in (None, "")]
        if not vals:
            return self
        ph = ",".join("?" * len(vals))
        return self.add(f"{column} IN ({ph})", *vals)

    def eq(self, column: str, value) -> Where:
        if value is None:
            return self
        return self.add(f"{column} = ?", value)

    def bool_eq(self, column: str, value) -> Where:
        if value is None:
            return self
        return self.add(f"{column} = ?", 1 if value else 0)

    def gte(self, column: str, value) -> Where:
        return self if value is None else self.add(f"{column} >= ?", value)

    def lte(self, column: str, value) -> Where:
        return self if value is None else self.add(f"{column} <= ?", value)

    def prefix(self, column: str, value) -> Where:
        if not value:
            return self
        return self.add(f"{column} LIKE ?", str(value).replace("\\", "/").rstrip("/") + "%")

    def sql(self, base: str = "1=1") -> str:
        return " AND ".join([base, *self.clauses]) if self.clauses else base

    def args(self) -> tuple:
        return tuple(self.params)


def json_list(value) -> list:
    if not value:
        return []
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def json_obj(value) -> dict | None:
    if not value:
        return None
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def format_params(n) -> str | None:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def thumb_url(uid: str, size: int = 320) -> str:
    """Thumbnail URL, carrying the renderer version.

    Thumbnails are served ``immutable`` with a one-year max-age, so a browser
    will not revalidate them.  That is only safe while the URL identifies the
    bytes: when the renderer changes (v2 gave videos real poster frames instead
    of placeholders) the URL has to change with it, or every existing client
    keeps its stale copy for a year.
    """
    from app.jobs.thumb_service import THUMB_VERSION
    return f"/api/v1/files/thumbnail?uid={uid}&size={size}&v={THUMB_VERSION}"


def raw_url(uid: str) -> str:
    return f"/api/v1/files/raw?uid={uid}"


def download_url(uid: str) -> str:
    return f"/api/v1/files/download?uid={uid}"


def tags_for(conn: sqlite3.Connection, uids: list[str]) -> dict[str, list[str]]:
    if not uids:
        return {}
    out: dict[str, list[str]] = {}
    for start in range(0, len(uids), 400):
        chunk = uids[start:start + 400]
        ph = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT at.uid, t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id "  # noqa: S608
            f"WHERE at.uid IN ({ph}) ORDER BY t.name", chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(str(r["uid"]), []).append(str(r["name"]))
    return out


def date_bucket(ts_ms: int | None, now_ms: int | None = None) -> str:
    if not ts_ms:
        return "Older"
    now = now_ms or dbmod.now_ms()
    day = 86_400_000
    delta = now - int(ts_ms)
    if delta < day:
        return "Today"
    if delta < 2 * day:
        return "Yesterday"
    if delta < 7 * day:
        return "This week"
    if delta < 31 * day:
        return "This month"
    try:
        import datetime as _dt

        d = _dt.datetime.fromtimestamp(int(ts_ms) / 1000, tz=_dt.UTC)
        return d.strftime("%B %Y")
    except (ValueError, OverflowError, OSError):
        return "Older"


def first_letter(name: str | None) -> str:
    s = (name or "").strip()
    if not s:
        return "#"
    ch = s[0].upper()
    return ch if ch.isalpha() else "#"


def search_uids(q: str | None, smart: bool, kinds: list[str],
                conn: sqlite3.Connection) -> tuple[list[int] | None, dict]:
    """Run the search engine and return the matching row ids for one kind."""
    if not (q or "").strip():
        return None, {"mode": "lexical", "smart_available": False}
    from ...search import hybrid

    res = hybrid.search(q, smart=bool(smart), kinds=kinds, limit=MAX_LIMIT * 2,
                        offset=0, conn=conn)
    ids: list[int] = []
    for item in res.items:
        try:
            ids.append(int(str(item["uid"]).split(":", 1)[1]))
        except (KeyError, IndexError, ValueError):
            continue
    return ids, {"mode": res.mode, "smart_available": res.smart_available,
                 "smart_reason": res.smart_reason}


def apply_id_filter(where: Where, column: str, ids: list[int] | None) -> bool:
    """Returns False when the search produced no candidates at all."""
    if ids is None:
        return True
    if not ids:
        return False
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        if start == 0:
            ph = ",".join("?" * len(chunk))
            where.add(f"{column} IN ({ph})", *chunk)
    return True


def order_by_search(ids: list[int] | None, column: str) -> str | None:
    """Preserve relevance order when the caller asked to sort by relevance."""
    if not ids:
        return None
    cases = " ".join(f"WHEN {int(i)} THEN {n}" for n, i in enumerate(ids[:900]))
    return f"CASE {column} {cases} ELSE 999999 END ASC"
