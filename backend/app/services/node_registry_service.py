"""Read-only catalogue of installable ComfyUI nodes.

The catalogue deliberately separates facts from installation.  It uses the
official Comfy Registry when online access is enabled and keeps a bounded local
cache, then augments it with ComfyUI-Manager's local legacy class map.  A
legacy map is useful for resolving old workflows, but is never presented as an
official verified package.
"""

from __future__ import annotations

import json
import logging
import platform
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..config import DATA_DIR
from ..core import config_service
from ..core import db as dbmod
from ..parsers import node_registry
from . import comfyui_service

log = logging.getLogger(__name__)

CACHE_PATH = DATA_DIR / "node_registry_cache.json"
CACHE_TTL_MS = 24 * 60 * 60 * 1000
API_HOST = "api.comfy.org"
API_BASE = f"https://{API_HOST}"
PAGE_LIMIT = 100
MAX_PAGES = 100
TIMEOUT_S = 12


def _now() -> int:
    return int(time.time() * 1000)


def _cache_read() -> dict:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cache_write(payload: dict) -> None:
    try:
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except OSError as exc:
        log.info("node registry cache write failed: %s", exc)


def _form_factor() -> str:
    system = platform.system().lower()
    return "git-windows" if system == "windows" else (
        "git-mac" if system == "darwin" else "git-linux" if system == "linux" else "other")


def _installed_version() -> str:
    try:
        return str(comfyui_service.probe().version or "unknown")
    except Exception:  # noqa: BLE001 - a catalogue must remain usable when probing is incomplete
        return "unknown"


def _fetch_cnr() -> list[dict]:
    """Fetch only from the fixed official Registry endpoint.

    No caller-supplied URL is accepted and response size/page count are bounded.
    The result is metadata only; this function never downloads a node archive.
    """
    records: list[dict] = []
    version = _installed_version()
    for page in range(1, MAX_PAGES + 1):
        query = urlencode({"page": page, "limit": PAGE_LIMIT,
                           "comfyui_version": version, "form_factor": _form_factor()})
        request = Request(f"{API_BASE}/nodes?{query}", headers={  # noqa: S310 - API_BASE is a fixed https:// constant, never caller input
            "Accept": "application/json", "User-Agent": "ComfyUI-Asset-Vault/2.1"
        })
        try:
            with urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - fixed HTTPS host
                raw = response.read(8 * 1024 * 1024 + 1)
        except (OSError, URLError) as exc:
            raise RuntimeError(f"Comfy Registry is unavailable: {exc}") from exc
        if len(raw) > 8 * 1024 * 1024:
            raise RuntimeError("Comfy Registry page exceeded the 8 MB safety limit.")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("Comfy Registry returned invalid JSON.") from exc
        nodes = body.get("nodes") if isinstance(body, dict) else None
        if not isinstance(nodes, list):
            raise RuntimeError("Comfy Registry response did not contain a node list.")
        records.extend(x for x in nodes if isinstance(x, dict))
        total_pages = int(body.get("totalPages") or page)
        if page >= total_pages:
            break
    return records


def _installed_by_repo() -> set[str]:
    try:
        rows = dbmod.rows(dbmod.get_ro(), "SELECT repo_url FROM node_packages WHERE missing_since IS NULL")
    except Exception:  # noqa: BLE001 - an unreadable DB just means "nothing installed"
        return set()
    return {node_registry.normalize_repo_url(str(r["repo_url"])).lower()
            for r in rows if r["repo_url"] and node_registry.normalize_repo_url(str(r["repo_url"]))}


def _cnr_item(raw: dict, installed: set[str]) -> dict | None:
    node_id = str(raw.get("id") or "").strip()
    if not node_id:
        return None
    publisher = raw.get("publisher") if isinstance(raw.get("publisher"), dict) else {}
    latest = raw.get("latest_version") if isinstance(raw.get("latest_version"), dict) else {}
    repo = node_registry.normalize_repo_url(str(raw.get("repository") or raw.get("repository_url") or ""))
    version = str(latest.get("version") or raw.get("version") or "") or None
    dependencies = latest.get("dependencies") if isinstance(latest.get("dependencies"), list) else []
    warnings = ["Registry metadata has no archive checksum/signature; installation requires a separate explicit review."]
    if latest.get("deprecated") or raw.get("deprecated"):
        warnings.insert(0, "This package is marked deprecated by the registry.")
    return {
        "id": node_id, "name": str(raw.get("name") or raw.get("title") or node_id),
        "description": str(raw.get("description") or "")[:2000] or None,
        "publisher": str(publisher.get("name") or publisher.get("id") or "") or None,
        "repository": repo, "source": "comfy_registry", "official": True,
        "installed": bool(repo and repo.lower() in installed), "version": version,
        "published_at": latest.get("created_at") or latest.get("createdAt"),
        "dependencies": [str(x)[:200] for x in dependencies[:100]],
        "classes": [], "compatibility": raw.get("comfyui_version") or raw.get("comfyuiVersion"),
        "warnings": warnings,
    }


def _legacy_items(installed: set[str]) -> list[dict]:
    cfg = config_service.get_config()
    registry = node_registry.get_registry(cfg.comfyui_path)
    out: list[dict] = []
    for repo, (classes, meta) in registry.by_url.items():
        package = str(meta.get("title_aux") or meta.get("title") or node_registry.repo_basename(repo) or repo)
        out.append({
            "id": f"legacy:{repo}", "name": package, "description": None,
            "publisher": None, "repository": repo, "source": "manager_legacy_map",
            "official": False, "installed": repo.lower() in installed,
            "version": None, "published_at": None, "dependencies": [],
            "classes": sorted(str(x) for x in classes)[:2000], "compatibility": None,
            "warnings": ["Legacy ComfyUI-Manager class mapping: package identity and version are not independently verified.",
                         "Git sources are resolved to an exact commit before any install plan is offered."],
        })
    return out


def _catalogue(*, refresh: bool = False) -> tuple[list[dict], dict]:
    cache = _cache_read()
    cached_at = int(cache.get("fetched_at") or 0)
    fresh = cached_at and _now() - cached_at < CACHE_TTL_MS
    cnr = cache.get("nodes") if isinstance(cache.get("nodes"), list) else []
    error = None
    cfg = config_service.get_config()
    if (refresh or not fresh) and cfg.online_enabled:
        try:
            cnr = _fetch_cnr()
            cached_at = _now()
            _cache_write({"fetched_at": cached_at, "nodes": cnr,
                          "comfyui_version": _installed_version(), "source": API_BASE})
        except RuntimeError as exc:
            error = str(exc)
    elif not cfg.online_enabled and not cnr:
        error = "Online lookup is disabled and there is no cached Comfy Registry catalogue."
    installed = _installed_by_repo()
    items = [item for item in (_cnr_item(x, installed) for x in cnr) if item]
    # Do not silently merge legacy entries into a CNR entry: a matching name is
    # inference. Keeping both sources makes the provenance visible.
    items.extend(_legacy_items(installed))
    meta = {"online_enabled": bool(cfg.online_enabled), "fetched_at": cached_at or None,
            "fresh": bool(cached_at and _now() - cached_at < CACHE_TTL_MS),
            "cache_ttl_ms": CACHE_TTL_MS, "error": error, "source": API_BASE,
            "legacy_source": node_registry.get_registry(cfg.comfyui_path).source}
    return items, meta


def list_registry(*, q: str | None = None, installed: bool | None = None,
                  source: str | None = None, refresh: bool = False,
                  limit: int = 100, offset: int = 0) -> dict:
    items, meta = _catalogue(refresh=refresh)
    needle = str(q or "").strip().lower()
    if needle:
        items = [x for x in items if needle in " ".join([
            str(x.get("name") or ""), str(x.get("id") or ""), str(x.get("publisher") or ""),
            str(x.get("repository") or ""), " ".join(x.get("classes") or [])]).lower()]
    if installed is not None:
        items = [x for x in items if bool(x.get("installed")) is bool(installed)]
    if source in {"comfy_registry", "manager_legacy_map"}:
        items = [x for x in items if x.get("source") == source]
    items.sort(key=lambda x: (not bool(x.get("installed")), str(x.get("name") or "").lower()))
    total = len(items)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    page_items = items[offset:offset + limit]
    return {"items": page_items,
            "page": {"limit": limit, "offset": offset, "total": total,
                     "returned": len(page_items), "has_more": offset + len(page_items) < total},
            "meta": meta}


def status(*, refresh: bool = False) -> dict:
    _items, meta = _catalogue(refresh=refresh)
    return meta
