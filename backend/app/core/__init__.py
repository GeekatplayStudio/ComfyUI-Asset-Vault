"""Core layer bootstrap.

``api-connectivity`` calls :func:`startup` from the FastAPI lifespan and
:func:`shutdown` on the way out.  Nothing here blocks the event loop for long:
the optional auto-reindex is scheduled on a timer, never awaited inline.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

AUTO_REINDEX_DELAY_S = 2.0

_timer: threading.Timer | None = None


def startup(*, auto_reindex: bool | None = None) -> dict:
    """Migrate, recover interrupted jobs, and optionally schedule a scan."""
    global _timer
    from . import config_service
    from .migrations import migrate

    report: dict = {"migrations": [], "warnings": []}
    report["migrations"] = migrate()
    cfg = config_service.reload_config()
    report["configured"] = cfg.is_configured
    report["comfyui_path"] = str(cfg.comfyui_path) if cfg.comfyui_path else None

    try:
        from ..indexing.service import get_indexer

        report["interrupted_jobs"] = get_indexer().mark_interrupted()
    except Exception as exc:  # noqa: BLE001 - startup must never hard-fail
        report["warnings"].append(f"indexer recovery: {exc}")

    try:
        from ..jobs.hash_service import get_hash_service

        report["hash_jobs_requeued"] = get_hash_service().resume_pending()
    except Exception as exc:  # noqa: BLE001
        report["warnings"].append(f"hash queue recovery: {exc}")

    try:
        from ..enable.service import get_enable_service

        report["enable_jobs_requeued"] = get_enable_service().resume_pending()
    except Exception as exc:  # noqa: BLE001
        report["warnings"].append(f"enable queue recovery: {exc}")

    try:
        from ..services.queries import albums_query

        albums_query.ensure_system_albums()
    except Exception as exc:  # noqa: BLE001
        report["warnings"].append(f"system albums: {exc}")

    try:
        from ..services import file_ops

        report["trash_purged"] = file_ops.purge_expired()
    except Exception as exc:  # noqa: BLE001
        report["warnings"].append(f"trash purge: {exc}")

    try:
        from ..jobs.embed_service import get_embed_service

        report["embeddings"] = get_embed_service().refresh_state()
    except Exception as exc:  # noqa: BLE001
        report["warnings"].append(f"embeddings: {exc}")

    want_auto = cfg.auto_reindex if auto_reindex is None else auto_reindex
    if want_auto and cfg.is_configured:
        _timer = threading.Timer(AUTO_REINDEX_DELAY_S, _auto_reindex)
        _timer.daemon = True
        _timer.start()
        report["auto_reindex"] = "scheduled"
    else:
        report["auto_reindex"] = "off"
    return report


def _auto_reindex() -> None:
    try:
        from ..indexing.service import get_indexer

        get_indexer().start(mode="incremental", trigger="startup")
    except Exception as exc:  # noqa: BLE001 - a failed auto-scan is informational
        log.info("startup auto-reindex skipped: %s", exc)


def shutdown() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
    for closer in (_close_indexer, _close_hash, _close_enable, _close_embed,
                   _close_thumbs, _close_db):
        try:
            closer()
        except Exception as exc:  # noqa: BLE001 - shutdown is best effort
            log.debug("shutdown step failed: %s", exc)


def _close_indexer() -> None:
    from ..indexing.service import shutdown_indexer

    shutdown_indexer()


def _close_hash() -> None:
    from ..jobs.hash_service import shutdown_hash_service

    shutdown_hash_service()


def _close_enable() -> None:
    from ..enable.service import shutdown_enable_service

    shutdown_enable_service()


def _close_embed() -> None:
    from ..jobs.embed_service import shutdown_embed_service

    shutdown_embed_service()


def _close_thumbs() -> None:
    from ..jobs.thumb_service import shutdown_thumb_service

    shutdown_thumb_service()


def _close_db() -> None:
    """Close every reader, truncate the WAL, then close the writer - in that order.

    The order is the whole point.  A checkpoint can only reclaim frames older
    than the oldest open read snapshot, so the readers have to go first or the
    truncate reclaims nothing; and the checkpoint has to happen before the
    writer connection closes, because it is the only connection that can perform
    one.  Closing the writer last also makes it the last connection to the
    database, which is when SQLite deletes the ``-wal`` and ``-shm`` outright.
    """
    from . import db as dbmod

    closed = dbmod.close_all_connections()
    result = dbmod.checkpoint("TRUNCATE", reap=False)
    dbmod.shutdown_writer()
    dbmod.close_all_connections()
    log.info("shutdown: closed %d readers, wal_checkpoint(TRUNCATE) busy=%s "
             "log_pages=%s checkpointed=%s, wal %d -> %d bytes",
             closed, result.get("busy"), result.get("log_pages"),
             result.get("checkpointed"), result.get("wal_before"),
             dbmod.wal_bytes())


def vault_stats(conn=None) -> dict:
    """The `v_vault_stats` view plus derived health counters."""
    from . import db as dbmod

    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, "SELECT * FROM v_vault_stats")
    stats = dict(row) if row else {}
    stats["scan_errors"] = int(dbmod.scalar(
        conn, "SELECT COUNT(*) FROM scan_errors") or 0)
    stats["missing"] = int(dbmod.scalar(
        conn, "SELECT COUNT(*) FROM model_files WHERE missing_since IS NOT NULL") or 0)
    last = dbmod.one(
        conn, "SELECT id, status, finished_at, duration_ms, error_count FROM scan_jobs "
              "ORDER BY created_at DESC LIMIT 1")
    stats["last_scan"] = dict(last) if last else None
    stats["db"] = dbmod.db_stat()
    return stats
