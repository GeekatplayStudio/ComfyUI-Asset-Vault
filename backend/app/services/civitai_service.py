"""Civitai enrichment - a consumer of the hash job, never a coupler.

Hardened per ARCHITECTURE 8.4: kill-switch before any socket, HTTP cache,
circuit breaker, bounded retries, and outbound requests that carry only hashes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import time
from typing import Any

from ..core import config_service
from ..core import db as dbmod
from ..core.errors import FeatureUnavailable
from ..search import sync

log = logging.getLogger(__name__)

API_BASE = "https://civitai.com/api/v1"
TIMEOUT_S = 10.0
RETRIES = 2
CACHE_TTL_MS = 24 * 60 * 60 * 1000
NOT_FOUND_TTL_MS = 7 * 24 * 60 * 60 * 1000
BREAKER_THRESHOLD = 5
BREAKER_COOLDOWN_S = 300

_breaker = {"failures": 0, "open_until": 0.0}


def _breaker_open() -> bool:
    return time.monotonic() < _breaker["open_until"]


def _record_failure() -> None:
    _breaker["failures"] += 1
    if _breaker["failures"] >= BREAKER_THRESHOLD:
        _breaker["open_until"] = time.monotonic() + BREAKER_COOLDOWN_S
        _breaker["failures"] = 0
        log.warning("Civitai circuit breaker opened for %ds", BREAKER_COOLDOWN_S)


def _record_success() -> None:
    _breaker["failures"] = 0
    _breaker["open_until"] = 0.0


def breaker_state() -> dict:
    return {"open": _breaker_open(), "failures": _breaker["failures"],
            "reopens_in_s": max(0, int(_breaker["open_until"] - time.monotonic()))}


# ---------------------------------------------------------------------------
# HTTP cache
# ---------------------------------------------------------------------------

def _cache_get(key: str) -> dict | None:
    try:
        conn = dbmod.get_ro()
        row = dbmod.one(conn, "SELECT status, body_json, expires_at FROM http_cache "
                              "WHERE cache_key = ?", (key,))
    except sqlite3.DatabaseError:
        return None
    if row is None or int(row["expires_at"] or 0) < dbmod.now_ms():
        return None
    try:
        return {"status": int(row["status"]),
                "body": json.loads(row["body_json"]) if row["body_json"] else None}
    except (ValueError, TypeError):
        return None


def _cache_put(key: str, status: int, body: Any, ttl_ms: int,
               error: str | None = None) -> None:
    now = dbmod.now_ms()
    try:
        blob = json.dumps(body, ensure_ascii=False, default=str) if body is not None else None
    except (TypeError, ValueError):
        blob = None

    def _op(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO http_cache(cache_key,provider,status,body_json,fetched_at,"
            "expires_at,error) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET status=excluded.status, "
            "body_json=excluded.body_json, fetched_at=excluded.fetched_at, "
            "expires_at=excluded.expires_at, error=excluded.error",
            (key, "civitai", int(status), blob, now, now + ttl_ms, error),
        )
        conn.commit()

    try:
        dbmod.writer().submit(_op)
    except BaseException as exc:  # noqa: BLE001 - caching is best effort
        log.debug("ignored (caching is best effort): %s", exc)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

# Sentinel: the lookup could not be completed (network error, 5xx, rate limit,
# open breaker).  Distinct from None, which means "Civitai answered: no match".
UNREACHABLE = object()


async def fetch_by_hash(model_hash: str, *, force: bool = False) -> dict | object | None:
    """Look a model up by AutoV2 or SHA-256.

    Returns a mapped dict on a match, ``None`` when Civitai definitively
    answered 404, and :data:`UNREACHABLE` when the answer is unknown.
    """
    if not model_hash or len(model_hash) < 8:
        return None
    cfg = config_service.get_config()
    # Kill-switch: short-circuit before any socket is created.
    if not (cfg.online_enabled and cfg.civitai_enabled):
        raise FeatureUnavailable(
            "Online lookups are disabled.",
            details={"online_enabled": cfg.online_enabled,
                     "civitai_enabled": cfg.civitai_enabled},
        )
    key = f"civitai:hash:{model_hash.upper()}"
    if not force:
        cached = _cache_get(key)
        if cached is not None:
            if cached["status"] == 200:
                return map_version(cached["body"])
            return None if cached["status"] == 404 else UNREACHABLE
    if _breaker_open():
        raise FeatureUnavailable("Civitai is temporarily unreachable; try again later.",
                                 details=breaker_state())
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise FeatureUnavailable("httpx is not installed.") from exc

    headers = {"User-Agent": "GeekatplayAssetVault/2.0"}
    if cfg.civitai_api_key:
        headers["Authorization"] = f"Bearer {cfg.civitai_api_key}"
    url = f"{API_BASE}/model-versions/by-hash/{model_hash}"

    last_error: str | None = None
    for attempt in range(RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True) as client:
                res = await client.get(url, headers=headers)
            if res.status_code == 200:
                body = res.json()
                _cache_put(key, 200, body, CACHE_TTL_MS)
                _record_success()
                return map_version(body)
            if res.status_code == 404:
                _cache_put(key, 404, None, NOT_FOUND_TTL_MS)
                _record_success()
                return None
            if res.status_code == 429:
                _cache_put(key, 429, None, 60_000, "rate limited")
                _record_failure()
                return UNREACHABLE
            last_error = f"HTTP {res.status_code}"
        except Exception as exc:  # noqa: BLE001 - network errors are expected
            last_error = str(exc)[:200]
        if attempt < RETRIES:
            await asyncio.sleep((2 ** attempt) * 0.5 + random.random() * 0.3)
    _record_failure()
    _cache_put(key, 0, None, 60_000, last_error)
    log.info("Civitai lookup failed for %s: %s", model_hash, last_error)
    return UNREACHABLE


async def fetch_latest_version(civitai_model_id: int, *,
                               force: bool = False) -> dict | None:
    """The newest published version of a model, for update detection.

    The by-hash endpoint answers "which version IS this file"; this one asks
    the parent model which version is newest.  Same kill-switch, cache and
    breaker discipline as ``fetch_by_hash``.  Returns ``None`` on any miss -
    update detection is a bonus on top of a successful match, never a blocker.
    """
    cfg = config_service.get_config()
    if not (cfg.online_enabled and cfg.civitai_enabled):
        return None
    key = f"civitai:model:{int(civitai_model_id)}"
    body = None
    if not force:
        cached = _cache_get(key)
        if cached is not None:
            if cached["status"] != 200:
                return None
            body = cached["body"]
    if body is None:
        if _breaker_open():
            return None
        try:
            import httpx
        except ImportError:  # pragma: no cover
            return None
        headers = {"User-Agent": "GeekatplayAssetVault/2.0"}
        if cfg.civitai_api_key:
            headers["Authorization"] = f"Bearer {cfg.civitai_api_key}"
        url = f"{API_BASE}/models/{int(civitai_model_id)}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True) as client:
                res = await client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 - network errors are expected
            log.debug("Civitai model fetch failed for %s: %s", civitai_model_id, exc)
            _record_failure()
            return None
        if res.status_code != 200:
            _cache_put(key, res.status_code, None,
                       NOT_FOUND_TTL_MS if res.status_code == 404 else 60_000)
            if res.status_code not in (404,):
                _record_failure()
            return None
        try:
            body = res.json()
        except ValueError:
            return None
        _cache_put(key, 200, body, CACHE_TTL_MS)
        _record_success()

    versions = body.get("modelVersions") if isinstance(body, dict) else None
    if not isinstance(versions, list):
        return None
    published = [v for v in versions if isinstance(v, dict)
                 and str(v.get("status") or "Published") == "Published"]
    if not published:
        return None
    # Civitai lists newest first; version ids are monotonic, so sort defensively.
    latest = max(published, key=lambda v: _int(v.get("id")) or 0)
    return {
        "latest_version_id": _int(latest.get("id")),
        "latest_version_name": latest.get("name"),
        "latest_version_notes": _text(latest.get("description")),
    }


def map_version(data: Any) -> dict | None:
    """Map a Civitai model-version payload onto our column set."""
    if not isinstance(data, dict):
        return None
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    files = data.get("files") if isinstance(data.get("files"), list) else []
    images = data.get("images") if isinstance(data.get("images"), list) else []
    primary = next((f for f in files if isinstance(f, dict) and f.get("primary")),
                   files[0] if files and isinstance(files[0], dict) else {})
    return {
        "civitai_model_id": _int(model.get("id")),
        "civitai_version_id": _int(data.get("id")),
        "civitai_url": (f"https://civitai.com/models/{model.get('id')}"
                        f"?modelVersionId={data.get('id')}" if model.get("id") else None),
        "description": _text(data.get("description") or model.get("description")),
        "trigger_words_json": [str(w) for w in (data.get("trainedWords") or [])][:40],
        "download_url": data.get("downloadUrl") or primary.get("downloadUrl"),
        "nsfw": 1 if model.get("nsfw") else 0,
        "rating": _float(stats.get("rating")),
        "download_count": _int(stats.get("downloadCount")),
        "latest_version_name": data.get("name"),
        "latest_version_id": _int(data.get("id")),
        "latest_version_notes": _text(data.get("description")),
        "base_model": data.get("baseModel"),
        "preview_image": next((i.get("url") for i in images
                               if isinstance(i, dict) and i.get("url")), None),
        "recommended_settings_json": _recommended(data),
    }


def _recommended(data: dict) -> dict | None:
    out: dict = {}
    for key in ("clipSkip", "steps", "cfgScale", "sampler", "scheduler"):
        if data.get(key) is not None:
            out[key] = data[key]
    air = data.get("air")
    if air:
        out["air"] = air
    return out or None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _text(v) -> str | None:
    if not isinstance(v, str):
        return None
    import re

    clean = re.sub(r"<[^>]+>", " ", v)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:8000] or None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def apply_enrichment(model_id: int, mapped: dict | None, *,
                     state: str = "matched") -> None:
    now = dbmod.now_ms()
    if mapped is None:
        def _miss(conn: sqlite3.Connection, _touch) -> None:
            conn.execute("UPDATE models SET civitai_state=?, civitai_checked_at=? "
                         "WHERE id=?", ("not_found", now, int(model_id)))

        sync.write_synced(_miss, [f"model:{int(model_id)}"])
        return

    b = dbmod.bind
    variant = mapped.get("base_model")

    def _op(conn: sqlite3.Connection, _touch) -> None:
        conn.execute(
            "UPDATE models SET civitai_model_id=?, civitai_version_id=?, civitai_url=?, "
            "civitai_state=?, civitai_checked_at=?, description=COALESCE(?, description), "
            "description_source=CASE WHEN ? IS NOT NULL THEN 'civitai' "
            "ELSE description_source END, trigger_words_json=?, "
            "recommended_settings_json=?, download_url=?, nsfw=?, rating=?, "
            "download_count=?, latest_version_name=?, latest_version_id=?, "
            "latest_version_notes=?, has_update=?, "
            "base_model_variant=COALESCE(?, base_model_variant), "
            "updated_at=? WHERE id=?",
            (
                b(mapped.get("civitai_model_id"), kind="int"),
                b(mapped.get("civitai_version_id"), kind="int"),
                b(mapped.get("civitai_url")), state, now,
                b(mapped.get("description")), b(mapped.get("description")),
                b(mapped.get("trigger_words_json"), kind="json"),
                b(mapped.get("recommended_settings_json"), kind="json"),
                b(mapped.get("download_url")), b(mapped.get("nsfw"), kind="int"),
                b(mapped.get("rating"), kind="real"),
                b(mapped.get("download_count"), kind="int"),
                b(mapped.get("latest_version_name")),
                b(mapped.get("latest_version_id"), kind="int"),
                b(mapped.get("latest_version_notes")),
                1 if mapped.get("has_update") else 0,
                b(variant if variant in ("Pony", "Illustrious", "NoobAI") else None),
                now, int(model_id),
            ),
        )

    # Description and trigger words feed the search document.
    sync.write_synced(_op, [f"model:{int(model_id)}"])


async def enrich_model(model_id: int, *, force: bool = False) -> dict:
    """Enrich one model from its stored hash.  Requires hash_state='done'."""
    conn = dbmod.get_ro()
    row = dbmod.one(
        conn, "SELECT f.sha256, f.autov2, f.hash_state FROM models m "
              "JOIN model_files f ON f.id = m.primary_file_id WHERE m.id = ?",
        (int(model_id),))
    if row is None:
        return {"state": "error", "reason": "not_found"}
    if row["hash_state"] != "done" or not row["sha256"]:
        return {"state": "error", "reason": "hash_required"}
    mapped = await fetch_by_hash(str(row["sha256"]), force=force)
    if mapped is UNREACHABLE:
        # A timeout or server error is not "this model is not on Civitai":
        # leave the stored state untouched so a later pass retries.
        return {"state": "error", "reason": "unreachable"}
    if mapped and mapped.get("civitai_model_id"):
        latest = await fetch_latest_version(mapped["civitai_model_id"], force=force)
        if latest and latest.get("latest_version_id"):
            mapped.update(latest)
            mapped["has_update"] = (
                mapped.get("civitai_version_id") is not None
                and latest["latest_version_id"] != mapped["civitai_version_id"])
    apply_enrichment(int(model_id), mapped)
    return {"state": "matched" if mapped else "not_found"}


def pending_count(conn: sqlite3.Connection | None = None) -> int:
    conn = conn or dbmod.get_ro()
    return int(dbmod.scalar(
        conn, "SELECT COUNT(*) FROM models WHERE civitai_state = 'pending'") or 0)
