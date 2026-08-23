"""Trash -> restore round-trip for EVERY trashable kind.

The reported defect was workflow-only, but its class - a trash payload whose key
set does not match the destination table's columns - applies to every kind, so
each one is exercised here.
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
from app.core.errors import ValidationError  # noqa: E402
from app.core.migrations import migrate  # noqa: E402
from app.indexing.service import get_indexer  # noqa: E402
from app.services import file_ops  # noqa: E402
from app.services.queries import albums_query, tags_query  # noqa: E402

TRASHABLE_KINDS = ("model", "workflow", "output")

# (kind, identity column used to prove the row really came back)
KIND_TABLE = {"model": "models", "workflow": "workflows", "output": "outputs"}


def _build_tree(root: Path) -> None:
    """A miniature ComfyUI install with one asset of each trashable kind."""
    (root / "models" / "loras").mkdir(parents=True, exist_ok=True)
    (root / "output").mkdir(parents=True, exist_ok=True)
    (root / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "custom_nodes").mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("# fixture\n", encoding="utf-8")
    (root / "nodes.py").write_text(
        'NODE_CLASS_MAPPINGS = {"FixtureNode": object}\n', encoding="utf-8")

    header = b'{"weight":{"dtype":"F16","shape":[8,8],"data_offsets":[0,128]}}'
    (root / "models" / "loras" / "roundtrip.safetensors").write_bytes(
        len(header).to_bytes(8, "little") + header + b"\x00" * 9000)

    (root / "workflows" / "roundtrip.json").write_text(json.dumps({
        "nodes": [
            {"id": 1, "type": "CheckpointLoaderSimple", "inputs": [],
             "widgets_values": ["roundtrip.safetensors"]},
            {"id": 2, "type": "CLIPTextEncode", "inputs": [],
             "widgets_values": ["a round trip"]},
        ],
        "links": [], "groups": [], "version": 0.4,
    }), encoding="utf-8")

    try:
        from PIL import Image

        img = Image.new("RGB", (32, 32), (90, 70, 40))
        img.save(root / "output" / "roundtrip.png")
    except ImportError:  # pragma: no cover
        (root / "output" / "roundtrip.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


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


def _first_uid(kind: str) -> str | None:
    table = KIND_TABLE[kind]
    row = dbmod.one(dbmod.connect(read_only=True),
                    f"SELECT id FROM {table} ORDER BY id LIMIT 1")  # noqa: S608
    return f"{kind}:{row['id']}" if row else None


def _row(kind: str, row_id: int):
    table = KIND_TABLE[kind]
    return dbmod.one(dbmod.connect(read_only=True),
                     f"SELECT * FROM {table} WHERE id = ?", (row_id,))  # noqa: S608


@pytest.mark.parametrize("kind", TRASHABLE_KINDS)
def test_trash_restore_round_trip(vault, kind):
    uid = _first_uid(kind)
    assert uid is not None, f"fixture produced no {kind} row"
    row_id = int(uid.split(":")[1])

    before = _row(kind, row_id)
    assert before is not None
    path = file_ops.resolve_file(uid)["path"]

    # Attach user metadata so the round trip has something to lose.
    tags_query.assign_tags([uid], add=["roundtrip-tag"])
    album_id = int(albums_query.list_albums().items[0]["id"])
    albums_query.set_album_items(album_id, [uid], mode="add")
    assert "roundtrip-tag" in tags_query.tags_of(uid)

    deleted = file_ops.delete([uid], mode="trash")[0].as_dict()
    assert deleted["ok"], deleted
    assert not os.path.exists(path), "file should have left its original location"
    assert _row(kind, row_id) is None, "row should be gone while trashed"

    entry = file_ops.trash_list()["items"][0]
    restored = file_ops.trash_restore([int(entry["id"])])[0].as_dict()

    assert restored["ok"], restored
    assert restored["uid_restored"] == uid, (
        f"expected {uid}, got {restored['uid_restored']!r}")
    assert os.path.isfile(restored["path"]), "file did not come back to disk"

    after = _row(kind, row_id)
    assert after is not None, f"{kind} row was not rebuilt from payload_json"
    assert after["missing_since"] is None

    # Metadata survives the round trip.
    assert "roundtrip-tag" in tags_query.tags_of(uid)
    members = [r["uid"] for r in dbmod.rows(
        dbmod.connect(read_only=True),
        "SELECT uid FROM album_items WHERE album_id = ?", (album_id,))]
    assert uid in members, "album membership was lost"

    # Identity-independent fields are preserved verbatim.
    for column in ("created_at",):
        if column in set(before.keys()):
            assert after[column] == before[column], f"{column} changed"


@pytest.mark.parametrize("kind", TRASHABLE_KINDS)
def test_payload_shape_matches_destination_table(vault, kind):
    """Every key a trash payload stores must exist on the destination table."""
    uid = _first_uid(kind)
    assert uid is not None
    file_ops.delete([uid], mode="trash")
    entry = file_ops.trash_list()["items"][0]
    row = dbmod.one(dbmod.connect(read_only=True),
                    "SELECT * FROM trash_items WHERE id = ?", (int(entry["id"]),))
    payload = json.loads(row["payload_json"])
    table = KIND_TABLE[kind]
    columns = file_ops._table_columns(table)
    unknown = sorted(set(payload["row"]) - columns - {"id"})
    assert not unknown, f"{kind} payload has keys absent from {table}: {unknown}"
    if kind == "model":
        file_cols = file_ops._table_columns("model_files")
        unknown_f = sorted(set(payload["file_row"] or {}) - file_cols - {"id"})
        assert not unknown_f, f"model file_row has stray keys: {unknown_f}"
    # plan_restore must accept it without raising.
    assert file_ops.plan_restore(row) is not None
    file_ops.trash_restore([int(entry["id"])])


def test_bad_payload_fails_loudly_and_moves_nothing(vault):
    """A payload naming a column the table lacks must raise, not fail silently."""
    uid = _first_uid("workflow")
    file_ops.delete([uid], mode="trash")
    entry = file_ops.trash_list()["items"][0]
    tid = int(entry["id"])

    row = dbmod.one(dbmod.connect(read_only=True),
                    "SELECT * FROM trash_items WHERE id = ?", (tid,))
    payload = json.loads(row["payload_json"])
    payload["row"]["filename"] = "not_a_workflows_column.json"  # the original defect

    def _poison(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE trash_items SET payload_json = ? WHERE id = ?",
                     (json.dumps(payload), tid))
        conn.commit()

    dbmod.writer().run(_poison)
    poisoned = dbmod.one(dbmod.connect(read_only=True),
                         "SELECT * FROM trash_items WHERE id = ?", (tid,))

    with pytest.raises(ValidationError) as excinfo:
        file_ops.plan_restore(poisoned)
    assert "filename" in str(excinfo.value)

    result = file_ops.trash_restore([tid])[0].as_dict()
    assert not result["ok"]
    assert "filename" in result["error"]["message"]
    assert os.path.isfile(entry["trash_path"]), "file moved despite a failed restore"

    # Repair and confirm a clean restore still works afterwards.
    payload["row"].pop("filename")

    def _repair(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE trash_items SET payload_json = ? WHERE id = ?",
                     (json.dumps(payload), tid))
        conn.commit()

    dbmod.writer().run(_repair)
    ok = file_ops.trash_restore([tid])[0].as_dict()
    assert ok["ok"] and ok["uid_restored"] == uid


def test_identity_fields_exist_on_every_table():
    """Guards the code-bug half: generated identity columns must really exist."""
    for kind, table in (("model", "models"), ("model_file", "model_files"),
                        ("workflow", "workflows"), ("output", "outputs")):
        columns = file_ops._table_columns(table)
        assert columns, f"no columns discovered for {table}"
        fields = file_ops._identity_fields(kind, __file__, str(BACKEND))
        stray = sorted(set(fields) - columns)
        assert not stray, f"{kind} identity fields absent from {table}: {stray}"
