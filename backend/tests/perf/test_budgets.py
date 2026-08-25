"""Performance budgets from ARCHITECTURE 10, asserted rather than asserted-to.

The audit's conclusion was that correctness, not speed, was the bottleneck: the
raw work of walking 231 models and 3,569 outputs took under a second.  That makes
the budgets meaningful — they are not aspirational, they are close to the
measured floor, so a regression shows up immediately.

Everything here is timing-sensitive, so it is marked ``perf``.  The tests that
need the owner's 1.5 TB install to be meaningful are additionally marked ``live``.
Both are deselected from the default run.

The headline numbers this file measures against are in the README's "Scale it was built and
measured against" section.
"""

from __future__ import annotations

import statistics
import threading
import time

import pytest
from builders import write_png_with_prompt, write_safetensors

from app.indexing.service import get_indexer

# --- budgets (ARCHITECTURE 10) ---------------------------------------------
COLD_INDEX_S = 25.0
WARM_INCREMENTAL_S = 1.5
SEARCH_LEXICAL_MS = 25.0
SEARCH_HYBRID_MS = 70.0
MODELS_LIST_MS = 40.0
PING_P95_MS = 50.0
THUMBNAIL_CACHED_MS = 5.0

SCAN_TIMEOUT_S = 300

# Mutating routes require this header (CSRF guard on a loopback-only API).
VAULT_HEADERS = {"X-Vault-Request": "1"}


def p95(samples: list[float]) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def time_scan(mode: str) -> float:
    indexer = get_indexer()
    t0 = time.perf_counter()
    indexer.start(mode=mode, trigger="test")
    deadline = time.monotonic() + SCAN_TIMEOUT_S
    while indexer.running():
        if time.monotonic() > deadline:
            indexer.cancel()
            pytest.fail(f"{mode} scan exceeded {SCAN_TIMEOUT_S}s")
        time.sleep(0.005)
    return time.perf_counter() - t0


def record(name: str, value: float, budget: float, unit: str = "s") -> None:
    """Print the measurement so a run doubles as the report's evidence."""
    verdict = "PASS" if value <= budget else "FAIL"
    print(f"\n  [budget] {name:44} {value:9.3f} {unit}  (budget {budget} {unit})  {verdict}")


# ---------------------------------------------------------------------------
# Hermetic: a synthetic install large enough for the numbers to mean something
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def _sizes():
    return {"models": 200, "outputs": 1200}


@pytest.fixture
def large_synthetic(temp_vault, synthetic_comfyui, _sizes):
    root = synthetic_comfyui
    for i in range(_sizes["models"]):
        write_safetensors(
            root / "models" / "loras" / f"perf_{i:04d}.safetensors",
            {"lora_unet_x.lora_down.weight": ("F16", (16, 320)),
             "lora_unet_x.lora_up.weight": ("F16", (320, 16))})
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "sd15-probe.safetensors"}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "a perf probe subject with several words", "clip": ["1", 1]}},
        "5": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["2", 0], "seed": 1,
                         "steps": 20, "cfg": 7.0, "sampler_name": "euler",
                         "scheduler": "normal"}},
    }
    for i in range(_sizes["outputs"]):
        write_png_with_prompt(root / "output" / f"perf_{i:05d}_.png", graph)
    return root


@pytest.mark.perf
def test_cold_index_of_a_synthetic_install_is_within_budget(large_synthetic, _sizes):
    elapsed = time_scan("full")
    n = _sizes["models"] + _sizes["outputs"]
    record(f"cold index, {n} synthetic items", elapsed, COLD_INDEX_S)
    assert elapsed <= COLD_INDEX_S, f"{elapsed:.2f}s > {COLD_INDEX_S}s"


@pytest.mark.perf
def test_warm_incremental_with_no_changes_is_within_budget(large_synthetic):
    time_scan("full")
    samples = [time_scan("incremental") for _ in range(3)]
    value = p95(samples)
    record("warm incremental, no changes", value, WARM_INCREMENTAL_S)
    assert value <= WARM_INCREMENTAL_S, f"{value:.3f}s > {WARM_INCREMENTAL_S}s ({samples})"


@pytest.mark.perf
def test_lexical_search_is_within_budget(large_synthetic):
    from app.search import hybrid

    time_scan("full")
    hybrid.search("perf", limit=100)  # warm the page cache
    samples = []
    for term in ("perf", "probe", "lora", "subject", "sd15", "euler", "words"):
        for _ in range(10):
            t0 = time.perf_counter()
            hybrid.search(term, limit=100)
            samples.append((time.perf_counter() - t0) * 1000)
    value = p95(samples)
    record("lexical search p95", value, SEARCH_LEXICAL_MS, "ms")
    assert value <= SEARCH_LEXICAL_MS, (
        f"{value:.2f}ms > {SEARCH_LEXICAL_MS}ms (median {statistics.median(samples):.2f}ms)")


@pytest.mark.perf
def test_the_models_list_query_is_within_budget(large_synthetic):
    from app.services.queries import models_query

    time_scan("full")
    models_query.list_models(limit=100)
    samples = []
    for _ in range(30):
        t0 = time.perf_counter()
        models_query.list_models(limit=100)
        samples.append((time.perf_counter() - t0) * 1000)
    value = p95(samples)
    record("models list(limit=100) p95", value, MODELS_LIST_MS, "ms")
    assert value <= MODELS_LIST_MS, f"{value:.2f}ms > {MODELS_LIST_MS}ms"


@pytest.mark.perf
def test_the_api_stays_responsive_during_a_scan(large_synthetic):
    """A scan must not block the event loop; ``/ping`` is the canary."""
    from fastapi.testclient import TestClient

    from app.main import app

    samples: list[float] = []
    stop = threading.Event()

    with TestClient(app) as client:
        def poll():
            while not stop.is_set():
                t0 = time.perf_counter()
                client.get("/api/v1/ping")
                samples.append((time.perf_counter() - t0) * 1000)
                time.sleep(0.01)

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        try:
            time_scan("full")
        finally:
            stop.set()
            poller.join(timeout=10)

    assert len(samples) > 30, f"only {len(samples)} ping samples during the scan"
    value = p95(samples)
    record("/api/v1/ping p95 during a scan", value, PING_P95_MS, "ms")
    assert value <= PING_P95_MS, (
        f"{value:.2f}ms > {PING_P95_MS}ms — the scan is blocking the event loop "
        f"(max {max(samples):.1f}ms)")


# ---------------------------------------------------------------------------
# Live: the owner's real install
#
# These drive the *running* backend over HTTP rather than opening a second
# indexer in the test process.  Two writers against one vault.db would be
# measuring lock contention, not the scanner — and would risk the owner's data.
# ---------------------------------------------------------------------------

def wait_idle(client, timeout_s: float = SCAN_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not client.get("/api/v1/index/status").json().get("active"):
            return
        time.sleep(0.05)
    pytest.fail(f"a scan was still active after {timeout_s}s")


def scan_over_http(client, mode: str) -> float:
    """Start a scan through the API and time it to completion.

    ``active`` does not flip to true synchronously with the POST, so waiting on
    ``active`` alone times a scan at nine milliseconds.  The job id is the only
    reliable handle: wait for *this* job to appear in ``last_completed``.
    """
    wait_idle(client)
    t0 = time.perf_counter()
    r = client.post("/api/v1/index/start", json={"mode": mode})
    assert r.status_code in (200, 202), f"{r.status_code}: {r.text[:300]}"
    job_id = r.json()["job_id"]
    deadline = time.monotonic() + SCAN_TIMEOUT_S
    while time.monotonic() < deadline:
        st = client.get("/api/v1/index/status").json()
        done = st.get("last_completed") or {}
        if not st.get("active") and done.get("id") == job_id:
            return time.perf_counter() - t0
        time.sleep(0.02)
    pytest.fail(f"{mode} scan (job {job_id}) exceeded {SCAN_TIMEOUT_S}s")
    return 0.0


@pytest.mark.live
@pytest.mark.perf
@pytest.mark.slow
def test_cold_index_of_the_real_install(running_server):
    """The headline number: 237 models over 1.589 TB, 3,834 outputs."""
    import httpx

    with httpx.Client(base_url=running_server, timeout=SCAN_TIMEOUT_S,
                      headers=VAULT_HEADERS) as c:
        elapsed = scan_over_http(c, "full")
        stats = c.get("/api/v1/system/stats").json()
    record("REAL cold full index", elapsed, COLD_INDEX_S)
    models = stats.get("models", {})
    total = models.get("total") if isinstance(models, dict) else models
    assert (total or 0) > 100, f"only {total} models indexed; this is not the real install"
    assert elapsed <= COLD_INDEX_S, f"{elapsed:.2f}s > {COLD_INDEX_S}s"


@pytest.mark.live
@pytest.mark.perf
def test_warm_incremental_of_the_real_install(running_server):
    import httpx

    with httpx.Client(base_url=running_server, timeout=SCAN_TIMEOUT_S,
                      headers=VAULT_HEADERS) as c:
        scan_over_http(c, "incremental")  # settle
        samples = [scan_over_http(c, "incremental") for _ in range(3)]
    value = p95(samples)
    record("REAL warm incremental", value, WARM_INCREMENTAL_S)
    assert value <= WARM_INCREMENTAL_S, f"{value:.3f}s > {WARM_INCREMENTAL_S}s ({samples})"


@pytest.mark.live
@pytest.mark.perf
def test_lexical_search_over_the_real_corpus(running_server):
    """6,183 documents, over HTTP, exactly as the UI does it."""
    import httpx

    samples = []
    with httpx.Client(base_url=running_server, timeout=30.0,
                      headers=VAULT_HEADERS) as c:
        wait_idle(c)  # a concurrent scan would be measuring lock contention
        for term in ("flux", "wan", "lora", "vae", "upscale", "portrait", "video"):
            c.get("/api/v1/search", params={"q": term, "limit": 50})
            for _ in range(10):
                t0 = time.perf_counter()
                r = c.get("/api/v1/search", params={"q": term, "limit": 50})
                samples.append((time.perf_counter() - t0) * 1000)
                assert r.status_code == 200
    value = p95(samples)
    record("REAL lexical search p95 (over HTTP)", value, SEARCH_LEXICAL_MS + 25, "ms")
    # the HTTP round trip is not part of the FTS budget, so allow the transport
    assert value <= SEARCH_LEXICAL_MS + 25


@pytest.mark.live
@pytest.mark.perf
def test_models_list_over_http_on_the_real_install(running_server):
    import httpx

    samples = []
    with httpx.Client(base_url=running_server, timeout=30.0,
                      headers=VAULT_HEADERS) as c:
        wait_idle(c)
        c.get("/api/v1/models", params={"limit": 100})
        for _ in range(30):
            t0 = time.perf_counter()
            r = c.get("/api/v1/models", params={"limit": 100})
            samples.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200
    value = p95(samples)
    record("REAL /models?limit=100 p95", value, MODELS_LIST_MS, "ms")
    assert value <= MODELS_LIST_MS, f"{value:.2f}ms > {MODELS_LIST_MS}ms"


@pytest.mark.live
@pytest.mark.perf
@pytest.mark.slow
def test_ping_stays_under_50ms_while_the_real_install_is_scanned(running_server):
    """The gate that matters to a user: the UI must not freeze during a reindex.

    A hard gate, not an expected failure.  QA-PERF-1 was diagnosed to two C
    calls that hold the GIL for their whole duration and so cannot be preempted:
    ``ast.parse`` over a custom node's source (measured to 152 ms on one 563 KB
    file) and ``json.loads`` over a safetensors header (94 ms on one 390 KB
    header).  Per phase, with `/ping` polled at 100 Hz through a forced full
    scan of the real install, `nodes` alone measured p95 235 ms and `models`
    77 ms, while `outputs` - 3,834 files, no such call - measured 7 ms.

    Thread count was ruled out by measurement: one AST worker was no better than
    four (p95 282 ms vs 250 ms), because the GIL makes the *total* blocked time
    invariant.  Both parsers now run in worker processes, which took the whole
    scan from p95 55.8/56.1/55.2 ms to 7.1/6.7/7.6 ms across three runs, and the
    scan itself from 10.5 s to 7.9 s.

    So a regression here means the analysis has come back on-process - check
    that the pool still starts and that `VAULT_NO_CPU_POOL` is not set.
    """
    import httpx

    samples: list[float] = []
    stop = threading.Event()

    def poll():
        with httpx.Client(base_url=running_server, timeout=10.0) as c:
            while not stop.is_set():
                t0 = time.perf_counter()
                try:
                    c.get("/api/v1/ping")
                    samples.append((time.perf_counter() - t0) * 1000)
                except Exception:  # noqa: BLE001 - a dropped sample is a failed sample
                    samples.append(10_000.0)
                time.sleep(0.01)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    try:
        with httpx.Client(base_url=running_server, timeout=SCAN_TIMEOUT_S,
                          headers=VAULT_HEADERS) as c:
            elapsed = scan_over_http(c, "full")
    finally:
        stop.set()
        poller.join(timeout=10)

    assert elapsed > 1.0, f"the scan took {elapsed:.2f}s; nothing was measured"
    assert len(samples) > 50, f"only {len(samples)} ping samples over {elapsed:.1f}s"
    value = p95(samples)
    record("REAL /ping p95 during a full scan", value, PING_P95_MS, "ms")
    print(f"  [budget] ... over {elapsed:.1f} s of scanning, {len(samples)} samples, "
          f"max {max(samples):.1f} ms")
    assert value <= PING_P95_MS, (
        f"{value:.2f}ms > {PING_P95_MS}ms (max {max(samples):.0f}ms) — "
        "the scan blocks the API")


@pytest.mark.live
@pytest.mark.perf
def test_cached_thumbnails_are_served_fast(running_server):
    import httpx

    with httpx.Client(base_url=running_server, timeout=30.0,
                      headers=VAULT_HEADERS) as c:
        wait_idle(c)
        r = c.get("/api/v1/outputs", params={"limit": 12})
        if r.status_code != 200 or not r.json().get("items"):
            pytest.skip("no outputs indexed")
        uids = [f"output:{it['id']}" for it in r.json()["items"]]
        for uid in uids:  # warm the cache
            c.get("/api/v1/files/thumbnail", params={"uid": uid, "size": 320})
        samples = []
        for uid in uids:
            for _ in range(5):
                t0 = time.perf_counter()
                resp = c.get("/api/v1/files/thumbnail", params={"uid": uid, "size": 320})
                if resp.status_code == 200:
                    samples.append((time.perf_counter() - t0) * 1000)
    if not samples:
        pytest.skip("no thumbnails could be served")
    value = p95(samples)
    record("REAL cached thumbnail p95 (over HTTP)", value, THUMBNAIL_CACHED_MS + 20, "ms")
    assert value <= THUMBNAIL_CACHED_MS + 20
