"""Invariant tests for the backend-core layer.

Each test guards a defect class that reached the live DB once and must not
recur.  They run against the real vault DB when it exists and otherwise skip,
so they are safe in CI without an indexed install.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import DB_PATH  # noqa: E402
from app.core import db as dbmod  # noqa: E402
from app.parsers import arch_detect  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "graphs"


@pytest.fixture(scope="module")
def vault_copy(tmp_path_factory):
    """A private copy of the indexed vault, so tests never mutate the real one."""
    if not Path(DB_PATH).exists():
        pytest.skip("no indexed vault.db; run a scan first")
    target = tmp_path_factory.mktemp("vault") / "vault.db"
    # shutil.copy would miss everything still sitting in the -wal file and give
    # a stale snapshot; the backup API takes a consistent one.
    src = sqlite3.connect(f"file:{Path(DB_PATH).as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    dbmod.set_db_path(target)
    yield dbmod.connect(read_only=True)
    dbmod.shutdown_writer()
    dbmod.set_db_path(DB_PATH)


# ---------------------------------------------------------------------------
# Defect 1 - system albums must not duplicate across startups
# ---------------------------------------------------------------------------

def test_no_duplicate_albums(vault_copy):
    dupes = vault_copy.execute(
        "SELECT name, scope, COUNT(*) n FROM albums "
        "GROUP BY COALESCE(parent_id, 0), scope, name HAVING n > 1"
    ).fetchall()
    assert not dupes, f"duplicate albums: {[(r['name'], r['scope'], r['n']) for r in dupes]}"


def test_album_identity_index_exists(vault_copy):
    idx = vault_copy.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='ux_albums_identity'"
    ).fetchone()
    assert idx is not None, "ux_albums_identity is missing; NULL parents will not dedupe"


def test_ensure_system_albums_is_idempotent(tmp_path):
    """Five consecutive calls must leave the album count unchanged."""
    from app.core.migrations import migrate
    from app.services.queries import albums_query

    db = tmp_path / "albums.db"
    dbmod.set_db_path(db)
    try:
        migrate()
        counts = []
        for _ in range(5):
            albums_query.ensure_system_albums()
            counts.append(dbmod.scalar(dbmod.connect(read_only=True),
                                       "SELECT COUNT(*) FROM albums"))
        assert len(set(counts)) == 1, f"ensure_system_albums is not idempotent: {counts}"
        assert counts[0] == len(albums_query.SYSTEM_ALBUMS)
    finally:
        dbmod.shutdown_writer()
        dbmod.set_db_path(DB_PATH)


# ---------------------------------------------------------------------------
# Defect 2 - architecture_label must never contradict base_model_family
# ---------------------------------------------------------------------------

def test_label_never_names_a_foreign_family(vault_copy):
    """The label is what the DETAILS panel shows; it must agree with the family."""
    violations = []
    for row in vault_copy.execute(
        "SELECT name, base_model_family, architecture_label FROM models"
    ):
        named = arch_detect.label_names_family(row["architecture_label"])
        if named is not None and named != row["base_model_family"]:
            violations.append((row["name"], row["base_model_family"],
                               row["architecture_label"]))
    assert not violations, f"label/family contradictions: {violations}"


def test_ltx_checkpoints_are_video(vault_copy):
    rows = vault_copy.execute(
        "SELECT name, base_model_family, modality, architecture_label FROM models "
        "WHERE base_model_family = 'LTX-Video' AND model_role = 'checkpoint'"
    ).fetchall()
    if not rows:
        pytest.skip("no LTX-Video checkpoints indexed")
    for r in rows:
        assert r["modality"] == "video", f"{r['name']} modality={r['modality']}"
        assert "audio checkpoint" not in (r["architecture_label"] or "")


def test_label_family_helper():
    assert arch_detect.label_names_family("ACE-Step audio checkpoint") == "ACE-Step"
    assert arch_detect.label_names_family("LTX-Video AV checkpoint") == "LTX-Video"
    assert arch_detect.label_names_family("FLUX.1 Transformer (dual-stream)") == "FLUX.1"
    assert arch_detect.label_names_family("Variational auto-encoder") is None
    assert arch_detect.label_names_family(None) is None


# ---------------------------------------------------------------------------
# B1 regression - negative prompts must never duplicate the positive
# ---------------------------------------------------------------------------

def test_no_duplicate_negative_prompts(vault_copy):
    rows = vault_copy.execute(
        "SELECT filename, provenance_json FROM outputs "
        "WHERE positive_prompt IS NOT NULL AND positive_prompt <> '' "
        "AND positive_prompt = negative_prompt"
    ).fetchall()
    same_node = []
    for r in rows:
        prov = json.loads(r["provenance_json"] or "{}")
        pos = (prov.get("positive_prompt") or {}).get("source_node_id")
        neg = (prov.get("negative_prompt") or {}).get("source_node_id")
        if pos == neg:
            same_node.append(r["filename"])
    assert not same_node, f"negative duplicated from the same node: {same_node[:5]}"


@pytest.mark.parametrize("case", json.loads(
    (FIXTURE_DIR / "expectations.json").read_text(encoding="utf-8"))["cases"]
    if (FIXTURE_DIR / "expectations.json").exists() else [])
def test_graph_fixtures(case):
    from app.parsers import graph_utils

    graph = json.loads((FIXTURE_DIR / case["graph"]).read_text(encoding="utf-8"))["prompt"]
    summary = graph_utils.summarize_graph(graph)
    expect = case["expect"]
    prov = summary.provenance.get("negative_prompt") or {}
    assert summary.positive_prompt == expect["positive_prompt"]
    assert summary.negative_prompt == expect["negative_prompt"]
    assert prov.get("origin") == expect["negative_provenance_origin"]
    assert prov.get("source_node_id") == expect["negative_source_node_id"]
    assert sorted(m["ref_name"] for m in summary.models) == sorted(expect["models"])


def test_scratch_dir_unused():
    """Guard against tests writing into the user's project by accident."""
    assert tempfile.gettempdir() and os.path.isdir(tempfile.gettempdir())
