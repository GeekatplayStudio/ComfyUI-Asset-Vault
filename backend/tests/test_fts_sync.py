"""FTS must stay in lockstep with the row on EVERY mutation path.

Search sync used to be per-call-site, so rename/move/restore silently diverged.
Each mutation kind is asserted here twice: the new term matches, and the stale
term no longer does.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import DB_PATH  # noqa: E402
from app.core import config_service  # noqa: E402
from app.core import db as dbmod  # noqa: E402
from app.core.migrations import migrate  # noqa: E402
from app.indexing.service import get_indexer  # noqa: E402
from app.search import fts, sync  # noqa: E402
from app.services import file_ops  # noqa: E402
from app.services.queries import albums_query, tags_query  # noqa: E402


def _build_tree(root: Path) -> None:
    (root / "models" / "loras").mkdir(parents=True, exist_ok=True)
    (root / "output").mkdir(parents=True, exist_ok=True)
    (root / "workflows").mkdir(parents=True, exist_ok=True)
    pkg = root / "custom_nodes" / "SyncProbePack"
    pkg.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("# fixture\n", encoding="utf-8")
    (root / "nodes.py").write_text(
        'NODE_CLASS_MAPPINGS = {"FixtureNode": object}\n', encoding="utf-8")

    header = b'{"weight":{"dtype":"F16","shape":[8,8],"data_offsets":[0,128]}}'
    (root / "models" / "loras" / "alphaterm.safetensors").write_bytes(
        len(header).to_bytes(8, "little") + header + b"\x00" * 9000)

    (root / "workflows" / "alphaflow.json").write_text(json.dumps({
        "nodes": [{"id": 1, "type": "CLIPTextEncode", "inputs": [],
                   "widgets_values": ["sync probe"]}],
        "links": [], "groups": [], "version": 0.4,
    }), encoding="utf-8")

    (pkg / "__init__.py").write_text(
        'NODE_CLASS_MAPPINGS = {"SyncProbeNode": object}\n'
        'NODE_DISPLAY_NAME_MAPPINGS = {"SyncProbeNode": "Sync Probe Node"}\n',
        encoding="utf-8")
    (pkg / "pyproject.toml").write_text(
        '[project]\nname = "syncprobepack"\ndescription = "probe"\n', encoding="utf-8")

    try:
        from PIL import Image

        Image.new("RGB", (24, 24), (40, 60, 90)).save(root / "output" / "alphashot.png")
    except ImportError:  # pragma: no cover
        (root / "output" / "alphashot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


@pytest.fixture(scope="module")
def vault(tmp_path_factory):
    root = tmp_path_factory.mktemp("comfy")
    _build_tree(root)
    db = tmp_path_factory.mktemp("db") / "vault.db"
    dbmod.set_db_path(db)
    migrate()
    config_service.set_config({"comfyui_path": str(root), "is_configured": True})
    albums_query.ensure_system_albums()
    ix = get_indexer()
    ix.start(mode="full", trigger="test")
    deadline = time.time() + 120
    while ix.running() and time.time() < deadline:
        time.sleep(0.02)
    yield root
    dbmod.shutdown_writer()
    dbmod.set_db_path(DB_PATH)
    config_service.invalidate()


def _hits(term: str) -> list[str]:
    conn = dbmod.connect(read_only=True)
    return [uid for uid, _kind, _score in fts.search(conn, term, limit=50)]


def _doc_matches_row(uid: str) -> bool:
    """The stored document must equal the one the live row would produce."""
    conn = dbmod.connect(read_only=True)
    expected = sync.doc_for(conn, uid)
    stored = conn.execute(
        "SELECT title, subtitle, body, tags FROM search_fts WHERE uid = ?", (uid,)
    ).fetchone()
    if expected is None:
        return stored is None
    if stored is None:
        return False
    return (stored["title"], stored["subtitle"], stored["body"], stored["tags"]) == (
        expected.title, expected.subtitle, expected.body, expected.tags)


def _uid(kind: str, table: str) -> str:
    row = dbmod.one(dbmod.connect(read_only=True),
                    f"SELECT id FROM {table} ORDER BY id LIMIT 1")  # noqa: S608
    assert row is not None, f"fixture produced no {kind}"
    return f"{kind}:{row['id']}"


# ---------------------------------------------------------------- rename
@pytest.mark.parametrize(("kind", "table", "new_name"), [
    ("model", "models", "renamedterm.safetensors"),
    ("workflow", "workflows", "renamedflow.json"),
    ("output", "outputs", "renamedshot.png"),
])
def test_rename_syncs_fts(vault, kind, table, new_name):
    uid = _uid(kind, table)
    old_term = dbmod.one(dbmod.connect(read_only=True),
                         "SELECT title FROM search_fts WHERE uid = ?", (uid,))["title"]
    assert uid in _hits(old_term), "precondition: old name should match"

    result = file_ops.rename(uid, new_name).as_dict()
    assert result["ok"], result

    stem = os.path.splitext(new_name)[0]
    assert uid in _hits(stem), "renamed asset is not searchable by its new name"
    assert uid not in _hits(old_term), "stale name still matches after rename"
    assert _doc_matches_row(uid)


# ---------------------------------------------------------------- move
def test_move_syncs_fts(vault):
    uid = _uid("output", "outputs")
    root_id = int(dbmod.one(dbmod.connect(read_only=True),
                            "SELECT root_id FROM outputs LIMIT 1")["root_id"])
    file_ops.create_folder(root_id, "output/moved")
    res = file_ops.move([uid], root_id, "output/moved")[0].as_dict()
    assert res["ok"], res
    assert _doc_matches_row(uid)
    assert uid in _hits("moved")


# ---------------------------------------------------------------- trash cycle
@pytest.mark.parametrize(("kind", "table"), [
    ("model", "models"), ("workflow", "workflows"),
    ("output", "outputs"), ("node_package", "node_packages"),
])
def test_trash_and_restore_sync_fts(vault, kind, table):
    uid = _uid(kind, table)
    term = dbmod.one(dbmod.connect(read_only=True),
                     "SELECT title FROM search_fts WHERE uid = ?", (uid,))["title"]
    assert uid in _hits(term)

    assert file_ops.delete([uid], mode="trash")[0].as_dict()["ok"]
    assert uid not in _hits(term), "trashed asset still matches in search"
    assert _doc_matches_row(uid), "document should be gone with the row"

    entry = file_ops.trash_list()["items"][0]
    restored = file_ops.trash_restore([int(entry["id"])])[0].as_dict()
    assert restored["ok"], restored
    assert restored["uid_restored"] == uid

    assert uid in _hits(term), "restored asset is missing from search"
    assert _doc_matches_row(uid)


# ---------------------------------------------------------------- metadata
def test_tag_assignment_syncs_fts(vault):
    uid = _uid("model", "models")
    tags_query.assign_tags([uid], add=["zebracrossing"])
    assert uid in _hits("zebracrossing"), "new tag is not searchable"
    assert _doc_matches_row(uid)

    tags_query.assign_tags([uid], remove=["zebracrossing"])
    assert uid not in _hits("zebracrossing"), "removed tag still matches"
    assert _doc_matches_row(uid)


def test_metadata_patch_syncs_fts(vault):
    uid = _uid("workflow", "workflows")
    tags_query.patch_asset(uid, {"description": "quokka telemetry harness"})
    assert uid in _hits("quokka"), "patched description is not searchable"
    assert _doc_matches_row(uid)


def test_album_membership_keeps_doc_consistent(vault):
    uid = _uid("output", "outputs")
    album_id = int(albums_query.list_albums().items[0]["id"])
    albums_query.set_album_items(album_id, [uid], mode="add")
    assert _doc_matches_row(uid)
    albums_query.set_album_items(album_id, [uid], mode="remove")
    assert _doc_matches_row(uid)


def test_permanent_delete_removes_doc(vault):
    uid = _uid("output", "outputs")
    term = dbmod.one(dbmod.connect(read_only=True),
                     "SELECT title FROM search_fts WHERE uid = ?", (uid,))["title"]
    assert file_ops.delete([uid], mode="permanent", confirm=True)[0].as_dict()["ok"]
    assert uid not in _hits(term)
    assert _doc_matches_row(uid)


def test_every_indexed_row_has_a_matching_document(vault):
    """Whole-vault invariant: no row without a doc, no doc without a row."""
    conn = dbmod.connect(read_only=True)
    mismatched = []
    for kind, table in (("model", "models"), ("node_package", "node_packages"),
                        ("node_class", "node_classes"), ("workflow", "workflows"),
                        ("output", "outputs")):
        for row in conn.execute(f"SELECT id FROM {table}"):  # noqa: S608
            uid = f"{kind}:{row['id']}"
            if not _doc_matches_row(uid):
                mismatched.append(uid)
    assert not mismatched, f"documents out of sync with their rows: {mismatched[:8]}"

    orphans = conn.execute(
        "SELECT uid FROM search_docs WHERE uid NOT IN "
        "(SELECT 'model:' || id FROM models UNION ALL "
        " SELECT 'node_package:' || id FROM node_packages UNION ALL "
        " SELECT 'node_class:' || id FROM node_classes UNION ALL "
        " SELECT 'workflow:' || id FROM workflows UNION ALL "
        " SELECT 'output:' || id FROM outputs)"
    ).fetchall()
    assert not orphans, f"documents without rows: {[r['uid'] for r in orphans][:8]}"


# ---------------------------------------------------- node-package policy
def test_node_package_rename_and_move_are_refused_with_a_reason(vault):
    """A withheld capability must say why, not fail as an unknown uid kind."""
    uid = _uid("node_package", "node_packages")

    renamed = file_ops.rename(uid, "RenamedPack").as_dict()
    assert not renamed["ok"]
    assert renamed["error"]["details"]["reason"] == "node_package_immovable"
    assert "folder name" in renamed["error"]["message"]
    assert renamed["error"]["details"]["allowed"] == ["delete"]

    root_id = int(dbmod.one(dbmod.connect(read_only=True),
                            "SELECT id FROM roots WHERE kind='comfyui'")["id"])
    moved = file_ops.move([uid], root_id, "custom_nodes/elsewhere")[0].as_dict()
    assert not moved["ok"]
    assert moved["error"]["details"]["reason"] == "node_package_immovable"


def test_node_package_delete_restores_its_classes(vault):
    uid = _uid("node_package", "node_packages")
    pkg_id = int(uid.split(":")[1])
    conn = dbmod.connect(read_only=True)
    before = [r["node_id"] for r in conn.execute(
        "SELECT node_id FROM node_classes WHERE package_id = ? ORDER BY node_id",
        (pkg_id,))]
    assert before, "fixture package should register at least one class"
    path = dbmod.one(conn, "SELECT abs_path FROM node_packages WHERE id = ?",
                     (pkg_id,))["abs_path"]
    assert os.path.isdir(path)

    res = file_ops.delete([uid], mode="trash")[0].as_dict()
    assert res["ok"], res
    assert not os.path.exists(path), "package directory should have been moved away"

    entry = file_ops.trash_list()["items"][0]
    assert entry["size"] > 0, "a package's trash size must be its recursive size"
    restored = file_ops.trash_restore([int(entry["id"])])[0].as_dict()
    assert restored["ok"], restored
    assert restored["uid_restored"] == uid
    assert os.path.isdir(restored["path"])

    after = [r["node_id"] for r in dbmod.rows(
        dbmod.connect(read_only=True),
        "SELECT node_id FROM node_classes WHERE package_id = ? ORDER BY node_id",
        (pkg_id,))]
    assert after == before, "node classes were lost across the round trip"
    assert _doc_matches_row(uid)


def test_node_package_size_is_recursive(vault):
    """Storage accounting must include .git and web assets, not the pruned walk."""
    conn = dbmod.connect(read_only=True)
    row = dbmod.one(conn, "SELECT abs_path, total_size FROM node_packages "
                          "WHERE is_single_file = 0 LIMIT 1")
    assert row is not None
    true_size = file_ops.directory_size(str(row["abs_path"]))
    assert int(row["total_size"]) == true_size, (
        f"stored {row['total_size']} != true recursive {true_size}")


def test_unsupported_uid_kinds_still_rejected(vault):
    for uid in ("node_class:1", "album:1", "bogus:1"):
        res = file_ops.delete([uid], mode="trash")[0].as_dict()
        assert not res["ok"]
        assert "not supported" in res["error"]["message"]
