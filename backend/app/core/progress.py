"""Broadcast progress bus feeding the SSE endpoints.

Publishers are worker threads; subscribers are asyncio consumers in the request
loop.  ``progress`` events are coalesced to <=10 Hz per (channel, phase).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

MAX_QUEUE = 1000
COALESCE_INTERVAL = 0.1  # seconds -> <=10 Hz


class ProgressBus:
    """One bus per stream channel (``index``, ``hash``, ``embed``)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._last_emit: dict[str, float] = {}
        self._last_payload: dict[str, dict] = {}
        self._history: list[tuple[str, dict]] = []

    # -- publish (worker threads) -----------------------------------------
    def publish(self, event: str, payload: dict, *, coalesce_key: str | None = None) -> None:
        if event == "progress" and coalesce_key is not None:
            now = time.monotonic()
            with self._lock:
                last = self._last_emit.get(coalesce_key, 0.0)
                self._last_payload[coalesce_key] = payload
                if now - last < COALESCE_INTERVAL:
                    return
                self._last_emit[coalesce_key] = now
        self._dispatch(event, payload)

    def flush(self, coalesce_key: str) -> None:
        with self._lock:
            payload = self._last_payload.pop(coalesce_key, None)
            self._last_emit.pop(coalesce_key, None)
        if payload is not None:
            self._dispatch("progress", payload)

    def _dispatch(self, event: str, payload: dict) -> None:
        item = (event, payload)
        with self._lock:
            if event in ("phase", "done", "error"):
                self._history.append(item)
                del self._history[:-200]
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._put, q, item, loop)
            except RuntimeError:
                self._remove(loop, q)

    def _put(self, q: asyncio.Queue, item: tuple[str, dict],
             loop: asyncio.AbstractEventLoop) -> None:
        if q.qsize() >= MAX_QUEUE:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(("overflow", {"dropped": q.qsize()}))
            self._remove(loop, q)
            return
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            self._remove(loop, q)

    def _remove(self, loop: asyncio.AbstractEventLoop, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(x, y) for (x, y) in self._subs if y is not q]

    # -- subscribe (asyncio) ----------------------------------------------
    async def subscribe(self, heartbeat: float = 15.0) -> AsyncIterator[tuple[str, dict]]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE + 8)
        with self._lock:
            self._subs.append((loop, q))
        try:
            while True:
                try:
                    event, payload = await asyncio.wait_for(q.get(), timeout=heartbeat)
                except TimeoutError:
                    yield ("heartbeat", {"t": int(time.time() * 1000)})
                    continue
                yield (event, payload)
                if event in ("done", "overflow") and event == "overflow":
                    return
        finally:
            self._remove(loop, q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


_buses: dict[str, ProgressBus] = {}
_buses_lock = threading.Lock()


def bus(name: str) -> ProgressBus:
    with _buses_lock:
        b = _buses.get(name)
        if b is None:
            b = ProgressBus(name)
            _buses[name] = b
        return b


def eta_ms(done: int, total: int, elapsed_s: float) -> int | None:
    if done <= 0 or total <= 0 or elapsed_s <= 0 or done >= total:
        return None
    rate = done / elapsed_s
    if rate <= 0:
        return None
    return int((total - done) / rate * 1000)


def rate_per_s(done: int, elapsed_s: float) -> float:
    return round(done / elapsed_s, 2) if elapsed_s > 0 else 0.0


def _unused(*_: Any) -> None:  # pragma: no cover - keeps typing import meaningful
    return None
