"""C10 - Storage & Maintenance: ranking, totals, and the cleanup round trip.

The cleanup tests run against a synthetic install with throwaway files.  They
must never be pointed at a real ComfyUI root: the whole point of this suite is to
prove that the delete path works, and it does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import DB_PATH  # noqa: E402
from app.core import config_service  # noqa: E402
from app.core import db as dbmod  # noqa: E402
from app.core.errors import ValidationError  # noqa: E402
from app.core.migrations import migrate  # noqa: E402
from app.services import file_ops, storage_service  # noqa: E402
from app.services.queries import storage_query  # noqa: E402

MB = 1024 * 1024


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


@pytest.fixture
def install(tmp_path):
    """A synthetic ComfyUI root with three models and one output, indexed by hand."""
    db = tmp_path / "storage.db"
    original = dbmod.db_path()
    dbmod.set_db_path(db)
    migrate()
    config_service.invalidate()
    storage_service.invalidate()

    root = tmp_path / "comfy"
    for rel in ("models/checkpoints", "models/loras", "output", "custom_nodes",
                "input"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("# ComfyUI\n", encoding="utf-8")
    (root / "comfyui_version.py").write_text('__version__ = "1.2.3"\n',
                                             encoding="utf-8")

    files = {
        "keep": root / "models" / "checkpoints" / "keep.safetensors",
        "unused": root / "models" / "checkpoints" / "unused.safetensors",
        "dupe_a": root / "models" / "loras" / "twin.safetensors",
        "output": root / "output" / "render.png",
    }
    _write(files["keep"], 3 * MB)
    _write(files["unused"], 8 * MB)
    _write(files["dupe_a"], 5 * MB)
    _write(files["output"], 1 * MB)

    config_service.set_config({"comfyui_path": str(root), "is_configured": True})
    now = dbmod.now_ms()

    def _seed(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO roots(kind,path,path_key,label,is_default,source,available,"
            "created_at) VALUES ('comfyui',?,?,'ComfyUI',1,'config',1,?)",
            (str(root), os.path.normcase(str(root)), now))
        root_id = conn.execute("SELECT id FROM roots").fetchone()["id"]

        def add_model(key, name, category, size, workflows, outputs, favorite=0):
            conn.execute(
                "INSERT INTO models(name,category,total_size,workflow_count,"
                "output_count,favorite,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (name, category, size, workflows, outputs, favorite, now, now))
            model_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            path = files[key]
            conn.execute(
                "INSERT INTO model_files(model_id,root_id,abs_path,path_key,rel_path,"
                "folder,filename,stem,ext,size,mtime_ns,fingerprint,format,"
                "first_seen_at,last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (model_id, root_id, str(path), os.path.normcase(str(path)),
                 str(path.relative_to(root)).replace("\\", "/"),
                 category, path.name, path.stem, path.suffix, size,
                 now * 1_000_000, "fp-" + name, "safetensors", now, now))
            file_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            conn.execute("UPDATE models SET primary_file_id = ? WHERE id = ?",
                         (file_id, model_id))
            return model_id

        ids = {
            "keep": add_model("keep", "keep", "checkpoints", 3 * MB, 4, 12),
            "unused": add_model("unused", "unused", "checkpoints", 8 * MB, 0, 0),
            "dupe": add_model("dupe_a", "twin", "loras", 5 * MB, 0, 0),
        }
        out = files["output"]
        conn.execute(
            "INSERT INTO outputs(root_id,abs_path,path_key,rel_path,folder,filename,"
            "ext,media_kind,size,mtime_ns,created_at_file,fingerprint,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,'image',?,?,?,?,?,?)",
            (root_id, str(out), os.path.normcase(str(out)), "output/render.png",
             "output", "render.png", ".png", MB, now * 1_000_000, now, "fp-out",
             now, now))
        ids["output"] = conn.execute(
            "SELECT last_insert_rowid() AS i").fetchone()["i"]
        conn.commit()
        return ids

    ids = dbmod.writer().run(_seed)
    storage_query.invalidate_signals()
    yield {"root": root, "files": files, "ids": ids}

    dbmod.shutdown_writer()
    dbmod.close_thread_connections()
    dbmod.set_db_path(original)
    config_service.invalidate()
    storage_service.invalidate()


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def test_unused_models_score_above_referenced_ones(install):
    result = storage_query.candidates({"kind": ["model"]}, sort="reclaim", limit=10)
    by_name = {i["name"]: i for i in result.items}
    assert by_name["unused"]["reclaim_score"] > by_name["keep"]["reclaim_score"]
    codes = {r["code"] for r in by_name["unused"]["reasons"]}
    assert "unused" in codes
    assert "unused" not in {r["code"] for r in by_name["keep"]["reasons"]}


def test_size_and_age_are_first_class_sorts(install):
    """C10.5: the owner asked for both, so both must page correctly on their own."""
    by_size = storage_query.candidates({}, sort="size", limit=10).items
    assert [i["size"] for i in by_size] == sorted(
        (i["size"] for i in by_size), reverse=True)

    by_age = storage_query.candidates({}, sort="age", limit=10).items
    assert [i["age_days"] for i in by_age] == sorted(
        (i["age_days"] for i in by_age), reverse=True)

    by_reclaim = storage_query.candidates({}, sort="reclaim", limit=10).items
    assert [i["reclaim_score"] for i in by_reclaim] == sorted(
        (i["reclaim_score"] for i in by_reclaim), reverse=True)


def test_pagination_is_stable_across_pages(install):
    first = storage_query.candidates({}, sort="size", limit=2, offset=0)
    second = storage_query.candidates({}, sort="size", limit=2, offset=2)
    uids = [i["uid"] for i in first.items] + [i["uid"] for i in second.items]
    assert len(set(uids)) == len(uids), "a scored sort must not repeat rows"
    assert first.page["total"] == second.page["total"]


def test_every_reason_declares_its_confidence(install):
    for item in storage_query.candidates({}, limit=50).items:
        for reason in item["reasons"]:
            assert reason["confidence"] in ("measured", "inferred"), reason
        assert item["confidence"] in ("measured", "inferred")


def test_selection_total_matches_the_index(install):
    ids = install["ids"]
    priced = storage_query.selection_total(
        [f"model:{ids['unused']}", f"model:{ids['dupe']}"])
    assert priced["resolved"] == 2
    assert priced["bytes"] == 13 * MB


# ---------------------------------------------------------------------------
# Footprint and volumes
# ---------------------------------------------------------------------------

def test_footprint_buckets_cover_the_install(install):
    prints = storage_service.footprint(refresh=True)
    keys = {b["key"] for b in prints["buckets"]}
    assert {"models", "outputs", "inputs", "custom_nodes", "cache"} <= keys
    models = next(b for b in prints["buckets"] if b["key"] == "models")
    assert models["bytes"] == 16 * MB
    assert models["indexed_bytes"] == 16 * MB


def test_every_root_reports_its_own_volume(install):
    """C10.1: roots can be on different drives, so each is probed separately."""
    for volume in storage_service.volumes():
        assert volume["mount"]
        assert volume["roots"]
        if volume["available"]:
            assert volume["total_bytes"] and volume["total_bytes"] > 0
            assert 0 <= volume["used_pct"] <= 100


def test_summary_is_compact_and_links_to_detail(install):
    """C11: the summary answers the headline question, detail is paged."""
    summary = storage_service.summary(refresh=True)
    assert summary["footprint"]["total_bytes"] > 0
    assert summary["detail_endpoints"]["candidates"] == "/api/v1/storage/candidates"
    assert "items" not in summary, "the summary must not carry a full item list"
    unused = next(g for g in summary["reclaim"]["groups"]
                  if g["key"] == "unused_models")
    assert unused["count"] == 2 and unused["confidence"] == "measured"


# ---------------------------------------------------------------------------
# Cleanup rails
# ---------------------------------------------------------------------------

def test_cleanup_refuses_an_empty_selection(install):
    with pytest.raises(ValidationError):
        storage_service.cleanup([])


def test_cleanup_refuses_permanent_without_confirm(install):
    ids = install["ids"]
    with pytest.raises(ValidationError):
        storage_service.cleanup([f"model:{ids['unused']}"], mode="permanent",
                                confirm=False)
    assert install["files"]["unused"].exists(), "the file must still be there"


def test_cleanup_refuses_kinds_it_does_not_own(install):
    with pytest.raises(ValidationError):
        storage_service.cleanup(["workflow:1"])


def test_cleanup_trashes_and_restores(install):
    """The default is recoverable, and recovery actually works (C10.4)."""
    ids = install["ids"]
    target = install["files"]["unused"]
    assert target.exists()

    result = storage_service.cleanup([f"model:{ids['unused']}"])
    assert result["ok"] and result["deleted"] == 1
    assert result["mode"] == "trash" and result["recoverable"]
    assert result["freed_bytes"] == 8 * MB
    assert result["estimated_bytes"] == 8 * MB
    assert not target.exists(), "the file should have moved to the trash"
    assert result["trash_ids"], "a trashed item must be restorable by id"

    listed = file_ops.trash_list()
    assert listed["page"]["total"] == 1

    restored = file_ops.trash_restore(result["trash_ids"])
    assert all(r.ok for r in restored), [r.as_dict() for r in restored]
    assert target.exists(), "restore must put the file back"


def test_cleanup_reports_only_what_actually_went(install):
    """A partial failure must not claim the whole estimate was freed."""
    ids = install["ids"]
    result = storage_service.cleanup(
        [f"model:{ids['dupe']}", "model:999999"])
    assert result["deleted"] == 1
    assert result["failed"] == 1
    assert result["freed_bytes"] == 5 * MB
    assert result["estimated_bytes"] == 5 * MB


def test_trash_footprint_is_reported(install):
    ids = install["ids"]
    storage_service.cleanup([f"model:{ids['unused']}"])
    trash = storage_service.trash_footprint()
    assert trash["count"] == 1
    assert trash["bytes"] == 8 * MB
    assert trash["bytes_on_disk"] >= 8 * MB
    assert trash["endpoint"] == "/api/v1/fileops/trash"


# ---------------------------------------------------------------------------
# Version-key heuristics: the part most likely to produce a wrong suggestion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("stem", "expected"), [
    ("flux_dev_v2", ("flux_dev", (2,))),
    ("flux_dev-v2.1", ("flux_dev", (2, 1))),
    ("model v3", ("model", (3,))),
    # Precision and quantisation suffixes are variants, not versions: calling
    # fp8 "superseded by" fp16 would suggest deleting a deliberate choice.
    ("model_fp8", None),
    ("model_bf16", None),
    ("model_q4_k_m", None),
    ("plain_model", None),
])
def test_version_key_never_mistakes_a_variant_for_a_version(stem, expected):
    assert storage_query._version_key(stem) == expected


def test_live_vault_summary_renders():
    """Smoke test against the owner's real index when one is present."""
    if not Path(DB_PATH).exists():
        pytest.skip("no indexed vault.db; run a scan first")
    summary = storage_service.summary()
    assert summary["footprint"]["total_bytes"] >= 0
    assert isinstance(summary["volumes"], list)
