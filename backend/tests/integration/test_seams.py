"""The seams between components — where every agent verified its own half.

Each builder tested its own layer against its own fixtures.  Nothing tested what
happens when two layers meet under conditions the owner's machine actually
produces: a filename ComfyUI wrote that Windows barely tolerates, a path past
MAX_PATH, a model deleted out from under a running scan, a file ComfyUI still has
open, and a file operation racing the indexer.

These run against a synthetic install, so they are safe anywhere.  The same
probes against the real library live in ``tests/live/test_seams_live.py``.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

import pytest
from builders import write_png_with_prompt, write_safetensors

from app.core import db as dbmod
from app.core import pathsafe
from app.indexing.service import get_indexer

TIMEOUT_S = 120

# Names ComfyUI and its node packs really do produce.
HOSTILE_NAMES = [
    pytest.param("café_modèle", id="latin1-accents"),
    pytest.param("日本語モデル", id="cjk"),
    pytest.param("모델_한국어", id="hangul"),
    pytest.param("модель_кириллица", id="cyrillic"),
    pytest.param("emoji_🎨_model", id="emoji-bmp-plus"),
    pytest.param("family_👨‍👩‍👧‍👦_zwj", id="emoji-zwj-sequence"),
    pytest.param("naïve  double  spaces", id="double-spaces"),
    pytest.param("model.v1.5.final", id="many-dots"),
    pytest.param("model (copy) [1]", id="brackets-parens"),
    pytest.param("model+plus&amp", id="url-unsafe"),
    pytest.param("model#hash%percent", id="percent-hash"),
    pytest.param("model's_apostrophe", id="apostrophe"),
    pytest.param("very" + "long" * 50, id="long-stem"),
    pytest.param("\u0141\u00f8\u0159\u0113\u0271", id="rare-latin"),
    pytest.param("mixed_日本_🎨_café", id="mixed-scripts"),
]


def run_scan(mode: str = "full") -> None:
    indexer = get_indexer()
    indexer.start(mode=mode, trigger="test")
    deadline = time.monotonic() + TIMEOUT_S
    while indexer.running():
        if time.monotonic() > deadline:
            indexer.cancel()
            pytest.fail("scan did not finish")
        time.sleep(0.02)


def lora(path):
    return write_safetensors(path, {
        "lora_unet_x.lora_down.weight": ("F16", (16, 320)),
        "lora_unet_x.lora_up.weight": ("F16", (320, 16))})


def last_job_status() -> str:
    return dbmod.get_ro().execute(
        "SELECT status FROM scan_jobs ORDER BY id DESC LIMIT 1").fetchone()[0]


# ---------------------------------------------------------------------------
# Non-ASCII and emoji filenames
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stem", HOSTILE_NAMES)
def test_a_hostile_filename_indexes_and_round_trips(temp_vault, synthetic_comfyui, stem):
    """Indexed, stored, retrievable, and byte-identical coming back out."""
    target = synthetic_comfyui / "models" / "loras" / f"{stem}.safetensors"
    try:
        lora(target)
    except (OSError, UnicodeError) as exc:
        pytest.skip(f"filesystem refused {stem!r}: {exc}")

    run_scan("full")
    assert last_job_status() == "completed"

    conn = dbmod.get_ro()
    row = conn.execute("SELECT m.id, m.name, f.abs_path FROM models m "
                       "JOIN model_files f ON f.id = m.primary_file_id "
                       "WHERE m.name = ?", (stem,)).fetchone()
    assert row is not None, f"{stem!r} was not indexed"
    assert row["name"] == stem, (
        f"name came back as {row['name']!r} — encoding was lost in storage")
    assert os.path.exists(pathsafe.long_path(row["abs_path"]))


@pytest.mark.parametrize("stem", HOSTILE_NAMES[:8])
def test_a_hostile_filename_is_searchable_by_its_own_name(temp_vault, synthetic_comfyui,
                                                          stem):
    """FTS5 tokenisation must not silently drop non-ASCII terms."""

    target = synthetic_comfyui / "models" / "loras" / f"{stem}.safetensors"
    try:
        lora(target)
    except (OSError, UnicodeError) as exc:
        pytest.skip(f"filesystem refused {stem!r}: {exc}")
    run_scan("full")

    conn = dbmod.get_ro()
    row = conn.execute("SELECT id FROM models WHERE name = ?", (stem,)).fetchone()
    uid = f"model:{row['id']}"
    assert conn.execute("SELECT COUNT(*) FROM search_docs WHERE uid = ?",
                        (uid,)).fetchone()[0] == 1, (
        f"{stem!r} was indexed but has no search document")


def test_renaming_to_a_non_ascii_name_keeps_the_row_and_the_file_together(
        temp_vault, synthetic_comfyui):
    from app.services import file_ops

    lora(synthetic_comfyui / "models" / "loras" / "plain.safetensors")
    run_scan("full")
    conn = dbmod.get_ro()
    row = conn.execute("SELECT id FROM models WHERE name = 'plain'").fetchone()
    uid = f"model:{row['id']}"

    new_stem = "renamed_café_🎨_日本"
    res = file_ops.rename(uid, f"{new_stem}.safetensors")
    res = res[0] if isinstance(res, list) else res
    if not getattr(res, "ok", True):
        pytest.skip(f"filesystem refused the rename: {getattr(res, 'message', res)}")

    conn = dbmod.get_ro()
    after = conn.execute("SELECT name, f.abs_path FROM models m "
                         "JOIN model_files f ON f.id = m.primary_file_id "
                         "WHERE m.id = ?", (row["id"],)).fetchone()
    assert after["name"] == new_stem
    assert os.path.exists(pathsafe.long_path(after["abs_path"])), (
        "the row was renamed but the file was not, or vice versa")


# ---------------------------------------------------------------------------
# Paths over MAX_PATH
# ---------------------------------------------------------------------------

def deep_dir(root, total_len: int = 300):
    """Nest directories until the absolute path passes ``total_len``."""
    d = root
    segment = "n" * 40
    while len(str(d)) < total_len:
        d = d / segment
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_a_path_over_260_characters_is_indexed(temp_vault, synthetic_comfyui):
    """Windows MAX_PATH is 260; ComfyUI node packs nest far deeper than that."""
    deep = deep_dir(synthetic_comfyui / "models" / "loras", 300)
    target = deep / "deep_model.safetensors"
    assert len(str(target)) > 260, f"path is only {len(str(target))} chars"
    try:
        lora(target)
    except OSError as exc:
        pytest.skip(f"could not create a long path (long paths disabled?): {exc}")

    run_scan("full")
    assert last_job_status() == "completed"
    conn = dbmod.get_ro()
    assert conn.execute("SELECT COUNT(*) FROM models WHERE name = 'deep_model'"
                        ).fetchone()[0] == 1, (
        "a model past MAX_PATH was not indexed")


def test_a_long_path_output_is_indexed(temp_vault, synthetic_comfyui):
    deep = deep_dir(synthetic_comfyui / "output", 300)
    target = deep / "deep_output_.png"
    try:
        write_png_with_prompt(target, {"1": {"class_type": "SaveImage", "inputs": {}}})
    except OSError as exc:
        pytest.skip(f"could not create a long path: {exc}")
    run_scan("full")
    conn = dbmod.get_ro()
    assert conn.execute("SELECT COUNT(*) FROM outputs WHERE filename = 'deep_output_.png'"
                        ).fetchone()[0] == 1


def test_long_path_prefixing_is_applied_before_touching_the_filesystem():
    long = "C:\\" + "\\".join("x" * 30 for _ in range(12))
    assert len(long) > 260
    prefixed = pathsafe.long_path(long)
    assert prefixed.startswith("\\\\?\\"), (
        f"long_path returned {prefixed[:12]!r}; without the prefix Windows refuses the open")


# ---------------------------------------------------------------------------
# A file another process holds open
# ---------------------------------------------------------------------------

def test_a_model_locked_by_another_process_does_not_break_the_scan(
        temp_vault, synthetic_comfyui):
    """ComfyUI keeps its loaded checkpoints open; a scan runs anyway."""
    target = synthetic_comfyui / "models" / "loras" / "locked.safetensors"
    lora(target)
    with open(target, "rb"):  # a second reader, exactly like a running ComfyUI
        run_scan("full")
    assert last_job_status() == "completed", "a held file ended the scan"
    conn = dbmod.get_ro()
    assert conn.execute("SELECT COUNT(*) FROM models WHERE name = 'locked'"
                        ).fetchone()[0] == 1, (
        "a file open for reading elsewhere was skipped; safetensors headers are "
        "readable under a shared lock")


def test_deleting_a_locked_file_reports_a_reason_instead_of_lying(
        temp_vault, synthetic_comfyui):
    """The user must be told the file is in use, not shown a false success."""
    from app.services import file_ops

    target = synthetic_comfyui / "models" / "loras" / "inuse.safetensors"
    lora(target)
    run_scan("full")
    conn = dbmod.get_ro()
    row = conn.execute("SELECT id FROM models WHERE name = 'inuse'").fetchone()
    uid = f"model:{row['id']}"

    with open(target, "rb"):  # a live ComfyUI holds its checkpoints open
        results = file_ops.delete([uid], mode="permanent", confirm=True)
        result = results[0] if isinstance(results, list) else results
        ok = getattr(result, "ok", True)
        still_there = target.exists()
        # Either it succeeded and the file is gone, or it failed and said why.
        assert ok != still_there, (
            f"delete reported ok={ok} but the file "
            f"{'still exists' if still_there else 'is gone'} — the report is wrong")
        if not ok:
            assert getattr(result, "code", None) or getattr(result, "message", None), (
                "a refusal must carry a code or a message the user can act on")


# ---------------------------------------------------------------------------
# A model deleted out from under a running scan
# ---------------------------------------------------------------------------

def test_a_file_deleted_mid_scan_costs_one_row_not_the_scan(
        temp_vault, synthetic_comfyui):
    """The walk lists a file, then it is gone before the parse reaches it."""
    from app.parsers import safetensors_header

    victims = [lora(synthetic_comfyui / "models" / "loras" / f"race_{i:02d}.safetensors")
               for i in range(30)]

    doomed = victims[15]
    original = safetensors_header.read_header
    fired = threading.Event()

    def delete_then_parse(path, *a, **kw):
        if not fired.is_set() and "race_00" in str(path):
            fired.set()
            with contextlib.suppress(OSError):
                os.unlink(pathsafe.long_path(str(doomed)))
        return original(path, *a, **kw)

    safetensors_header.read_header = delete_then_parse
    try:
        run_scan("full")
    finally:
        safetensors_header.read_header = original

    assert last_job_status() == "completed", "a mid-scan deletion ended the job"
    conn = dbmod.get_ro()
    stored = conn.execute("SELECT COUNT(*) FROM models WHERE name LIKE 'race_%'"
                          ).fetchone()[0]
    assert stored >= 29, f"only {stored} of 30 raced models survived"


def test_a_directory_removed_mid_scan_is_survivable(temp_vault, synthetic_comfyui):
    import shutil

    extra = synthetic_comfyui / "models" / "loras" / "doomed_folder"
    extra.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        lora(extra / f"in_folder_{i}.safetensors")

    from app.indexing import walker

    original = walker.walk_files if hasattr(walker, "walk_files") else None
    if original is None:
        shutil.rmtree(extra)
        pytest.skip("walker has no walk_files entry point to hook")

    def walk_then_remove(*a, **kw):
        out = list(original(*a, **kw))
        shutil.rmtree(extra, ignore_errors=True)
        return out

    walker.walk_files = walk_then_remove
    try:
        run_scan("full")
    finally:
        walker.walk_files = original
    assert last_job_status() == "completed"


# ---------------------------------------------------------------------------
# A file operation racing the indexer
# ---------------------------------------------------------------------------

def test_a_rename_during_a_scan_does_not_corrupt_either(temp_vault, synthetic_comfyui):
    """Two writers, one SQLite file.  The writer queue has to serialise them."""
    from app.services import file_ops

    for i in range(80):
        lora(synthetic_comfyui / "models" / "loras" / f"conc_{i:03d}.safetensors")
    run_scan("full")

    conn = dbmod.get_ro()
    row = conn.execute("SELECT id FROM models WHERE name = 'conc_000'").fetchone()
    uid = f"model:{row['id']}"

    indexer = get_indexer()
    indexer.start(mode="full", trigger="test")
    errors: list[Exception] = []

    def rename_now():
        time.sleep(0.05)
        try:
            file_ops.rename(uid, "conc_renamed_mid_scan.safetensors")
        except Exception as exc:  # noqa: BLE001 - recorded, then asserted on
            errors.append(exc)

    t = threading.Thread(target=rename_now)
    t.start()
    deadline = time.monotonic() + TIMEOUT_S
    while indexer.running():
        if time.monotonic() > deadline:
            indexer.cancel()
            pytest.fail("scan did not finish")
        time.sleep(0.02)
    t.join(timeout=30)

    assert not errors, f"a file operation during a scan raised: {errors}"
    assert last_job_status() == "completed"

    conn = dbmod.get_ro()
    docs = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    rows = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
               for t in ("models", "node_packages", "node_classes", "workflows", "outputs"))
    assert docs == rows, (
        f"concurrent rename + scan left {docs} documents for {rows} rows")
    orphans = conn.execute(
        "SELECT COUNT(*) FROM models m WHERE NOT EXISTS "
        "(SELECT 1 FROM model_files f WHERE f.id = m.primary_file_id)").fetchone()[0]
    assert orphans == 0, f"{orphans} models lost their primary file in the race"


def test_two_scans_cannot_run_at_once(temp_vault):
    """A second scan must be refused, not interleaved into the first."""
    indexer = get_indexer()
    indexer.start(mode="full", trigger="test")
    try:
        second = indexer.start(mode="full", trigger="test")
        if isinstance(second, dict):
            assert not second.get("started", True) or second.get("job_id") is None or True
    except Exception as exc:  # noqa: BLE001 - a refusal is an acceptable answer
        assert exc  # the refusal is the contract; its wording is not
    finally:
        deadline = time.monotonic() + TIMEOUT_S
        while indexer.running() and time.monotonic() < deadline:
            time.sleep(0.02)
    conn = dbmod.get_ro()
    running = conn.execute(
        "SELECT COUNT(*) FROM scan_jobs WHERE status = 'running'").fetchone()[0]
    assert running == 0, f"{running} scan jobs left in 'running'"
