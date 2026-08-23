"""The off-GIL analysis pool must keep B1's guarantee: one bad file, one error row.

Moving `ast.parse` and the safetensors header read into worker processes fixed
QA-PERF-1, but it also moved them across a process boundary, where an exception
arrives as a pickled copy and a crash arrives as a dead worker.  Neither may
cost the scan anything more than the item that caused it.
"""

from __future__ import annotations

import os
import threading

import pytest
from cpu_probe import Probe, double_it, explode_on_one, kill_the_worker

from app.core import progress
from app.indexing import service as svc


@pytest.fixture
def ctx():
    """A ScanContext with just enough wired up to collect error rows."""
    from concurrent.futures import ThreadPoolExecutor

    pools = [ThreadPoolExecutor(max_workers=2) for _ in range(3)]
    c = svc.ScanContext(
        job_id=1, kind="full", trigger="test", force=True, enrich_online=False,
        phases=svc.PHASES, cfg=None, cancel=threading.Event(),
        bus=progress.bus("test-cpu-pool"),
        ex_io=pools[0], ex_ast=pools[1], ex_img=pools[2],
    )
    try:
        yield c
    finally:
        svc.shutdown_cpu_pool()
        # `_cpu_pool_broken` is a process-wide latch: once a worker crashes the
        # pool is not trusted again for the life of the process.  That is right
        # in production and wrong between tests, where it would quietly move
        # every later scan - including the perf gate - onto threads.
        svc._cpu_pool_broken = False
        for p in pools:
            p.shutdown(wait=False, cancel_futures=True)


def _probes(n: int) -> list[Probe]:
    return [Probe(abs_path=f"C:\\probe\\{i}.safetensors", value=i) for i in range(n)]


def test_the_pool_really_runs_the_work_in_another_process(ctx):
    """Otherwise this whole exercise bought nothing: the GIL would be the same one."""
    items = _probes(8)
    out = svc.map_cpu(ctx, double_it, items, phase="models", kind="model",
                      fallback=ctx.ex_io)
    assert [o.doubled for o in out] == [i * 2 for i in range(8)]
    pids = {o.pid for o in out}
    assert pids and os.getpid() not in pids, (
        f"the work ran in this process ({pids}); the GIL was never released")


def test_one_failing_item_costs_only_itself(ctx):
    items = _probes(8)
    out = svc.map_cpu(ctx, explode_on_one, items, phase="models", kind="model",
                      fallback=ctx.ex_io)

    assert len(out) == 8
    assert out[3] is None, "the failing item should come back as None"
    assert [o.doubled for i, o in enumerate(out) if i != 3] == \
           [i * 2 for i in range(8) if i != 3]
    assert ctx.error_count == 1, f"expected one error row, got {ctx.error_count}"
    rows = ctx.drain_errors()
    assert len(rows) == 1
    assert "3.safetensors" in (rows[0][3] or ""), (
        f"the error row must name the file that failed: {rows[0]}")


def test_a_worker_that_dies_does_not_lose_the_batch(ctx):
    """A dead child breaks every future the pool holds, including queued ones.

    The rest of the batch has to be finished on threads rather than silently
    dropped - that is the difference between a slow scan and a lost library.
    """
    items = _probes(6)
    out = svc.map_cpu(ctx, kill_the_worker, items, phase="models", kind="model",
                      fallback=ctx.ex_io)

    assert len(out) == 6, f"the batch lost items: {out}"
    completed = [o for o in out if o is not None]
    assert len(completed) == 6, (
        f"only {len(completed)} of 6 items survived a worker crash: {out}")
    assert [o.doubled for o in out] == [i * 2 for i in range(6)]
    # The tail of the batch is finished here, in this process, once the pool is
    # gone - which is what makes the crash cost time rather than data.
    assert os.getpid() in {o.pid for o in out}
    # And the pool is not trusted again for the rest of the scan.
    assert svc._cpu_pool_broken is True


def test_a_batch_too_small_to_be_worth_a_spawn_stays_in_process(ctx):
    """A warm incremental scan has almost nothing to analyse and must not pay
    the price of starting interpreters to find that out."""
    items = _probes(1)
    out = svc.map_cpu(ctx, double_it, items, phase="models", kind="model",
                      fallback=ctx.ex_io)
    assert [o.doubled for o in out] == [0]
    assert out[0].pid == os.getpid()


def test_the_pool_can_be_switched_off_entirely(ctx, monkeypatch):
    """The fallback is a supported configuration, not just an error path."""
    monkeypatch.setenv("VAULT_NO_CPU_POOL", "1")
    items = _probes(8)
    out = svc.map_cpu(ctx, double_it, items, phase="models", kind="model",
                      fallback=ctx.ex_io)
    assert [o.doubled for o in out] == [i * 2 for i in range(8)]
    assert {o.pid for o in out} == {os.getpid()}


def test_no_items_needs_no_pool(ctx):
    assert svc.map_cpu(ctx, double_it, [], phase="models", kind="model",
                       fallback=ctx.ex_io) == []
