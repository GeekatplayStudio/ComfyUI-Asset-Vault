"""The reference dataset: a seed shipped with the app, overlaid by refreshes.

Three JSON files, all plain data:

``app/advisor/data/model_catalog.json``  the **seed** - a snapshot produced by
    the updater at release time, so the advisor answers on first launch and
    with outbound lookups switched off;
``app/advisor/data/sources.json``        the **source list** - which services
    are asked, which repositories are pinned, which discovery queries run and
    which publishers are recognised.  This is the app's memory of *where model
    information comes from*;
``data/advisor/catalog.json``            the **refreshed** catalog the updater
    writes.  When present and newer it replaces the seed entirely.

``data/advisor/state.json`` holds what the updater learned: when each source
last answered, the pins the owner added, repositories discovered by following
``base_model`` links, and when each entry was first seen.

Nothing in these files is code and nothing is executed: they are loaded with
``json.load`` and every field is read defensively.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

from .. import config as buildcfg
from ..core import db as dbmod
from . import fit

log = logging.getLogger("vault.advisor.catalog")

SEED_DIR = Path(__file__).resolve().parent / "data"
SEED_CATALOG = SEED_DIR / "model_catalog.json"
SEED_SOURCES = SEED_DIR / "sources.json"

USER_DIR = buildcfg.DATA_DIR / "advisor"
USER_CATALOG = USER_DIR / "catalog.json"
USER_STATE = USER_DIR / "state.json"

SCHEMA = 1
MAX_USER_PINS = 200
MAX_LEARNED = 400

_lock = threading.RLock()
_catalog_cache: tuple[tuple, dict] | None = None
_sources_cache: tuple[tuple, dict] | None = None


def _stamp(path: Path) -> tuple:
    try:
        stat = path.stat()
    except OSError:
        return (str(path), 0, 0)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _load_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        if path.exists():
            log.warning("could not read %s: %s", path.name, exc)
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    """Atomic replace, so a crash mid-write never leaves half a catalog."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


#: Name and tag fragments that keep an entry out of the catalog.  The advisor
#: is a planning tool shipped to everyone, so adult-content models and
#: safety-stripped repacks are left out by default; they remain one pin away
#: for an owner who wants a specific repository assessed.
_UNSUITABLE = re.compile(
    r"nsfw|uncensor|abliterat|obliterat|heretic|porn|hentai|\bnude|naked|"
    r"explicit|xxx|erotic|lewd|18\+|\bsex|\bass\b|boob|nipple|fetish|undress",
    re.IGNORECASE)


def suitable(entry: dict) -> bool:
    """False for an entry the default catalog leaves out."""
    if entry.get("nsfw"):
        return False
    text = " ".join([str(entry.get("name") or ""), str(entry.get("id") or ""),
                     " ".join(str(t) for t in entry.get("tags") or [])])
    return not _UNSUITABLE.search(text)


def suitable_lead(lead: dict) -> bool:
    text = f"{lead.get('name') or ''} {lead.get('description') or ''}"
    return not _UNSUITABLE.search(text)


def _https(value: object) -> str | None:
    """A link the interface may render: ``https`` only, no credentials."""
    text = str(value or "").strip()
    if not text.lower().startswith("https://") or len(text) > 2048:
        return None
    authority = text[8:].split("/", 1)[0]
    if not authority or "@" in authority or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in text):
        return None
    return text


def _scrub_links(entry: dict) -> dict:
    """Blank any link that is not plain https.

    The catalog is assembled from third-party JSON and is rendered as anchors,
    so a ``javascript:`` or ``http:`` URL must never survive loading - whether
    it arrived from a source or from a hand-edited catalog file.
    """
    if "url" in entry:
        entry["url"] = _https(entry.get("url"))
    for group in ("variants", "companions"):
        for item in entry.get(group) or []:
            if isinstance(item, dict) and "download_url" in item:
                item["download_url"] = _https(item.get("download_url"))
    return entry


def _unique_variant_ids(entry: dict) -> dict:
    """Make variant ids unique within an entry, numbering any repeat.

    The interface and the fit engine both address a variant by id, so two
    variants sharing one would be indistinguishable.  Sources are expected to
    produce unique ids; this is the guarantee for data that did not.
    """
    seen: set[str] = set()
    for variant in entry.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        base = str(variant.get("id") or variant.get("file") or "variant")
        candidate, suffix = base, 2
        while candidate in seen:
            candidate = f"{base}#{suffix}"
            suffix += 1
        variant["id"] = candidate
        seen.add(candidate)
    return entry


def _valid_entry(entry: object) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("id"), str)
            and isinstance(entry.get("name"), str)
            and entry.get("kind") in fit.KINDS
            and isinstance(entry.get("variants"), list) and bool(entry["variants"]))


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def load() -> dict:
    """The catalog in use: the refreshed one if present, else the seed."""
    global _catalog_cache
    with _lock:
        key = (_stamp(USER_CATALOG), _stamp(SEED_CATALOG))
        if _catalog_cache and _catalog_cache[0] == key:
            return _catalog_cache[1]
        seed = _load_json(SEED_CATALOG) or {}
        user = _load_json(USER_CATALOG)
        chosen, origin = seed, "seed"
        if user and int(user.get("generated_at") or 0) >= int(seed.get("generated_at") or 0) \
                and user.get("entries"):
            chosen, origin = user, "refreshed"
        entries = [_unique_variant_ids(_scrub_links(e))
                   for e in chosen.get("entries") or []
                   if _valid_entry(e) and suitable(e)]
        result = {
            "schema": int(chosen.get("schema") or SCHEMA),
            "origin": origin,
            "generated_at": int(chosen.get("generated_at") or 0),
            "seed_generated_at": int(seed.get("generated_at") or 0),
            "sources": chosen.get("sources") or {},
            "leads": [lead for lead in chosen.get("leads") or []
                      if isinstance(lead, dict) and suitable_lead(lead)
                      and _https(lead.get("url"))],
            "entries": entries,
            "by_id": {e["id"]: e for e in entries},
        }
        _catalog_cache = (key, result)
        return result


def get(entry_id: str) -> dict | None:
    return load()["by_id"].get(entry_id)


def _compact(value):
    """Drop empty fields before writing: every reader uses ``.get``."""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items()
                if v is not None and v != [] and v != "" and v is not False}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def save(entries: list[dict], *, sources: dict, leads: list[dict]) -> dict:
    """Write a refreshed catalog and return its header."""
    clean = [_unique_variant_ids(_compact(e)) for e in entries
             if _valid_entry(e) and suitable(e)]
    leads = [lead for lead in leads if suitable_lead(lead)]
    doc = {"schema": SCHEMA, "generated_at": dbmod.now_ms(),
           "generator": f"{buildcfg.APP_NAME} {buildcfg.VERSION}",
           "sources": sources, "leads": leads, "entries": clean}
    with _lock:
        _write_json(USER_CATALOG, doc)
        invalidate()
    return {"generated_at": doc["generated_at"], "entries": len(clean)}


def invalidate() -> None:
    global _catalog_cache, _sources_cache
    with _lock:
        _catalog_cache = None
        _sources_cache = None


def counts(entries: list[dict]) -> dict:
    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for entry in entries:
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
        source = str(entry.get("source") or "unknown")
        by_source[source] = by_source.get(source, 0) + 1
    return {"total": len(entries), "by_kind": by_kind, "by_source": by_source}


# ---------------------------------------------------------------------------
# State: what the updater learned
# ---------------------------------------------------------------------------

def load_state() -> dict:
    state = _load_json(USER_STATE) or {}
    state.setdefault("sources", {})
    state.setdefault("user_pins", [])
    state.setdefault("learned", [])
    state.setdefault("first_seen", {})
    state.setdefault("remote_sources", None)
    state.setdefault("last_run", None)
    return state


def save_state(state: dict) -> None:
    state["user_pins"] = list(state.get("user_pins") or [])[:MAX_USER_PINS]
    state["learned"] = list(state.get("learned") or [])[-MAX_LEARNED:]
    with _lock:
        _write_json(USER_STATE, state)
        invalidate()


# ---------------------------------------------------------------------------
# Source list
# ---------------------------------------------------------------------------

def valid_sources_doc(doc: object) -> bool:
    """Shape check for a source list - the seed, or one fetched from the repo."""
    if not isinstance(doc, dict) or not isinstance(doc.get("revision"), int):
        return False
    if not isinstance(doc.get("sources"), list):
        return False
    for key in ("pins", "discovery", "publishers"):
        if key in doc and not isinstance(doc[key], dict):
            return False
    return True


def sources() -> dict:
    """The effective source list: seed, or a newer one fetched from the repo,
    plus the pins the owner added and the repositories discovery learned."""
    global _sources_cache
    with _lock:
        key = (_stamp(SEED_SOURCES), _stamp(USER_STATE))
        if _sources_cache and _sources_cache[0] == key:
            return _sources_cache[1]
        seed = _load_json(SEED_SOURCES) or {"revision": 0, "sources": []}
        state = load_state()
        remote = state.get("remote_sources")
        chosen, origin = seed, "seed"
        if valid_sources_doc(remote) and remote["revision"] > int(seed.get("revision") or 0):
            chosen, origin = remote, "repository"
        result = {
            "revision": int(chosen.get("revision") or 0),
            "origin": origin,
            "sources": chosen.get("sources") or [],
            "pins": chosen.get("pins") or {},
            "discovery": chosen.get("discovery") or {},
            "publishers": chosen.get("publishers") or {},
            "user_pins": state.get("user_pins") or [],
            "learned": state.get("learned") or [],
        }
        _sources_cache = (key, result)
        return result
