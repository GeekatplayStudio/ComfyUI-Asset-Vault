"""One bad item costs one row — never the scan.

This is the structural half of B1.  The crash itself was a bind error, but what
turned it into "nothing is ever indexed" was that a single failure propagated all
the way out of the scan, past every ``INSERT``, and the deferred ``conn.commit()``
was never reached.  Two hundred and thirty-seven models, thirty-four packages and
two hundred and twelve workflows were rolled back because one output had a list
where a string was expected.

So the property under test is not "parsing is correct".  It is: **failure is
local**.  A failure injected into exactly one item must leave every other item
committed, must appear as exactly one ``scan_errors`` row, and must leave the job
in ``completed`` — not ``failed``, because the scan did in fact do its job.
"""

from __future__ import annotations

import time

import pytest
from builders import write_png_with_prompt, write_safetensors

from app.core import db as dbmod
from app.indexing.service import get_indexer

N_ITEMS = 60
TIMEOUT_S = 120


def run_scan(mode: str = "full") -> None:
    indexer = get_indexer()
    indexer.start(mode=mode, trigger="test")
    deadline = time.monotonic() + TIMEOUT_S
    while indexer.running():
        if time.monotonic() > deadline:
            indexer.cancel()
            pytest.fail(f"scan did not finish within {TIMEOUT_S}s")
        time.sleep(0.02)


def last_job() -> dict:
    conn = dbmod.get_ro()
    row = conn.execute(
        "SELECT id, status, error_count, error_message, items_done, items_skipped "
        "FROM scan_jobs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row)


def job_errors(job_id: int) -> list[dict]:
    conn = dbmod.get_ro()
    return [dict(r) for r in conn.execute(
        "SELECT phase, kind, abs_path, code, message FROM scan_errors WHERE job_id = ?",
        (job_id,))]


def table_count(table: str) -> int:
    return dbmod.get_ro().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


@pytest.fixture
def many_outputs(temp_vault, synthetic_comfyui):
    root = synthetic_comfyui
    prompt = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "sd15-probe.safetensors"}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "isolation probe", "clip": ["1", 1]}},
        "5": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["2", 0], "seed": 1,
                         "steps": 10, "cfg": 5.0, "sampler_name": "euler",
                         "scheduler": "normal"}},
    }
    for i in range(N_ITEMS):
        write_png_with_prompt(root / "output" / f"iso_{i:05d}_.png", prompt)
    return root


@pytest.fixture
def many_models(temp_vault, synthetic_comfyui):
    root = synthetic_comfyui
    for i in range(N_ITEMS):
        write_safetensors(
            root / "models" / "loras" / f"iso_lora_{i:03d}.safetensors",
            {"lora_unet_x.lora_down.weight": ("F16", (16, 320)),
             "lora_unet_x.lora_up.weight": ("F16", (320, 16))})
    return root


# ---------------------------------------------------------------------------
# Injected failure, outputs phase
# ---------------------------------------------------------------------------

def test_one_failing_output_leaves_every_other_output_committed(many_outputs, monkeypatch):
    from app.parsers import image_meta

    victim = "iso_00007_.png"
    original = image_meta.read_output

    def exploding(path, *a, **kw):
        if victim in str(path):
            raise RuntimeError("injected failure")
        return original(path, *a, **kw)

    monkeypatch.setattr(image_meta, "read_output", exploding)
    run_scan("full")

    job = last_job()
    assert job["status"] == "completed", (
        f"one bad item ended the job as {job['status']}: {job['error_message']}")

    stored = table_count("outputs")
    assert stored == N_ITEMS - 1, (
        f"{stored} of {N_ITEMS} outputs committed; the failure was not isolated")

    errs = [e for e in job_errors(job["id"]) if victim in (e["abs_path"] or "")]
    assert len(errs) == 1, f"expected exactly one scan_errors row, got {errs}"
    assert errs[0]["phase"] == "outputs"
    assert errs[0]["code"], "an error row with no code is not actionable"


def test_the_error_row_names_the_file_that_failed(many_outputs, monkeypatch):
    from app.parsers import image_meta

    victim = "iso_00011_.png"
    original = image_meta.read_output
    monkeypatch.setattr(image_meta, "read_output",
                        lambda p, *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
                        if victim in str(p) else original(p, *a, **k))
    run_scan("full")
    rows = job_errors(last_job()["id"])
    assert any(victim in (r["abs_path"] or "") for r in rows), (
        "the failing path must be recorded, or the user cannot find it")


def test_a_failure_does_not_poison_the_search_index(many_outputs, monkeypatch):
    from app.parsers import image_meta

    original = image_meta.read_output
    monkeypatch.setattr(image_meta, "read_output",
                        lambda p, *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
                        if "iso_00003_" in str(p) else original(p, *a, **k))
    run_scan("full")
    conn = dbmod.get_ro()
    rows = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
               for t in ("models", "node_packages", "node_classes", "workflows", "outputs"))
    docs = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    assert docs == rows, f"{docs} documents for {rows} rows after a failed item"


# ---------------------------------------------------------------------------
# Injected failure, models phase
# ---------------------------------------------------------------------------

def test_one_failing_model_leaves_every_other_model_committed(many_models, monkeypatch):
    # Header parsing normally runs in worker processes (QA-PERF-1), and a
    # monkeypatch cannot cross a process boundary, so this pins the in-process
    # analyser — which is production code: it is what `map_cpu` falls back to
    # when the pool cannot start or the batch is too small to be worth one.
    # `tests/integration/test_wal_lifecycle.py` is not the pair for this;
    # `test_cpu_pool_isolation.py` covers the same guarantee in the pool path.
    monkeypatch.setenv("VAULT_NO_CPU_POOL", "1")
    from app.parsers import safetensors_header

    victim = "iso_lora_042.safetensors"
    original = safetensors_header.read_header

    def exploding(path, *a, **kw):
        if victim in str(path):
            raise RuntimeError("injected failure")
        return original(path, *a, **kw)

    monkeypatch.setattr(safetensors_header, "read_header", exploding)
    run_scan("full")

    job = last_job()
    assert job["status"] == "completed", job["error_message"]
    # 3 baseline models from the fixture plus N_ITEMS loras, minus the victim
    stored = table_count("models")
    assert stored == N_ITEMS + 3 - 1, (
        f"{stored} models committed; expected {N_ITEMS + 2}")
    errs = [e for e in job_errors(job["id"]) if victim in (e["abs_path"] or "")]
    assert len(errs) == 1, f"expected exactly one error row, got {errs}"


def test_a_failure_in_one_phase_does_not_stop_later_phases(many_models, monkeypatch):
    """Outputs must still be indexed even when the models phase had a casualty."""
    monkeypatch.setenv("VAULT_NO_CPU_POOL", "1")  # see the test above
    from app.parsers import safetensors_header

    original = safetensors_header.read_header
    monkeypatch.setattr(safetensors_header, "read_header",
                        lambda p, *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
                        if "iso_lora_000" in str(p) else original(p, *a, **k))
    write_png_with_prompt(many_models / "output" / "after_failure_.png",
                          {"1": {"class_type": "SaveImage", "inputs": {}}})
    run_scan("full")
    assert last_job()["status"] == "completed"
    assert table_count("outputs") >= 1, "the outputs phase never ran"
    assert table_count("workflows") >= 1, "the workflows phase never ran"


# ---------------------------------------------------------------------------
# Many failures, and the shape of the report
# ---------------------------------------------------------------------------

def test_many_failures_still_complete_and_are_all_reported(many_outputs, monkeypatch):
    from app.parsers import image_meta

    victims = {f"iso_{i:05d}_.png" for i in range(0, N_ITEMS, 5)}
    original = image_meta.read_output

    def exploding(path, *a, **kw):
        if any(v in str(path) for v in victims):
            raise RuntimeError("injected failure")
        return original(path, *a, **kw)

    monkeypatch.setattr(image_meta, "read_output", exploding)
    run_scan("full")

    job = last_job()
    assert job["status"] == "completed"
    assert table_count("outputs") == N_ITEMS - len(victims)
    rows = job_errors(job["id"])
    named = {r["abs_path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for r in rows
             if r["abs_path"]}
    assert victims <= named, f"unreported failures: {sorted(victims - named)}"


def test_error_rows_carry_a_stable_code_not_just_a_message(many_outputs, monkeypatch):
    from app.parsers import image_meta

    original = image_meta.read_output
    monkeypatch.setattr(image_meta, "read_output",
                        lambda p, *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
                        if "iso_00001_" in str(p) else original(p, *a, **k))
    run_scan("full")
    for row in job_errors(last_job()["id"]):
        assert row["code"], f"error row without a code: {row}"
        assert row["code"] == row["code"].upper(), (
            f"error codes must be stable identifiers, got {row['code']!r}")


def test_the_job_error_count_matches_the_rows_written(many_outputs, monkeypatch):
    from app.parsers import image_meta

    victims = {f"iso_{i:05d}_.png" for i in (2, 13, 44)}
    original = image_meta.read_output
    monkeypatch.setattr(image_meta, "read_output",
                        lambda p, *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
                        if any(v in str(p) for v in victims) else original(p, *a, **k))
    run_scan("full")
    job = last_job()
    rows = job_errors(job["id"])
    assert job["error_count"] == len(rows), (
        f"job reports {job['error_count']} errors but wrote {len(rows)} rows")


def test_a_failure_in_the_prompt_graph_is_isolated_like_any_other(many_outputs, monkeypatch):
    """B1's literal injection point."""
    from app.parsers import graph_utils

    original = graph_utils.summarize_graph
    calls = {"n": 0}

    def exploding(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("injected graph failure")
        return original(*a, **kw)

    monkeypatch.setattr(graph_utils, "summarize_graph", exploding)
    run_scan("full")
    job = last_job()
    assert job["status"] == "completed", job["error_message"]
    assert table_count("outputs") >= N_ITEMS - 1, (
        "a single graph failure discarded more than its own row")
