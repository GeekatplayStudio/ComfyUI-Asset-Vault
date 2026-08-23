"""Storage & maintenance rankings (REQUIREMENTS_R2 C10).

Everything here answers one question: *what can I delete, and what would it buy
me?*  The owner's drive is 86 % full, so this is a primary feature.

Three design rules run through the whole module:

1. **Sortable in SQL.**  ``reclaim_score``, ``size`` and ``age`` are all first-class
   ORDER BY keys computed inside the statement (C10.5), because a score computed
   after the LIMIT would page incorrectly.
2. **Measured and inferred are never mixed silently.**  Every reason an item
   appears carries a ``confidence`` of ``measured`` or ``inferred``, and the
   response says which method produced it.  "0 references" is a fact the index
   holds; "probably a duplicate of X because the names and sizes match" is a
   guess.  C11 paints the first amber and the second violet.
3. **Nothing here deletes anything.**  Cleanup goes through ``services/file_ops``
   with an explicit uid selection.  These functions only rank and total.

Duplicate detection degrades honestly with hashing coverage (C1 makes hashing
opt-in, and 2 of 237 model files are hashed on the owner's install): SHA-256
grouping is exact but only available for hashed files, so size+name and
cross-root name matching carry the rest and are reported as inferred.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time

from ...core import db as dbmod
from ...core.errors import ValidationError
from . import (
    ListResult,
    Where,
    clamp_page,
    meta_dict,
    page_dict,
)

KINDS = ("model", "output")

#: ORDER BY fragments.  ``reclaim`` is the default the owner sees first.
SORTS = {
    "reclaim": "reclaim_score DESC, size DESC",
    "size": "size DESC",
    "age": "age_days DESC",
    "name": "name COLLATE NOCASE ASC",
    "score": "reclaim_score DESC, size DESC",
}
DEFAULT_SORT = "reclaim"

REASONS = ("unused", "duplicate", "superseded", "stale", "large", "integrity",
           "orphan_output", "non_media", "protected")

GB = 1024 ** 3
MB = 1024 ** 2
DAY_MS = 86_400_000

#: A judgement call the UI must be able to explain, so it lives here and is
#: documented rather than buried in a magic number.
WEIGHTS = {
    "unused": 35, "size": 25, "age": 20, "duplicate_hash": 15,
    "duplicate_inferred": 10, "superseded": 10, "integrity": 10,
    "orphan_output": 25, "non_media": 10, "no_provenance": 5, "protected": -40,
}

# ---------------------------------------------------------------------------
# Score expressions
# ---------------------------------------------------------------------------

_MODEL_SIZE_SCORE = """(CASE
    WHEN v.total_size >= 34359738368 THEN 25 WHEN v.total_size >= 17179869184 THEN 22
    WHEN v.total_size >=  8589934592 THEN 19 WHEN v.total_size >=  4294967296 THEN 16
    WHEN v.total_size >=  2147483648 THEN 13 WHEN v.total_size >=  1073741824 THEN 10
    WHEN v.total_size >=   536870912 THEN  7 WHEN v.total_size >=   134217728 THEN  4
    ELSE 1 END)"""

_OUTPUT_SIZE_SCORE = """(CASE
    WHEN o.size >= 268435456 THEN 25 WHEN o.size >= 67108864 THEN 20
    WHEN o.size >=  16777216 THEN 15 WHEN o.size >=  4194304 THEN 10
    WHEN o.size >=   1048576 THEN  5 ELSE 2 END)"""

_MODEL_AGE_DAYS = ("(CASE WHEN v.mtime_ns > 0 "
                   "THEN (? - v.mtime_ns / 1000000) / 86400000.0 ELSE 0 END)")
_OUTPUT_AGE_DAYS = ("(CASE WHEN o.created_at_file > 0 "
                    "THEN (? - o.created_at_file) / 86400000.0 ELSE 0 END)")


def _age_score(expr: str, top: int) -> str:
    hi = top
    return (f"(CASE WHEN {expr} >= 730 THEN {hi} WHEN {expr} >= 365 THEN {hi * 3 // 4} "
            f"WHEN {expr} >= 180 THEN {hi // 2} WHEN {expr} >= 90 THEN {hi * 7 // 20} "
            f"WHEN {expr} >= 30 THEN {hi // 10} ELSE 0 END)")


# ---------------------------------------------------------------------------
# Signals computed in Python: duplicates and supersession
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^(?P<base>.*?)[._\- ]?v(?P<num>\d+(?:[._]\d+)*)$", re.I)
#: Quantisation / precision suffixes are variants, never versions.  Treating
#: ``model_fp8`` as superseded by ``model_fp16`` would be wrong and expensive.
_VARIANT_TOKENS = frozenset({
    "fp8", "fp16", "fp32", "bf16", "f16", "f32", "q4", "q5", "q6", "q8",
    "q4_0", "q4_k_m", "q5_k_m", "q8_0", "int8", "int4", "gguf", "pruned",
    "emaonly", "ema", "distilled", "turbo", "lightning", "scaled", "fp8mixed",
})

_signal_lock = threading.RLock()
_signal_cache: dict[str, tuple[tuple, dict]] = {}


def _version_key(stem: str) -> tuple[str, tuple[int, ...]] | None:
    """``flux_dev_v2.1`` -> ``('flux_dev', (2, 1))``; ``flux_fp8`` -> ``None``."""
    m = _VERSION_RE.match(str(stem or "").strip())
    if not m:
        return None
    base = m.group("base").strip("._- ")
    if not base:
        return None
    if base.rsplit("_", 1)[-1].lower() in _VARIANT_TOKENS:
        return None
    try:
        nums = tuple(int(p) for p in re.split(r"[._]", m.group("num")) if p != "")
    except ValueError:
        return None
    return base.lower(), nums or (0,)


def _fingerprint(conn: sqlite3.Connection) -> tuple:
    """``PRAGMA data_version`` alone: it moves on every committed write from any
    path, so no mutation site has to remember this cache exists."""
    return (dbmod.data_version(conn),)


def model_signals(conn: sqlite3.Connection | None = None) -> dict:
    """Duplicate and supersession signals for every model, keyed by model id.

    Cached on a cheap fingerprint of the ``models`` / ``model_files`` tables so a
    paged UI does not recompute it for every page.
    """
    conn = conn or dbmod.get_ro()
    key = os.path.normcase(str(dbmod.db_path()))
    fp = _fingerprint(conn)
    with _signal_lock:
        cached = _signal_cache.get(key)
        if cached is not None and cached[0] == fp:
            return cached[1]

    rows = dbmod.rows(
        conn,
        "SELECT f.id AS file_id, f.model_id, f.filename, f.stem, f.size, f.sha256, "
        "f.root_id, f.abs_path, m.name, m.has_update, m.latest_version_name "
        "FROM model_files f JOIN models m ON m.id = f.model_id "
        "WHERE f.missing_since IS NULL",
    )

    by_hash: dict[str, list[dict]] = {}
    by_name_size: dict[tuple, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    by_version: dict[str, list[tuple[tuple[int, ...], dict]]] = {}
    records: list[dict] = []

    for r in rows:
        rec = {
            "file_id": int(r["file_id"]), "model_id": int(r["model_id"]),
            "filename": str(r["filename"] or ""), "stem": str(r["stem"] or ""),
            "size": int(r["size"] or 0), "sha256": r["sha256"],
            "root_id": r["root_id"], "abs_path": str(r["abs_path"] or ""),
            "name": str(r["name"] or ""), "has_update": bool(r["has_update"]),
            "latest": r["latest_version_name"],
        }
        records.append(rec)
        if rec["sha256"]:
            by_hash.setdefault(str(rec["sha256"]).lower(), []).append(rec)
        lower = rec["filename"].lower()
        if lower:
            by_name_size.setdefault((lower, rec["size"]), []).append(rec)
            by_name.setdefault(lower, []).append(rec)
        vk = _version_key(rec["stem"])
        if vk is not None:
            by_version.setdefault(vk[0], []).append((vk[1], rec))

    out: dict[int, dict] = {}

    def _mark(rec: dict, method: str, confidence: str, group: str,
              peers: list[dict]) -> None:
        entry = out.setdefault(rec["model_id"], {})
        # sha256 beats every heuristic; never downgrade an already-measured mark.
        if entry.get("dup_confidence") == "measured" and confidence != "measured":
            return
        entry.update({"dup_method": method, "dup_confidence": confidence,
                      "dup_group": group,
                      "dup_peers": [p["model_id"] for p in peers
                                    if p["model_id"] != rec["model_id"]][:8]})

    for digest, group in by_hash.items():
        if len(group) < 2:
            continue
        for rec in group:
            _mark(rec, "sha256", "measured", digest[:16], group)

    for (lower, size), group in by_name_size.items():
        if len(group) < 2 or all(r["sha256"] for r in group):
            continue
        for rec in group:
            _mark(rec, "name+size", "inferred", f"{lower}@{size}", group)

    for lower, group in by_name.items():
        roots = {r["root_id"] for r in group}
        if len(group) < 2 or len(roots) < 2:
            continue
        for rec in group:
            _mark(rec, "name across roots", "inferred", lower, group)

    for base, entries in by_version.items():
        if len(entries) < 2:
            continue
        newest = max(nums for nums, _rec in entries)
        winner = next(rec for nums, rec in entries if nums == newest)
        for nums, rec in entries:
            if nums == newest:
                continue
            out.setdefault(rec["model_id"], {}).update({
                "superseded_by": winner["name"] or winner["filename"],
                "superseded_source": "filename",
                "superseded_confidence": "inferred",
                "superseded_base": base,
            })

    for rec in records:
        if not rec["has_update"]:
            continue
        # A Civitai-declared newer version is a published fact, not a guess, and
        # it outranks any filename reading of the same model.
        out.setdefault(rec["model_id"], {}).update({
            "superseded_by": rec["latest"] or "a newer published version",
            "superseded_source": "civitai",
            "superseded_confidence": "measured",
        })

    with _signal_lock:
        _signal_cache[key] = (fp, out)
    return out


def invalidate_signals() -> None:
    with _signal_lock:
        _signal_cache.clear()


_SIGNAL_DDL = (
    "CREATE TEMP TABLE IF NOT EXISTS storage_model_signals ("
    "model_id INTEGER PRIMARY KEY, dup_method TEXT, dup_group TEXT, "
    "dup_confidence TEXT, superseded_by TEXT, superseded_source TEXT, "
    "superseded_confidence TEXT)"
)


def _stage_signals(conn: sqlite3.Connection) -> dict:
    """Publish the Python-computed signals into a TEMP table.

    A temp table is writable even on the read-only connection (it lives in the
    separate ``temp`` database, which ``PRAGMA temp_store = MEMORY`` keeps in
    RAM), and joining it beats binding a few thousand ids into every statement.
    """
    signals = model_signals(conn)
    conn.execute(_SIGNAL_DDL)
    conn.execute("DELETE FROM storage_model_signals")
    if signals:
        conn.executemany(
            "INSERT INTO storage_model_signals(model_id,dup_method,dup_group,"
            "dup_confidence,superseded_by,superseded_source,superseded_confidence) "
            "VALUES (?,?,?,?,?,?,?)",
            [(int(mid), s.get("dup_method"), s.get("dup_group"),
              s.get("dup_confidence"), s.get("superseded_by"),
              s.get("superseded_source"), s.get("superseded_confidence"))
             for mid, s in signals.items()],
        )
    if conn.in_transaction:
        # Defence in depth.  ``core/db`` puts read-only connections in autocommit
        # precisely so a temp-table write cannot pin a read snapshot; if that
        # ever regresses, this connection must still not leave here holding one.
        conn.commit()
    return signals


# ---------------------------------------------------------------------------
# The unified candidate query
# ---------------------------------------------------------------------------

#: Written as templates with ``__AGE__`` / ``__SCORE__`` markers rather than
#: f-strings.  Both substitutions are module constants - no caller value ever
#: reaches the SQL text, every filter is bound - and spelling it this way makes
#: that structurally obvious instead of something a reader has to verify.
_MODEL_SQL = """
    SELECT 'model' AS kind, v.id AS row_id, ('model:' || v.id) AS uid,
           v.name AS name, v.filename AS filename, v.ext AS ext,
           v.abs_path AS abs_path, v.rel_path AS rel_path, v.folder AS folder,
           v.root_id AS root_id, v.category AS category, NULL AS media_kind,
           v.model_role AS role,
           v.total_size AS size,
           (v.mtime_ns / 1000000) AS modified_at,
           (v.mtime_ns / 1000000) AS created_at,
           __AGE__ AS age_days,
           v.workflow_count AS workflow_count, v.output_count AS output_count,
           v.favorite AS favorite, v.user_rating AS user_rating,
           v.integrity AS integrity, v.hash_state AS hash_state, v.sha256 AS sha256,
           s.dup_method AS dup_method, s.dup_group AS dup_group,
           s.dup_confidence AS dup_confidence, s.superseded_by AS superseded_by,
           s.superseded_source AS superseded_source,
           s.superseded_confidence AS superseded_confidence,
           NULL AS model_name, NULL AS model_id, NULL AS has_metadata,
           __SCORE__ AS reclaim_score
    FROM v_model_list v
    LEFT JOIN storage_model_signals s ON s.model_id = v.id
    WHERE v.missing_since IS NULL
"""

_OUTPUT_SQL = """
    SELECT 'output' AS kind, o.id AS row_id, ('output:' || o.id) AS uid,
           o.filename AS name, o.filename AS filename, o.ext AS ext,
           o.abs_path AS abs_path, o.rel_path AS rel_path, o.folder AS folder,
           o.root_id AS root_id, NULL AS category, o.media_kind AS media_kind,
           NULL AS role,
           o.size AS size,
           (o.mtime_ns / 1000000) AS modified_at, o.created_at_file AS created_at,
           __AGE__ AS age_days,
           0 AS workflow_count, 0 AS output_count,
           o.favorite AS favorite, o.user_rating AS user_rating,
           'ok' AS integrity, NULL AS hash_state, NULL AS sha256,
           NULL AS dup_method, NULL AS dup_group, NULL AS dup_confidence,
           NULL AS superseded_by, NULL AS superseded_source,
           NULL AS superseded_confidence,
           o.model_name AS model_name, o.model_id AS model_id,
           o.has_metadata AS has_metadata,
           __SCORE__ AS reclaim_score
    FROM outputs o
    WHERE o.missing_since IS NULL
"""


def _render(template: str, age: str, score: str) -> tuple[str, list]:
    """Substitute the two computed expressions and count the bound placeholders.

    The age expression carries the single ``?`` and is expanded once in the
    projection and five times inside the score's CASE ladder.  Counting the
    placeholders in the finished statement is the only binding that cannot drift
    when a bucket is added.
    """
    sql = template.replace("__AGE__", age).replace("__SCORE__", score)
    return sql, [None] * sql.count("?")


def _model_select(now: int) -> tuple[str, list]:
    score = "".join((
        "MAX(0, MIN(100, ",
        f"(CASE WHEN v.workflow_count = 0 AND v.output_count = 0 "
        f"THEN {WEIGHTS['unused']} ELSE 0 END) + ",
        _MODEL_SIZE_SCORE, " + ", _age_score(_MODEL_AGE_DAYS, WEIGHTS["age"]), " + ",
        f"(CASE WHEN s.dup_method = 'sha256' THEN {WEIGHTS['duplicate_hash']} ",
        f"WHEN s.dup_method IS NOT NULL THEN {WEIGHTS['duplicate_inferred']} "
        f"ELSE 0 END) + ",
        f"(CASE WHEN s.superseded_by IS NOT NULL THEN {WEIGHTS['superseded']} "
        f"ELSE 0 END) + ",
        f"(CASE WHEN v.integrity <> 'ok' THEN {WEIGHTS['integrity']} ELSE 0 END) + ",
        f"(CASE WHEN v.favorite = 1 OR COALESCE(v.user_rating, 0) >= 4 "
        f"THEN {WEIGHTS['protected']} ELSE 0 END)))",
    ))
    sql, slots = _render(_MODEL_SQL, _MODEL_AGE_DAYS, score)
    return sql, [now] * len(slots)


def _output_select(now: int) -> tuple[str, list]:
    score = "".join((
        "MAX(0, MIN(100, ",
        f"(CASE WHEN o.model_id IS NULL AND o.model_name IS NOT NULL "
        f"THEN {WEIGHTS['orphan_output']} ELSE 0 END) + ",
        _OUTPUT_SIZE_SCORE, " + ", _age_score(_OUTPUT_AGE_DAYS, 25), " + ",
        f"(CASE WHEN o.media_kind NOT IN ('image', 'video') "
        f"THEN {WEIGHTS['non_media']} ELSE 0 END) + ",
        f"(CASE WHEN o.workflow_id IS NULL AND o.has_metadata = 0 "
        f"THEN {WEIGHTS['no_provenance']} ELSE 0 END) + ",
        f"(CASE WHEN o.favorite = 1 OR COALESCE(o.user_rating, 0) >= 4 "
        f"THEN {WEIGHTS['protected']} ELSE 0 END)))",
    ))
    sql, slots = _render(_OUTPUT_SQL, _OUTPUT_AGE_DAYS, score)
    return sql, [now] * len(slots)


def _reason_clause(reason: str, min_age_days: int) -> str:
    return {
        "unused": "(kind = 'model' AND workflow_count = 0 AND output_count = 0)",
        "duplicate": "dup_method IS NOT NULL",
        "superseded": "superseded_by IS NOT NULL",
        "stale": f"age_days >= {int(min_age_days)}",
        "large": ("(CASE WHEN kind = 'model' THEN size >= 8589934592 "
                  "ELSE size >= 67108864 END)"),
        "integrity": "integrity NOT IN ('ok', '')",
        "orphan_output": "(kind = 'output' AND model_id IS NULL "
                         "AND model_name IS NOT NULL)",
        "non_media": "(kind = 'output' AND media_kind NOT IN ('image', 'video'))",
        "protected": "(favorite = 1 OR COALESCE(user_rating, 0) >= 4)",
    }[reason]


def _base_query(filters: dict, now: int) -> tuple[str, list]:
    filters = filters or {}
    kinds = [k for k in (filters.get("kind") or list(KINDS)) if k in KINDS]
    if not kinds:
        raise ValidationError(f"kind must be one of {', '.join(KINDS)}.",
                              details={"allowed": list(KINDS)})

    parts: list[str] = []
    params: list = []
    if "model" in kinds:
        sql, p = _model_select(now)
        parts.append(sql)
        params.extend(p)
    if "output" in kinds:
        sql, p = _output_select(now)
        parts.append(sql)
        params.extend(p)
    inner = "\nUNION ALL\n".join(parts)

    w = Where()
    w.any_of("category", filters.get("category"))
    w.any_of("role", filters.get("role"))
    w.any_of("media_kind", filters.get("media_kind"))
    w.any_of("root_id", filters.get("root_id"))
    w.prefix("folder", filters.get("folder"))
    w.gte("size", filters.get("min_size"))
    w.lte("size", filters.get("max_size"))
    w.gte("age_days", filters.get("older_than_days"))
    if filters.get("q"):
        w.add("name LIKE ? COLLATE NOCASE", f"%{str(filters['q'])[:120]}%")

    reasons = [r for r in (filters.get("reason") or []) if r in REASONS]
    if reasons:
        min_age = int(filters.get("stale_days") or 180)
        w.add("(" + " OR ".join(_reason_clause(r, min_age) for r in reasons) + ")")
    if not filters.get("include_protected", True):
        w.add("NOT (favorite = 1 OR COALESCE(user_rating, 0) >= 4)")

    # Both halves are module-built SQL; every caller value is in ``w.args()``.
    outer = "SELECT * FROM (\n" + inner + "\n) WHERE " + w.sql()  # noqa: S608
    return outer, [*params, *w.args()]


def _reasons_for(row: sqlite3.Row) -> list[dict]:
    """The human-readable 'why is this here', each tagged measured vs inferred."""
    out: list[dict] = []
    kind = str(row["kind"])
    if kind == "model" and not row["workflow_count"] and not row["output_count"]:
        out.append({"code": "unused", "label": "Referenced by no workflow and no output",
                    "confidence": "measured", "weight": WEIGHTS["unused"]})
    if row["dup_method"]:
        method = str(row["dup_method"])
        confidence = str(row["dup_confidence"] or "inferred")
        out.append({
            "code": "duplicate",
            "label": ("Identical content (SHA-256) to another file"
                      if method == "sha256"
                      else f"Possible duplicate - matched by {method}"),
            "confidence": confidence, "method": method,
            "weight": (WEIGHTS["duplicate_hash"] if method == "sha256"
                       else WEIGHTS["duplicate_inferred"]),
        })
    if row["superseded_by"]:
        source = str(row["superseded_source"] or "filename")
        out.append({
            "code": "superseded",
            "label": f"A newer version is present: {row['superseded_by']}",
            "confidence": str(row["superseded_confidence"] or "inferred"),
            "method": source, "weight": WEIGHTS["superseded"],
        })
    size = int(row["size"] or 0)
    threshold = 8 * GB if kind == "model" else 64 * MB
    if size >= threshold:
        out.append({
            "code": "large",
            "label": f"Large file ({size / GB:.1f} GB)" if size >= GB
                     else f"Large file ({size / MB:.0f} MB)",
            "confidence": "measured", "weight": WEIGHTS["size"],
        })
    age = float(row["age_days"] or 0)
    if age >= 180:
        out.append({"code": "stale", "label": f"Untouched for {int(age)} days",
                    "confidence": "measured", "weight": WEIGHTS["age"]})
    if kind == "model" and str(row["integrity"] or "ok") != "ok":
        out.append({"code": "integrity",
                    "label": f"Failed the integrity check ({row['integrity']})",
                    "confidence": "measured", "weight": WEIGHTS["integrity"]})
    if kind == "output" and row["model_id"] is None and row["model_name"]:
        out.append({"code": "orphan_output",
                    "label": f"Its source model is gone ({row['model_name']})",
                    "confidence": "measured", "weight": WEIGHTS["orphan_output"]})
    if kind == "output" and str(row["media_kind"] or "") not in ("image", "video"):
        out.append({"code": "non_media",
                    "label": f"Not an image or video ({row['media_kind'] or 'unknown'})",
                    "confidence": "measured", "weight": WEIGHTS["non_media"]})
    if row["favorite"] or int(row["user_rating"] or 0) >= 4:
        out.append({"code": "protected",
                    "label": "Marked favourite or rated 4+ - excluded from suggestions",
                    "confidence": "measured", "weight": WEIGHTS["protected"]})
    return out


def _row_to_item(row: sqlite3.Row) -> dict:
    reasons = _reasons_for(row)
    inferred = [r for r in reasons if r["confidence"] == "inferred"]
    return {
        "uid": str(row["uid"]), "kind": str(row["kind"]), "id": int(row["row_id"]),
        "name": row["name"], "filename": row["filename"], "ext": row["ext"],
        "category": row["category"], "media_kind": row["media_kind"],
        # Carried so the UI's per-role coverage warning needs no extra round trip.
        "role": row["role"],
        "folder": row["folder"] or "", "rel_path": row["rel_path"],
        "abs_path": row["abs_path"], "root_id": row["root_id"],
        "size": int(row["size"] or 0),
        "modified_at": int(row["modified_at"] or 0),
        "created_at": int(row["created_at"] or 0),
        "age_days": int(row["age_days"] or 0),
        "counts": {"workflows": int(row["workflow_count"] or 0),
                   "outputs": int(row["output_count"] or 0)},
        "hash_state": row["hash_state"],
        "reclaim_score": int(row["reclaim_score"] or 0),
        "confidence": "inferred" if inferred else "measured",
        "reasons": reasons,
        "protected": bool(row["favorite"] or int(row["user_rating"] or 0) >= 4),
        "duplicate_group": row["dup_group"],
        "thumbnail_url": f"/api/v1/files/thumbnail?uid={row['uid']}&size=160",
    }


def candidates(filters: dict | None = None, sort: str = DEFAULT_SORT,
               limit: int = 100, offset: int = 0,
               conn: sqlite3.Connection | None = None) -> ListResult:
    """The paged detail table behind the Storage view (C10.2, C10.3, C10.5)."""
    t0 = time.perf_counter()
    conn = conn or dbmod.get_ro()
    limit, offset = clamp_page(limit, offset)
    order = SORTS.get(sort or DEFAULT_SORT)
    if order is None:
        raise ValidationError(f"Unsupported sort '{sort}'.",
                              details={"allowed": sorted(SORTS)})

    _stage_signals(conn)
    now = dbmod.now_ms()
    base, params = _base_query(filters or {}, now)

    agg = dbmod.one(
        conn, f"SELECT COUNT(*) n, COALESCE(SUM(size), 0) b FROM ({base})", tuple(params))  # noqa: S608
    total = int(agg["n"] or 0)
    total_bytes = int(agg["b"] or 0)

    rows = dbmod.rows(
        conn,
        f"SELECT * FROM ({base}) ORDER BY {order}, uid LIMIT ? OFFSET ?",  # noqa: S608
        (*params, limit, offset),
    )
    items = [_row_to_item(r) for r in rows]
    return ListResult(
        items=items,
        page=page_dict(limit, offset, total, len(items)),
        meta=meta_dict(t0, sort=sort, matched_bytes=total_bytes,
                       page_bytes=sum(i["size"] for i in items),
                       weights=WEIGHTS),
    )


def _split_uids(uids) -> tuple[list[int], list[int], list[str]]:
    model_ids: list[int] = []
    output_ids: list[int] = []
    unknown: list[str] = []
    for uid in list(uids or [])[:1000]:
        kind, _sep, num = str(uid).partition(":")
        try:
            row_id = int(num)
        except (TypeError, ValueError):
            unknown.append(str(uid))
            continue
        if kind == "model":
            model_ids.append(row_id)
        elif kind == "output":
            output_ids.append(row_id)
        else:
            unknown.append(str(uid))
    return model_ids, output_ids, unknown


def selection_sizes(uids: list[str],
                    conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """``uid -> size`` for a selection, read while the rows still exist.

    ``cleanup`` calls this *before* deleting so it can report exactly what each
    successfully removed item was worth, instead of pro-rating a total across a
    partial failure.
    """
    conn = conn or dbmod.get_ro()
    model_ids, output_ids, _unknown = _split_uids(uids)
    out: dict[str, int] = {}
    for table, ids, kind, size_col in (
        ("models", model_ids, "model", "total_size"),
        ("outputs", output_ids, "output", "size"),
    ):
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            ph = ",".join("?" * len(chunk))
            for r in dbmod.rows(
                conn,
                f"SELECT id, {size_col} AS sz FROM {table} WHERE id IN ({ph})",  # noqa: S608
                tuple(chunk),
            ):
                out[f"{kind}:{int(r['id'])}"] = int(r["sz"] or 0)
    return out


def selection_total(uids: list[str],
                    conn: sqlite3.Connection | None = None) -> dict:
    """Exact byte total for an explicit selection (C10.2 / C10.4).

    Sizes come from the index, so this is the number to show *before* anything is
    deleted; ``file_ops`` reports what was actually freed afterwards.
    """
    conn = conn or dbmod.get_ro()
    model_ids, output_ids, unknown = _split_uids(uids)

    total = 0
    counts = {"model": 0, "output": 0}
    protected: list[str] = []
    for table, ids, kind, size_col, id_col in (
        ("models", model_ids, "model", "total_size", "id"),
        ("outputs", output_ids, "output", "size", "id"),
    ):
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            ph = ",".join("?" * len(chunk))
            rows = dbmod.rows(
                conn,
                f"SELECT {id_col} AS rid, {size_col} AS sz, favorite, user_rating "  # noqa: S608
                f"FROM {table} WHERE {id_col} IN ({ph})", tuple(chunk),
            )
            for r in rows:
                total += int(r["sz"] or 0)
                counts[kind] += 1
                if r["favorite"] or int(r["user_rating"] or 0) >= 4:
                    protected.append(f"{kind}:{int(r['rid'])}")
    return {
        "requested": len(uids or []), "resolved": counts["model"] + counts["output"],
        "by_kind": counts, "bytes": total, "unknown_uids": unknown[:20],
        "protected_uids": protected[:50], "protected_count": len(protected),
    }


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def duplicate_groups(limit: int = 100, offset: int = 0, method: str | None = None,
                     conn: sqlite3.Connection | None = None) -> ListResult:
    """Duplicate sets, each stating the method that found it (C10.3).

    ``sha256`` groups are exact.  ``name+size`` and ``name across roots`` are
    candidates the owner must confirm - two files can share a name and a byte
    count and still differ.  Hash the group to promote it to certainty.
    """
    t0 = time.perf_counter()
    conn = conn or dbmod.get_ro()
    limit, offset = clamp_page(limit, offset)
    signals = model_signals(conn)

    grouped: dict[tuple[str, str], list[int]] = {}
    for model_id, sig in signals.items():
        if not sig.get("dup_method"):
            continue
        if method and sig["dup_method"] != method:
            continue
        grouped.setdefault((sig["dup_method"], str(sig.get("dup_group") or "")),
                           []).append(int(model_id))

    ids = sorted({i for group in grouped.values() for i in group})
    info: dict[int, dict] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        ph = ",".join("?" * len(chunk))
        for r in dbmod.rows(
            conn,
            f"SELECT id, name, category, total_size, favorite, user_rating "  # noqa: S608
            f"FROM models WHERE id IN ({ph})", tuple(chunk),
        ):
            info[int(r["id"])] = {
                "uid": f"model:{int(r['id'])}", "name": r["name"],
                "category": r["category"], "size": int(r["total_size"] or 0),
                "protected": bool(r["favorite"] or int(r["user_rating"] or 0) >= 4),
            }
    paths: dict[int, str] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        ph = ",".join("?" * len(chunk))
        for r in dbmod.rows(
            conn,
            f"SELECT model_id, abs_path FROM model_files WHERE model_id IN ({ph})",  # noqa: S608
            tuple(chunk),
        ):
            paths.setdefault(int(r["model_id"]), str(r["abs_path"]))

    groups: list[dict] = []
    for (dup_method, key), members in grouped.items():
        items = [{**info[m], "abs_path": paths.get(m)} for m in members if m in info]
        if len(items) < 2:
            continue
        items.sort(key=lambda i: (-i["size"], i["name"] or ""))
        total = sum(i["size"] for i in items)
        keeper = items[0]
        groups.append({
            "key": key, "method": dup_method,
            "confidence": "measured" if dup_method == "sha256" else "inferred",
            "count": len(items), "bytes": total,
            "reclaimable_bytes": total - keeper["size"],
            "suggested_keep_uid": keeper["uid"],
            "items": items,
        })
    groups.sort(key=lambda g: -g["reclaimable_bytes"])
    window = groups[offset:offset + limit]

    hashed = int(dbmod.scalar(
        conn, "SELECT COUNT(*) FROM model_files WHERE sha256 IS NOT NULL") or 0)
    files = int(dbmod.scalar(
        conn, "SELECT COUNT(*) FROM model_files WHERE missing_since IS NULL") or 0)
    return ListResult(
        items=window,
        page=page_dict(limit, offset, len(groups), len(window)),
        meta=meta_dict(
            t0,
            reclaimable_bytes=sum(g["reclaimable_bytes"] for g in groups),
            hash_coverage={"hashed": hashed, "total": files,
                           "exact_detection_available": hashed > 1},
            methods=["sha256", "name+size", "name across roots"],
        ),
    )


# ---------------------------------------------------------------------------
# Summary aggregates
# ---------------------------------------------------------------------------

def indexed_bytes(conn: sqlite3.Connection | None = None) -> dict:
    """What the index knows it is holding, per kind and per root."""
    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, "SELECT * FROM v_vault_stats")
    stats = dict(row) if row else {}
    by_root: dict[int, dict] = {}
    for r in dbmod.rows(
        conn,
        "SELECT root_id, COUNT(*) n, COALESCE(SUM(size), 0) b FROM model_files "
        "WHERE missing_since IS NULL GROUP BY root_id",
    ):
        by_root.setdefault(int(r["root_id"] or 0), {})["models"] = {
            "count": int(r["n"]), "bytes": int(r["b"])}
    for r in dbmod.rows(
        conn,
        "SELECT root_id, COUNT(*) n, COALESCE(SUM(size), 0) b FROM outputs "
        "WHERE missing_since IS NULL GROUP BY root_id",
    ):
        by_root.setdefault(int(r["root_id"] or 0), {})["outputs"] = {
            "count": int(r["n"]), "bytes": int(r["b"])}
    for r in dbmod.rows(
        conn,
        "SELECT root_id, COUNT(*) n FROM workflows WHERE missing_since IS NULL "
        "GROUP BY root_id",
    ):
        by_root.setdefault(int(r["root_id"] or 0), {})["workflows"] = {
            "count": int(r["n"]), "bytes": 0}
    return {
        "models": {"count": int(stats.get("model_files") or 0),
                   "bytes": int(stats.get("models_bytes") or 0)},
        "outputs": {"count": int(stats.get("outputs") or 0),
                    "bytes": int(stats.get("outputs_bytes") or 0)},
        "node_packages": {"count": int(stats.get("node_packages") or 0), "bytes": 0},
        "workflows": {"count": int(stats.get("workflows") or 0), "bytes": 0},
        "by_root": by_root,
        "hashed": int(stats.get("models_hashed") or 0),
    }


def reclaimable_summary(stale_days: int = 180,
                        conn: sqlite3.Connection | None = None) -> dict:
    """The headline "where could a terabyte come from" numbers (C10.1, C10.3)."""
    conn = conn or dbmod.get_ro()
    signals = _stage_signals(conn)
    now = dbmod.now_ms()
    cutoff = now - int(stale_days) * DAY_MS

    unused = dbmod.one(
        conn, "SELECT COUNT(*) n, COALESCE(SUM(total_size), 0) b FROM models "
              "WHERE missing_since IS NULL AND workflow_count = 0 AND output_count = 0")
    unused_unprotected = dbmod.one(
        conn, "SELECT COUNT(*) n, COALESCE(SUM(total_size), 0) b FROM models "
              "WHERE missing_since IS NULL AND workflow_count = 0 AND output_count = 0 "
              "AND favorite = 0 AND COALESCE(user_rating, 0) < 4")
    stale_models = dbmod.one(
        conn,
        "SELECT COUNT(*) n, COALESCE(SUM(v.total_size), 0) b FROM v_model_list v "
        "WHERE v.missing_since IS NULL AND v.mtime_ns > 0 "
        "AND v.mtime_ns / 1000000 < ?", (cutoff,))
    old_outputs = dbmod.one(
        conn, "SELECT COUNT(*) n, COALESCE(SUM(size), 0) b FROM outputs "
              "WHERE missing_since IS NULL AND created_at_file < ?", (cutoff,))
    orphan_outputs = dbmod.one(
        conn, "SELECT COUNT(*) n, COALESCE(SUM(size), 0) b FROM outputs "
              "WHERE missing_since IS NULL AND model_id IS NULL "
              "AND model_name IS NOT NULL")
    non_media = dbmod.one(
        conn, "SELECT COUNT(*) n, COALESCE(SUM(size), 0) b FROM outputs "
              "WHERE missing_since IS NULL AND media_kind NOT IN ('image', 'video')")
    integrity = dbmod.one(
        conn, "SELECT COUNT(*) n, COALESCE(SUM(total_size), 0) b FROM models "
              "WHERE missing_since IS NULL AND integrity <> 'ok'")
    trash = dbmod.one(
        conn, "SELECT COUNT(*) n, COALESCE(SUM(size), 0) b FROM trash_items")

    dup_ids = [int(m) for m, s in signals.items() if s.get("dup_method")]
    dup_bytes = 0
    dup_exact = 0
    if dup_ids:
        for start in range(0, len(dup_ids), 400):
            chunk = dup_ids[start:start + 400]
            ph = ",".join("?" * len(chunk))
            dup_bytes += int(dbmod.scalar(
                conn,
                f"SELECT COALESCE(SUM(total_size), 0) FROM models WHERE id IN ({ph})",  # noqa: S608
                tuple(chunk)) or 0)
        dup_exact = sum(1 for m in dup_ids
                        if signals[m].get("dup_confidence") == "measured")
    superseded_ids = [int(m) for m, s in signals.items() if s.get("superseded_by")]
    superseded_bytes = 0
    for start in range(0, len(superseded_ids), 400):
        chunk = superseded_ids[start:start + 400]
        ph = ",".join("?" * len(chunk))
        superseded_bytes += int(dbmod.scalar(
            conn,
            f"SELECT COALESCE(SUM(total_size), 0) FROM models WHERE id IN ({ph})",  # noqa: S608
            tuple(chunk)) or 0)

    def block(key: str, label: str, row, confidence: str, **extra) -> dict:
        return {"key": key, "label": label, "count": int(row["n"] or 0),
                "bytes": int(row["b"] or 0), "confidence": confidence, **extra}

    return {
        "stale_days": int(stale_days),
        "groups": [
            block("unused_models", "Models referenced by no workflow and no output",
                  unused, "measured",
                  unprotected_count=int(unused_unprotected["n"] or 0),
                  unprotected_bytes=int(unused_unprotected["b"] or 0),
                  reason="unused"),
            {"key": "duplicates", "label": "Duplicate or near-duplicate models",
             "count": len(dup_ids), "bytes": dup_bytes,
             "confidence": "measured" if dup_exact == len(dup_ids) and dup_ids
                           else "inferred",
             "exact_count": dup_exact, "reason": "duplicate"},
            {"key": "superseded", "label": "Superseded by a newer version",
             "count": len(superseded_ids), "bytes": superseded_bytes,
             "confidence": "inferred", "reason": "superseded"},
            block("stale_models", f"Models untouched for {stale_days}+ days",
                  stale_models, "measured", reason="stale"),
            block("old_outputs", f"Outputs older than {stale_days} days",
                  old_outputs, "measured", reason="stale"),
            block("orphan_outputs", "Outputs whose source model is gone",
                  orphan_outputs, "measured", reason="orphan_output"),
            block("non_media_outputs", "Non-image, non-video files in output/",
                  non_media, "measured", reason="non_media"),
            block("integrity", "Models that failed the integrity check",
                  integrity, "measured", reason="integrity"),
            block("trash", "Already in the vault trash", trash, "measured",
                  reason=None),
        ],
    }
