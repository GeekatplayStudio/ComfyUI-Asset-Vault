"""Picklable probes for the off-GIL analysis pool.

This lives at the top of ``tests/`` rather than inside a test module on purpose.
``multiprocessing`` spawn pickles a callable **by name**, so the worker process
has to be able to import it; the child inherits the parent's ``sys.path``, and
``conftest`` puts this directory on it, exactly as it does for ``builders``.  A
helper defined inside ``tests/integration/test_*.py`` would not be importable
there and the pool would fail on the pickle rather than on the thing under test.
"""

from __future__ import annotations

import multiprocessing
import os
from dataclasses import dataclass


@dataclass
class Probe:
    """Stands in for ``ModelWork``/``PackageWork``: a picklable dataclass in,
    the same dataclass back."""
    abs_path: str
    value: int = 0
    doubled: int | None = None
    pid: int | None = None


def double_it(probe: Probe) -> Probe:
    """The happy path."""
    probe.doubled = probe.value * 2
    probe.pid = os.getpid()
    return probe


def explode_on_one(probe: Probe) -> Probe:
    """Fails for exactly one item, the way a single unparseable file does."""
    if probe.value == 3:
        raise RuntimeError("injected failure in the worker process")
    probe.doubled = probe.value * 2
    probe.pid = os.getpid()
    return probe


def kill_the_worker(probe: Probe) -> Probe:
    """Takes the whole worker process down, not just the item.

    A segfault or an OOM in a child is not catchable in the parent - the pool
    reports ``BrokenProcessPool`` and every queued item with it - so the caller
    has to be able to finish the batch some other way.

    It only pulls the trigger in a *child*.  When the caller retries the item on
    threads it is running here, in the test process, and ``os._exit`` there
    would take the whole suite with it - which is also precisely why the retry
    must exist: an item that kills a worker still has to be finishable.
    """
    if probe.value == 2 and multiprocessing.parent_process() is not None:
        os._exit(1)
    probe.doubled = probe.value * 2
    probe.pid = os.getpid()
    return probe
