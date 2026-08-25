"""Folder watching: the ``watch_enabled`` setting, made true.

A poll-driven watcher rather than an OS event subscription, on purpose:

* the incremental scan is already the cheap change probe - a no-change pass
  over the reference install finishes well under two seconds and touches
  nothing it can skip by fingerprint, so "watch" and "rescan cheaply" are the
  same operation here;
* OS event APIs differ per platform and would add the first watcher-specific
  dependency to the backend for latency nobody asked for - a model that
  finished copying is in the vault within the poll interval either way;
* the thread reads the live config every tick, so flipping the Settings
  toggle takes effect without a restart, like every other config consumer.

The thread always runs; ``watch_enabled=False`` just makes every tick a no-op.
"""

from __future__ import annotations

import logging
import threading

from ..core import config_service

log = logging.getLogger(__name__)

#: Seconds between polls.  A change is visible within this bound.
WATCH_INTERVAL_S = 120

_thread: threading.Thread | None = None
_stop = threading.Event()
_lock = threading.Lock()


def _tick() -> None:
    cfg = config_service.get_config()
    if not (cfg.watch_enabled and cfg.is_configured):
        return
    from ..indexing.service import get_indexer

    indexer = get_indexer()
    if indexer.running():
        return
    try:
        indexer.start(mode="incremental", trigger="watch")
    except Exception as exc:  # noqa: BLE001 - a busy or failed pass waits for the next tick
        log.debug("watch pass skipped: %s", exc)


def _run() -> None:
    while not _stop.wait(WATCH_INTERVAL_S):
        try:
            _tick()
        except Exception:
            log.exception("watch tick failed; the watcher keeps running")


def start_watcher() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_run, daemon=True, name="folder-watch")
        _thread.start()


def shutdown_watcher() -> None:
    global _thread
    with _lock:
        _stop.set()
        _thread = None
