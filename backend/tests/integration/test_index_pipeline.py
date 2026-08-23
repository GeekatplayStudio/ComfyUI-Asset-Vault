"""A whole scan, end to end, against a synthetic install.

B1's real damage was not the exception — it was that ``conn.commit()`` sat at the
very end, so one bad row discarded every model, node and workflow already
inserted.  The test that would have caught it is this one: run a full scan and
assert rows exist afterwards.
"""

from __future__ import annotations

import json
import time

import pytest
from builders import write_png_with_prompt, write_safetensors

from app.core import db as dbmod
from app.indexing.service import get_indexer

TIMEOUT_S = 120


def run_scan(mode: str = "full", **kw) -> dict:
    indexer = get_indexer()
    indexer.start(mode=mode, trigger="test", **kw)
    deadline = time.monotonic() + TIMEOUT_S
    while indexer.running():
        if time.monotonic() > deadline:
            indexer.cancel()
            pytest.fail(f"scan did not finish within {TIMEOUT_S}s")
        time.sleep(0.02)
    return indexer.status()


def counts() -> dict[str, int]:
    conn = dbmod.get_ro()
    out = {}
    for table in ("models", "node_packages", "node_classes", "workflows", "outputs",
                  "search_docs", "scan_errors", "roots"):
        out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    return out


@pytest.fixture
def populated(temp_vault, synthetic_comfyui):
    """Add the assets a scan has to survive, including B1's exact shape."""
    root = synthetic_comfyui
    # 40 outputs whose prompt text is a node LINK, not a literal
    for i in range(40):
        prompt = {
            "1": {"class_type": "CheckpointLoaderSimple",
                  "inputs": {"ckpt_name": "sd15-probe.safetensors"}},
            "2": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": f"probe positive {i}", "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": "probe negative", "clip": ["1", 1]}},
            "5": {"class_type": "KSampler",
                  "inputs": {"model": ["1", 0], "positive": ["2", 0],
                             "negative": ["3", 0], "seed": i, "steps": 20,
                             "cfg": 7.0, "sampler_name": "euler",
                             "scheduler": "normal"}},
            "9": {"class_type": "SaveImage",
                  "inputs": {"images": ["5", 0], "filename_prefix": "probe"}},
        }
        if i % 2 == 0:  # half carry a link-valued text input
            prompt["8"] = {"class_type": "PrimitiveString",
                           "inputs": {"value": f"linked positive {i}"}}
            prompt["2"]["inputs"]["text"] = ["8", 0]
        write_png_with_prompt(root / "output" / f"probe_{i:05d}_.png", prompt)

    # a custom node package that only S5 can read
    pkg = root / "custom_nodes" / "probe_pack"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        'class ProbeNode:\n'
        '    @classmethod\n'
        '    def INPUT_TYPES(cls): return {"required": {"x": ("INT",)}}\n'
        '    RETURN_TYPES = ("INT",)\n'
        '    FUNCTION = "run"\n'
        '    CATEGORY = "probe"\n'
        'NODE_CLASS_MAPPINGS = {"ProbeNode": ProbeNode}\n', encoding="utf-8")
    return root


def test_a_full_scan_completes_and_persists_every_kind(populated):
    status = run_scan("full")
    assert status.get("state") in (None, "idle") or not status.get("running")
    c = counts()
    assert c["models"] >= 3, f"models table is empty after a scan: {c}"
    assert c["workflows"] >= 1, c
    assert c["outputs"] >= 40, c
    assert c["node_packages"] >= 1, c
    assert c["node_classes"] >= 1, c
    assert c["roots"] >= 1, c


def test_the_job_row_records_completion_not_failure(populated):
    run_scan("full")
    conn = dbmod.get_ro()
    row = conn.execute("SELECT status, error_message FROM scan_jobs "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "completed", f"job ended {row['status']}: {row['error_message']}"


def test_link_valued_prompts_are_resolved_and_stored(populated):
    """B1 at pipeline scale, not just in the parser."""
    run_scan("full")
    conn = dbmod.get_ro()
    with_prompt = conn.execute(
        "SELECT COUNT(*) FROM outputs WHERE positive_prompt IS NOT NULL "
        "AND positive_prompt <> ''").fetchone()[0]
    assert with_prompt >= 40, f"only {with_prompt} outputs got a prompt"
    linked = conn.execute(
        "SELECT COUNT(*) FROM outputs WHERE positive_prompt LIKE 'linked positive%'"
    ).fetchone()[0]
    assert linked >= 20, (
        f"only {linked} link-valued prompts resolved; the link was not followed")


def test_no_output_reports_positive_equal_to_negative(populated):
    run_scan("full")
    conn = dbmod.get_ro()
    same = conn.execute(
        "SELECT COUNT(*) FROM outputs WHERE positive_prompt IS NOT NULL "
        "AND positive_prompt <> '' AND positive_prompt = negative_prompt").fetchone()[0]
    assert same == 0, f"{same} outputs have pos == neg — the resolver gave up"


def test_the_search_index_matches_the_row_count(populated):
    run_scan("full")
    conn = dbmod.get_ro()
    rows = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
               for t in ("models", "node_packages", "node_classes", "workflows", "outputs"))
    docs = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    fts = conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
    assert docs == rows, f"{docs} search documents for {rows} rows"
    assert fts == docs, f"FTS holds {fts} rows but search_docs holds {docs}"


def test_a_scan_is_idempotent(populated):
    run_scan("full")
    first = counts()
    run_scan("full")
    second = counts()
    for table in ("models", "node_packages", "node_classes", "workflows", "outputs",
                  "search_docs"):
        assert first[table] == second[table], (
            f"{table} changed from {first[table]} to {second[table]} on a re-scan")


def test_models_get_an_architecture_not_a_shrug(populated):
    run_scan("full")
    conn = dbmod.get_ro()
    unknown = conn.execute(
        "SELECT name FROM models WHERE base_model_family IS NULL "
        "OR base_model_family IN ('', 'Unknown')").fetchall()
    assert not unknown, f"models with no detected family: {[r[0] for r in unknown]}"


def test_a_workflow_records_its_missing_node(populated):
    run_scan("full")
    conn = dbmod.get_ro()
    row = conn.execute("SELECT missing_node_count, is_runnable FROM workflows "
                       "WHERE name LIKE 'probe%' LIMIT 1").fetchone()
    assert row is not None
    assert row["missing_node_count"] >= 1, (
        "the fixture references AbsentThirdPartyNode, which is not installed")


def test_nothing_is_written_outside_the_configured_root(populated, tmp_path):
    """A scan reads; it must not create files in the library it is indexing."""
    before = {p for p in populated.rglob("*") if p.is_file()}
    run_scan("full")
    after = {p for p in populated.rglob("*") if p.is_file()}
    assert after == before, f"scan created or removed files: {after ^ before}"


def test_a_scan_records_provenance_for_resolved_values(populated):
    run_scan("full")
    conn = dbmod.get_ro()
    row = conn.execute(
        "SELECT provenance_json FROM outputs WHERE provenance_json IS NOT NULL "
        "AND provenance_json <> '' LIMIT 1").fetchone()
    assert row is not None, "no output recorded where its values came from"
    assert isinstance(json.loads(row["provenance_json"]), dict)


def test_an_unreadable_model_is_recorded_and_the_rest_still_land(populated):
    """One corrupt file must cost exactly one row, not the whole scan."""
    bad = populated / "models" / "checkpoints" / "corrupt.safetensors"
    bad.write_bytes(b"\xff" * 64)
    run_scan("full")
    c = counts()
    assert c["models"] >= 3, "healthy models were lost because of one bad file"
    conn = dbmod.get_ro()
    job = conn.execute("SELECT status FROM scan_jobs ORDER BY id DESC LIMIT 1").fetchone()
    assert job["status"] == "completed"


def test_a_deleted_file_is_pruned_on_the_next_scan(populated):
    run_scan("full")
    conn = dbmod.get_ro()
    before = conn.execute("SELECT COUNT(*) FROM outputs WHERE missing_since IS NULL"
                          ).fetchone()[0]
    (populated / "output" / "probe_00000_.png").unlink()
    run_scan("full")
    conn = dbmod.get_ro()
    after = conn.execute("SELECT COUNT(*) FROM outputs WHERE missing_since IS NULL"
                         ).fetchone()[0]
    assert after == before - 1, f"prune did not notice the deletion ({before} -> {after})"


def test_a_new_model_appears_without_a_full_rescan(populated):
    run_scan("full")
    conn = dbmod.get_ro()
    before = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    write_safetensors(
        populated / "models" / "loras" / "late-arrival.safetensors",
        {"lora_unet_x.lora_down.weight": ("F16", (16, 320)),
         "lora_unet_x.lora_up.weight": ("F16", (320, 16))})
    run_scan("incremental")
    conn = dbmod.get_ro()
    after = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    assert after == before + 1, f"incremental scan missed a new file ({before} -> {after})"
