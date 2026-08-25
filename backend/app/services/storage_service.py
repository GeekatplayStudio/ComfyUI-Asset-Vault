"""Disk-side half of Storage & Maintenance (REQUIREMENTS_R2 C10).

``queries/storage_query`` answers "what does the index know".  This module answers
"what does the filesystem say" - free space per *volume*, the on-disk footprint of
each part of the install, and the cleanup call itself.

Two things it deliberately does not do:

* It does not guess at free space from one root.  Roots can live on different
  drives (C10.1), so every root is probed against its own volume and roots that
  share a volume are reported once, with the roots that sit on them listed.
* It does not delete.  ``cleanup()`` validates the request, prices it, and then
  hands the uid list to ``services/file_ops`` - the same code path, the same
  trash, the same root guard the UI's own delete button uses.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DATA_DIR, QUARANTINE_DIRNAME, THUMB_DIR, TRASH_DIRNAME
from ..core import config_service
from ..core import db as dbmod
from ..core.errors import ValidationError
from ..core.pathsafe import long_path, normalize, path_key
from .queries import storage_query

log = logging.getLogger(__name__)

#: A footprint walk is cheap on this shape of install (~20 k files, well under a
#: second warm) but it is still I/O, so it is cached.  The cache is keyed on
#: ``PRAGMA data_version`` *as well as* a short TTL: any committed mutation from
#: any path invalidates it immediately, and the TTL only bounds staleness from
#: changes made outside the app (ComfyUI writing an output, say).  Free space is
#: never served from here - see ``volumes()``.
FOOTPRINT_TTL_S = 120.0
WALK_FILE_CAP = 400_000

#: Which subdirectories of a ComfyUI root make up which bucket.  ``models`` and
#: ``output`` are measured here too even though the index knows their sizes: the
#: difference between the two is real information (sidecars, previews, partial
#: downloads, files the indexer skipped) and the UI shows both.
BUCKETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("models", "Models", ("models",)),
    ("outputs", "Outputs", ("output",)),
    ("inputs", "Inputs", ("input",)),
    ("custom_nodes", "Custom nodes", ("custom_nodes",)),
    ("cache", "Cache & temp", ("temp", "user")),
)

#: Everything else inside the ComfyUI root - the application itself.
PROGRAM_LABEL = "ComfyUI program files"

_lock = threading.RLock()
#: key -> (monotonic_at, data_version, payload)
_cache: dict[str, tuple[float, int, dict]] = {}


@dataclass
class DirStat:
    bytes: int = 0
    files: int = 0
    dirs: int = 0
    truncated: bool = False
    error: str | None = None
    children: dict = field(default_factory=dict)


def measure_dir(root: str | Path, *, cap: int = WALK_FILE_CAP,
                depth_children: int = 0) -> DirStat:
    """Recursive size of ``root``.  Never raises, never follows directory links.

    Following a junction would double-count a shared model folder that also
    appears as its own root - and on this install ``extra_model_paths`` is exactly
    the mechanism that creates those.
    """
    stat = DirStat()
    base = str(normalize(root))
    if not os.path.isdir(long_path(base)):
        stat.error = "not_found"
        return stat
    stack: list[tuple[str, int]] = [(base, 0)]
    while stack:
        current, level = stack.pop()
        try:
            entries = list(os.scandir(long_path(current)))
        except OSError as exc:
            stat.error = stat.error or type(exc).__name__
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stat.dirs += 1
                    stack.append((entry.path, level + 1))
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            size = int(info.st_size)
            stat.files += 1
            stat.bytes += size
            if depth_children and level < depth_children:
                key = os.path.basename(current) if level else ""
                if key:
                    child = stat.children.setdefault(key, {"bytes": 0, "files": 0})
                    child["bytes"] += size
                    child["files"] += 1
            if stat.files >= cap:
                stat.truncated = True
                return stat
    return stat


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------

def _volume_key(path: str | Path) -> str:
    """One key per physical volume.

    Windows: the drive letter.  POSIX: ``st_dev`` of the nearest existing
    ancestor - ``splitdrive`` is always empty there, and keying on the path
    would report every root as its own disk with duplicated free space.
    """
    p = str(normalize(path))
    drive = os.path.splitdrive(p)[0]
    if drive:
        return os.path.normcase(drive)
    probe = p
    while probe:
        try:
            return f"dev:{os.stat(probe).st_dev}"
        except OSError:
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
    return os.path.normcase(p)


def _mount_label(path: str | Path) -> str:
    """What a person calls the volume: ``C:`` on Windows, the mount point on POSIX."""
    p = str(normalize(path))
    drive = os.path.splitdrive(p)[0]
    if drive:
        return drive
    probe = p if os.path.isdir(p) else os.path.dirname(p)
    while probe and not os.path.ismount(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return probe or "/"


def volume_usage(path: str | Path) -> dict:
    """``shutil.disk_usage`` for the volume holding ``path``, degrading to nulls."""
    target = str(normalize(path))
    probe = target
    while probe and not os.path.isdir(long_path(probe)):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(probe)
    except (OSError, ValueError) as exc:
        return {"total_bytes": None, "free_bytes": None, "used_bytes": None,
                "used_pct": None, "available": False, "error": str(exc)[:200]}
    used_pct = round(usage.used / usage.total * 100, 1) if usage.total else None
    return {"total_bytes": int(usage.total), "free_bytes": int(usage.free),
            "used_bytes": int(usage.used), "used_pct": used_pct, "available": True,
            "error": None}


def volumes(cfg=None) -> list[dict]:
    """One entry per distinct volume, with the configured roots that live on it.

    Never cached.  Headroom is the whole point of this view, so every read
    re-probes the volume - showing a stale free-space figure right after the
    owner deleted 40 GB would defeat the feature.
    """
    cfg = cfg or config_service.get_config()
    known = _db_roots()
    out: dict[str, dict] = {}
    for root in _all_roots(cfg, known):
        key = _volume_key(root["path"])
        entry = out.get(key)
        if entry is None:
            usage = volume_usage(root["path"])
            entry = out[key] = {
                "key": key,
                "mount": _mount_label(root["path"]),
                "roots": [], **usage,
            }
        entry["roots"].append(root)
    return sorted(out.values(), key=lambda v: str(v["mount"]).lower())


def _db_roots() -> dict[str, dict]:
    """Root rows the index has actually written against, keyed by ``path_key``."""
    try:
        conn = dbmod.get_ro()
        return {str(r["path_key"]): dict(r) for r in dbmod.rows(
            conn, "SELECT id, kind, path, path_key, label, category, is_default, "
                  "source, available FROM roots")}
    except Exception as exc:  # noqa: BLE001 - a storage view must never 500 on this
        log.debug("roots read failed: %s", exc)
        return {}


def _all_roots(cfg, known: dict[str, dict]) -> list[dict]:
    """Configured roots first, then retired roots the index is still holding.

    C7.3: pointing the app at a different ComfyUI install **retains** the old
    rows.  This is where the UI learns that they exist, what they weigh, and
    which root they belong to, so "prune them" can be an explicit choice rather
    than a silent side effect of changing a path.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for root in cfg.roots:
        key = path_key(root.path)
        seen.add(key)
        row = known.get(key) or {}
        out.append({
            "id": int(row.get("id") or root.id), "kind": root.kind,
            "path": str(root.path), "label": root.label, "category": root.category,
            "is_default": bool(root.is_default), "source": root.source,
            "configured": True, "retired": False,
            "exists": os.path.isdir(long_path(root.path)),
            "indexed": key in known,
        })
    for key, row in known.items():
        if key in seen:
            continue
        out.append({
            "id": int(row["id"]), "kind": str(row["kind"]), "path": str(row["path"]),
            "label": str(row["label"]), "category": row["category"],
            "is_default": False, "source": str(row["source"]),
            "configured": False, "retired": True,
            "exists": os.path.isdir(long_path(str(row["path"]))),
            "indexed": True,
        })
    return out


def roots_report(cfg=None) -> dict:
    """Per-root volume, row counts and retention state (C10.1 + C7.3)."""
    cfg = cfg or config_service.get_config()
    known = _db_roots()
    indexed = storage_query.indexed_bytes()
    by_root = indexed.get("by_root") or {}
    items = []
    for root in _all_roots(cfg, known):
        counts = by_root.get(int(root["id"])) or {}
        items.append({
            **root,
            "volume": volume_usage(root["path"]),
            "contents": {
                "models": counts.get("models") or {"count": 0, "bytes": 0},
                "outputs": counts.get("outputs") or {"count": 0, "bytes": 0},
                "workflows": counts.get("workflows") or {"count": 0, "bytes": 0},
            },
            "indexed_bytes": sum(int((counts.get(k) or {}).get("bytes") or 0)
                                 for k in ("models", "outputs")),
        })
    retired = [r for r in items if r["retired"]]
    return {
        "items": items,
        "retention_policy": "retain",
        "retention_note": (
            "Rows indexed under a root you have pointed away from are kept, not "
            "deleted. They hold ratings, tags, notes and album membership that a "
            "re-scan cannot rebuild. Remove them explicitly when you are sure."
        ),
        "retired_roots": len(retired),
        "retired_bytes": sum(int(r["indexed_bytes"] or 0) for r in retired),
    }


# ---------------------------------------------------------------------------
# Footprint
# ---------------------------------------------------------------------------

def footprint(cfg=None, *, refresh: bool = False) -> dict:
    """On-disk size of the install, broken out per C10.1."""
    cfg = cfg or config_service.get_config()
    root = cfg.comfyui_path
    key = os.path.normcase(str(root or "-"))
    now = time.monotonic()
    version = dbmod.data_version()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and not refresh and hit[1] == version                 and now - hit[0] < FOOTPRINT_TTL_S:
            return hit[2]

    started = time.perf_counter()
    indexed = storage_query.indexed_bytes()
    buckets: list[dict] = []
    total_measured = 0
    truncated = False

    if root is not None and os.path.isdir(long_path(str(root))):
        base = Path(root)
        claimed: set[str] = set()
        for bucket_key, label, dirnames in BUCKETS:
            size = files = 0
            present: list[str] = []
            bucket_truncated = False
            for name in dirnames:
                directory = base / name
                claimed.add(name.lower())
                if not directory.is_dir():
                    continue
                stat = measure_dir(directory)
                size += stat.bytes
                files += stat.files
                bucket_truncated = bucket_truncated or stat.truncated
                present.append(name)
            entry = {"key": bucket_key, "label": label, "bytes": size,
                     "files": files, "dirs": present, "measured": True,
                     "truncated": bucket_truncated}
            if bucket_key == "models":
                entry["indexed_bytes"] = indexed["models"]["bytes"]
                entry["indexed_count"] = indexed["models"]["count"]
            elif bucket_key == "outputs":
                entry["indexed_bytes"] = indexed["outputs"]["bytes"]
                entry["indexed_count"] = indexed["outputs"]["count"]
            elif bucket_key == "custom_nodes":
                entry["indexed_count"] = indexed["node_packages"]["count"]
            truncated = truncated or bucket_truncated
            total_measured += size
            buckets.append(entry)

        program_bytes = program_files = 0
        try:
            for entry_ in os.scandir(long_path(str(base))):
                name = entry_.name
                if name.lower() in claimed or name in (TRASH_DIRNAME, QUARANTINE_DIRNAME):
                    continue
                try:
                    if entry_.is_dir(follow_symlinks=False):
                        stat = measure_dir(entry_.path)
                        program_bytes += stat.bytes
                        program_files += stat.files
                    else:
                        program_bytes += int(entry_.stat(follow_symlinks=False).st_size)
                        program_files += 1
                except OSError:
                    continue
        except OSError as exc:
            log.debug("program-files walk failed: %s", exc)
        buckets.append({"key": "program", "label": PROGRAM_LABEL,
                        "bytes": program_bytes, "files": program_files,
                        "dirs": [], "measured": True, "truncated": False})
        total_measured += program_bytes

    # App-side storage lives outside the ComfyUI root and is reported separately
    # so the ComfyUI total stays a ComfyUI total.
    thumbs = measure_dir(THUMB_DIR)
    db = dbmod.db_stat()
    vault = {
        "key": "vault", "label": "Vault database & thumbnails",
        "bytes": int(thumbs.bytes + db.get("size_bytes", 0) + db.get("wal_bytes", 0)),
        "files": thumbs.files,
        "detail": {
            "thumbnails_bytes": thumbs.bytes, "thumbnails_files": thumbs.files,
            "database_bytes": int(db.get("size_bytes") or 0),
            "wal_bytes": int(db.get("wal_bytes") or 0),
            "path": str(DATA_DIR),
        },
        "measured": True, "truncated": False, "outside_comfyui": True,
    }

    trash = trash_footprint(cfg)
    result = {
        "comfyui_path": str(root) if root else None,
        "total_bytes": total_measured,
        "buckets": buckets,
        "vault": vault,
        "trash": trash,
        "measured_at": dbmod.now_ms(),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "truncated": truncated,
    }
    with _lock:
        _cache[key] = (now, version, result)
    return result


def trash_footprint(cfg=None) -> dict:
    """What the vault's own trash is holding, on disk and in the index (C10.4)."""
    cfg = cfg or config_service.get_config()
    conn = dbmod.get_ro()
    row = dbmod.one(conn, "SELECT COUNT(*) n, COALESCE(SUM(size), 0) b, "
                          "MIN(purge_after) p FROM trash_items")
    on_disk = 0
    dirs: list[str] = []
    for root in cfg.roots:
        directory = Path(root.path) / TRASH_DIRNAME
        if directory.is_dir():
            stat = measure_dir(directory)
            on_disk += stat.bytes
            dirs.append(str(directory))
    return {
        "count": int(row["n"] or 0) if row else 0,
        "bytes": int(row["b"] or 0) if row else 0,
        "bytes_on_disk": on_disk,
        "next_purge_at": (int(row["p"]) if row and row["p"] is not None else None),
        "retention_days": cfg.trash_retention_days,
        "directories": dirs,
        "endpoint": "/api/v1/fileops/trash",
    }


def invalidate() -> None:
    with _lock:
        _cache.clear()
    storage_query.invalidate_signals()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summary(*, stale_days: int = 180, refresh: bool = False) -> dict:
    """The compact payload the Storage tab opens with (C10.1 + C11).

    Deliberately small: totals, one breakdown, one volume list, one set of
    reclaim headlines.  Every number here has a paged endpoint behind it.
    """
    cfg = config_service.get_config()
    prints = footprint(cfg, refresh=refresh)
    indexed = storage_query.indexed_bytes()
    reclaim = storage_query.reclaimable_summary(stale_days)
    vols = volumes(cfg)

    headline = None
    for vol in vols:
        if any(r["is_default"] for r in vol["roots"]) and vol.get("total_bytes"):
            headline = vol
            break
    if headline is None and vols:
        headline = vols[0]

    return {
        "generated_at": dbmod.now_ms(),
        "configured": cfg.is_configured,
        "comfyui_path": str(cfg.comfyui_path) if cfg.comfyui_path else None,
        "footprint": {
            "total_bytes": prints["total_bytes"],
            "buckets": prints["buckets"],
            "vault": prints["vault"],
            "measured_at": prints["measured_at"],
            "elapsed_ms": prints["elapsed_ms"],
            "truncated": prints["truncated"],
        },
        "volumes": vols,
        "primary_volume": headline,
        "trash": prints["trash"],
        "reclaim": reclaim,
        "index": {
            "models": indexed["models"], "outputs": indexed["outputs"],
            "node_packages": indexed["node_packages"],
            "workflows": indexed["workflows"],
            "hashed_files": indexed["hashed"],
            "duplicate_detection": (
                "exact" if indexed["hashed"] >= indexed["models"]["count"] > 0
                else "partial" if indexed["hashed"] > 1 else "heuristic"
            ),
        },
        "detail_endpoints": {
            "candidates": "/api/v1/storage/candidates",
            "duplicates": "/api/v1/storage/duplicates",
            "roots": "/api/v1/storage/roots",
            "trash": "/api/v1/fileops/trash",
        },
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

CLEANUP_KINDS = ("model", "output")
MAX_CLEANUP = 200


def estimate(uids: list[str]) -> dict:
    """Price a selection before anything is touched (C10.2 / C10.4)."""
    return storage_query.selection_total(list(uids or []))


def cleanup(uids: list[str], *, mode: str = "trash", confirm: bool = False) -> dict:
    """Delete an explicit selection through ``file_ops``.

    Every rail C10.4 asks for is enforced here rather than in the router, so the
    MCP surface gets the identical behaviour: an empty selection is refused, the
    batch is capped, ``permanent`` demands ``confirm``, and the default is the
    recoverable trash.
    """
    from . import file_ops

    selection = [str(u) for u in (uids or []) if str(u).strip()]
    if not selection:
        raise ValidationError(
            "Nothing was selected. Cleanup never infers what to delete.",
            details={"hint": "Pass the uids you want removed."},
        )
    if len(selection) > MAX_CLEANUP:
        raise ValidationError(
            f"A single cleanup may affect at most {MAX_CLEANUP} items.",
            details={"requested": len(selection), "max": MAX_CLEANUP},
        )
    bad = [u for u in selection if str(u).split(":", 1)[0] not in CLEANUP_KINDS]
    if bad:
        raise ValidationError(
            "Cleanup only handles models and outputs.",
            details={"rejected": bad[:10], "allowed": list(CLEANUP_KINDS)},
        )
    mode = (mode or "trash").lower()
    if mode not in ("trash", "permanent"):
        raise ValidationError("mode must be 'trash' or 'permanent'.")
    if mode == "permanent" and not confirm:
        raise ValidationError(
            "Permanent deletion requires confirm=true.",
            details={"mode": mode,
                     "hint": "Leave mode='trash' to keep the files recoverable."},
        )

    priced = estimate(selection)
    # Price every item individually *before* the delete: afterwards the rows are
    # gone and the only honest source for "you freed N bytes" is the number the
    # user was shown when they confirmed.
    sizes = storage_query.selection_sizes(selection)

    results = file_ops.delete(selection, mode=mode, confirm=confirm)
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    freed_bytes = sum(int(sizes.get(r.uid, 0)) for r in ok)

    invalidate()
    return {
        "ok": bool(ok),
        "mode": mode,
        "requested": len(selection),
        "deleted": len(ok),
        "failed": len(failed),
        "freed_bytes": freed_bytes,
        "estimated_bytes": int(priced["bytes"]),
        "trash_ids": [int(r.details["trash_id"]) for r in ok
                      if r.details.get("trash_id") is not None],
        "protected_count": int(priced.get("protected_count") or 0),
        "results": [r.as_dict() for r in results],
        "recoverable": mode == "trash",
    }
