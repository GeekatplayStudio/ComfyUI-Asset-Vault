"""Aggregates must never outlive the write that changed them.

The defect this guards: `/outputs` `page.total` and `/system/stats` kept serving
pre-cleanup numbers until the process restarted.  It was never a cached
aggregate - it was a **leaked read transaction**.

`services/queries/storage_query` stages its duplicate/supersession signals in a
`CREATE TEMP TABLE` on the thread-local *read-only* connection.  That is legal,
but Python's legacy transaction control issues an implicit `BEGIN` before the
INSERT and never commits it, so the connection sat inside a transaction and WAL
pinned its read snapshot.  Every later SELECT on that worker thread - list
totals, facet counts, album counts, the left rail, `v_vault_stats` - then served
data frozen at the instant the Storage tab was first opened.

The owner deletes 40 GB and the app insists the space is still occupied.

Two structural guards, both asserted below:
  * `core/db._configure` puts read-only connections in autocommit, so an implicit
    BEGIN cannot happen at all;
  * `core/db.get_ro` rolls back any transaction it finds, so even an explicit one
    cannot outlive the request that opened it.

Derived caches then key on `PRAGMA data_version`, which moves on every committed
write from every path - so no mutation site has to remember they exist.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core import config_service, vault_stats  # noqa: E402
from app.core import db as dbmod  # noqa: E402
from app.core.migrations import migrate  # noqa: E402
from app.services import file_ops, storage_service  # noqa: E402
from app.services.queries import (  # noqa: E402
    albums_query,
    models_query,
    outputs_query,
    storage_query,
    tags_query,
)

MB = 1024 * 1024


@pytest.fixture
def vault(tmp_path):
    """A synthetic install with disposable probe files.  Never the real vault."""
    db = tmp_path / "freshness.db"
    original = dbmod.db_path()
    dbmod.set_db_path(db)
    migrate()
    config_service.invalidate()
    storage_service.invalidate()

    root = tmp_path / "comfy"
    for rel in ("models/checkpoints", "output", "custom_nodes", "input"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("# ComfyUI\n", encoding="utf-8")

    probes: dict[str, Path] = {}
    for name, size in (("probe_a", 4 * MB), ("probe_b", 6 * MB)):
        path = root / "models" / "checkpoints" / f"{name}.safetensors"
        path.write_bytes(b"\0" * size)
        probes[name] = path
    shot = root / "output" / "probe_shot.png"
    shot.write_bytes(b"\0" * MB)
    probes["probe_shot"] = shot

    config_service.set_config({"comfyui_path": str(root), "is_configured": True})
    now = dbmod.now_ms()

    def _seed(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO roots(kind,path,path_key,label,is_default,source,available,"
            "created_at) VALUES ('comfyui',?,?,'ComfyUI',1,'config',1,?)",
            (str(root), os.path.normcase(str(root)), now))
        root_id = conn.execute("SELECT id FROM roots").fetchone()["id"]
        ids = {}
        for name in ("probe_a", "probe_b"):
            path = probes[name]
            size = path.stat().st_size
            conn.execute(
                "INSERT INTO models(name,category,total_size,workflow_count,"
                "output_count,created_at,updated_at) VALUES (?,'checkpoints',?,0,0,?,?)",
                (name, size, now, now))
            model_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            conn.execute(
                "INSERT INTO model_files(model_id,root_id,abs_path,path_key,rel_path,"
                "folder,filename,stem,ext,size,mtime_ns,fingerprint,format,"
                "first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (model_id, root_id, str(path), os.path.normcase(str(path)),
                 f"checkpoints/{path.name}", "checkpoints", path.name, path.stem,
                 path.suffix, size, now * 1_000_000, "fp-" + name, "safetensors",
                 now, now))
            file_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            conn.execute("UPDATE models SET primary_file_id=? WHERE id=?",
                         (file_id, model_id))
            ids[name] = model_id
        conn.execute(
            "INSERT INTO outputs(root_id,abs_path,path_key,rel_path,folder,filename,"
            "ext,media_kind,size,mtime_ns,created_at_file,fingerprint,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,'.png','image',?,?,?,?,?,?)",
            (root_id, str(shot), os.path.normcase(str(shot)), "output/probe_shot.png",
             "output", "probe_shot.png", shot.stat().st_size, now * 1_000_000, now,
             "fp-shot", now, now))
        ids["probe_shot"] = conn.execute(
            "SELECT last_insert_rowid() AS i").fetchone()["i"]
        conn.commit()
        return ids

    ids = dbmod.writer().run(_seed)
    albums_query.ensure_system_albums()
    yield {"root": root, "files": probes, "ids": ids}

    dbmod.shutdown_writer()
    dbmod.close_thread_connections()
    dbmod.set_db_path(original)
    config_service.invalidate()
    storage_service.invalidate()


def _aggregates() -> dict:
    """Everything the report named, read exactly the way a request reads it."""
    stats = vault_stats()
    return {
        "stats_models": int(stats["models"]),
        "stats_outputs": int(stats["outputs"]),
        "stats_model_bytes": int(stats["models_bytes"]),
        "outputs_total": int(outputs_query.list_outputs({}, limit=1).page["total"]),
        "models_total": int(models_query.list_models({}, limit=1).page["total"]),
        "model_facet_total": sum(
            int(f["count"]) for f in models_query.model_facets({})["category"]),
        "album_items": sum(int(a["item_count"])
                           for a in albums_query.list_albums().items),
        "tree_count": sum(int(n["count"])
                          for n in models_query.model_tree({})["nodes"]),
        "storage_candidates": int(
            storage_query.candidates({}, limit=1).page["total"]),
        "storage_unused": next(
            g["count"] for g in storage_query.reclaimable_summary()["groups"]
            if g["key"] == "unused_models"),
        "footprint_models": next(
            b["bytes"] for b in storage_service.footprint()["buckets"]
            if b["key"] == "models"),
    }


# ---------------------------------------------------------------------------
# The structural guards
# ---------------------------------------------------------------------------

def test_read_only_connections_are_autocommit(vault):
    conn = dbmod.get_ro()
    assert conn.isolation_level is None, (
        "a read-only connection in legacy transaction mode will pin a WAL read "
        "snapshot the first time anything writes to a TEMP table")


def test_staging_signals_leaves_no_open_transaction(vault):
    """The exact call that leaked the snapshot."""
    conn = dbmod.get_ro()
    storage_query.candidates({}, limit=1)
    assert not conn.in_transaction, (
        "storage signal staging left the reader inside a transaction - every "
        "later aggregate on this thread would serve pre-mutation data")


def test_get_ro_heals_a_transaction_left_open_by_anyone(vault):
    conn = dbmod.get_ro()
    conn.execute("BEGIN")
    assert conn.in_transaction
    assert not dbmod.get_ro().in_transaction, (
        "get_ro must not hand back a reader that is holding a stale snapshot")


def test_data_version_moves_on_every_committed_write(vault):
    before = dbmod.data_version()

    def _touch(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE models SET updated_at = updated_at + 1")
        conn.commit()

    dbmod.writer().run(_touch)
    assert dbmod.data_version() != before


# ---------------------------------------------------------------------------
# The regression the report asked for: no restart, aggregates move
# ---------------------------------------------------------------------------

def test_cleanup_moves_every_aggregate_in_the_same_process(vault):
    ids = vault["ids"]
    # Open the Storage tab first - this is what used to freeze the reader.
    storage_service.summary(refresh=True)
    before = _aggregates()
    assert before["stats_models"] == 2
    assert before["outputs_total"] == 1

    result = storage_service.cleanup([f"model:{ids['probe_a']}"])
    assert result["deleted"] == 1 and result["freed_bytes"] == 4 * MB

    after = _aggregates()
    assert after["stats_models"] == before["stats_models"] - 1
    assert after["models_total"] == before["models_total"] - 1
    assert after["stats_model_bytes"] == before["stats_model_bytes"] - 4 * MB
    assert after["model_facet_total"] == before["model_facet_total"] - 1
    assert after["tree_count"] == before["tree_count"] - 1
    assert after["storage_candidates"] == before["storage_candidates"] - 1
    assert after["storage_unused"] == before["storage_unused"] - 1
    assert after["footprint_models"] == before["footprint_models"] - 4 * MB


def test_output_cleanup_moves_output_aggregates(vault):
    ids = vault["ids"]
    storage_service.summary(refresh=True)
    before = _aggregates()

    storage_service.cleanup([f"output:{ids['probe_shot']}"])

    after = _aggregates()
    assert after["stats_outputs"] == before["stats_outputs"] - 1
    assert after["outputs_total"] == before["outputs_total"] - 1


@pytest.mark.parametrize("mutation", ["rename", "move", "trash", "restore",
                                      "permanent", "tag", "album"])
def test_every_mutation_path_refreshes_aggregates(vault, mutation):
    """Cleanup is just where it was noticed - all of these share the reader."""
    ids = vault["ids"]
    uid = f"model:{ids['probe_b']}"
    storage_service.summary(refresh=True)   # prime every cache first
    before = _aggregates()

    if mutation == "rename":
        assert file_ops.rename(uid, "probe_b_renamed.safetensors").ok
        after = _aggregates()
        assert after["stats_models"] == before["stats_models"]
        assert models_query.list_models({"q": "probe_b_renamed"},
                                        limit=5).page["total"] == 1
        return

    if mutation == "move":
        (vault["root"] / "models" / "loras").mkdir(parents=True, exist_ok=True)
        results = file_ops.move([uid], 1, "models/loras")
        assert all(r.ok for r in results), [r.as_dict() for r in results]
        after = _aggregates()
        assert after["stats_models"] == before["stats_models"]
        return

    if mutation == "tag":
        tags_query.assign_tags([uid], add=["probe-tag"])
        assert models_query.list_models({"tag": ["probe-tag"]},
                                        limit=5).page["total"] == 1
        return

    if mutation == "album":
        album = albums_query.create_album("Probe album", scope="models",
                                          kind="manual")
        albums_query.set_album_items(int(album["id"]), [uid], mode="add")
        after = _aggregates()
        assert after["album_items"] == before["album_items"] + 1
        return

    if mutation == "permanent":
        assert file_ops.delete([uid], mode="permanent", confirm=True)[0].ok
        after = _aggregates()
        assert after["stats_models"] == before["stats_models"] - 1
        assert after["storage_candidates"] == before["storage_candidates"] - 1
        return

    # trash / restore
    trashed = file_ops.delete([uid], mode="trash")[0]
    assert trashed.ok
    mid = _aggregates()
    assert mid["stats_models"] == before["stats_models"] - 1
    assert mid["storage_candidates"] == before["storage_candidates"] - 1

    if mutation == "restore":
        restored = file_ops.trash_restore([int(trashed.details["trash_id"])])
        assert all(r.ok for r in restored), [r.as_dict() for r in restored]
        after = _aggregates()
        assert after["stats_models"] == before["stats_models"], \
            "a restore must put the count back without a restart"


def test_free_space_is_re_probed_never_cached(vault):
    """C10.4 headroom: the number the whole view exists for."""
    first = storage_service.volumes()
    second = storage_service.volumes()
    assert first and second
    # Distinct dict objects each call: nothing is served from a memo.
    assert first[0] is not second[0]
    for volume in second:
        if volume["available"]:
            assert volume["free_bytes"] is not None


def test_footprint_cache_is_keyed_on_data_version(vault):
    ids = vault["ids"]
    first = storage_service.footprint()
    again = storage_service.footprint()
    assert again is first, "an unchanged database should still hit the cache"

    file_ops.delete([f"model:{ids['probe_a']}"], mode="permanent", confirm=True)
    after = storage_service.footprint()
    assert after is not first, "a committed write must invalidate the footprint"
    models_before = next(b["bytes"] for b in first["buckets"] if b["key"] == "models")
    models_after = next(b["bytes"] for b in after["buckets"] if b["key"] == "models")
    assert models_after == models_before - 4 * MB
