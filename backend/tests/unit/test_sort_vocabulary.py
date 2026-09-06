"""Every advertised sort must be executable against the schema it reads.

Regression for the v9 migration: ``models_query.SORTS`` mapped ``created`` onto
``created_at`` while ``v_model_list`` never selected that column, so the Models
tab's "Recently added" sort returned a 500.  Nothing checked that the sort
vocabulary and the view/table it orders agree, so a view rebuild that drops a
column would slip through again.  This test runs each sort key of each scope
through the real query layer on a migrated DB, ascending and descending.
"""

from __future__ import annotations

import os

import pytest

from app.services.queries import (
    models_query,
    nodes_query,
    outputs_query,
    workflows_query,
)


def _seed(conn) -> None:
    now = 1_700_000_000_000
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO roots(kind,path,path_key,label,is_default,source,available,created_at) "
        "VALUES ('comfyui','C:/probe','c:/probe','Probe',1,'config',1,?)", (now,))
    root_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for i, name in enumerate(("alpha", "beta")):
        conn.execute(
            "INSERT INTO models(name,category,created_at,updated_at) "
            "VALUES (?,'checkpoints',?,?)", (name, now + i, now + i))
        model_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO model_files(model_id,root_id,abs_path,path_key,rel_path,folder,"
            "filename,stem,ext,size,mtime_ns,fingerprint,format,first_seen_at,last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (model_id, root_id, f"C:/probe/{name}.safetensors",
             os.path.normcase(f"C:/probe/{name}.safetensors"),
             f"checkpoints/{name}.safetensors", "checkpoints", f"{name}.safetensors",
             name, ".safetensors", 1 + i, now * 1_000_000, f"fp-{name}", "safetensors",
             now, now))
        file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE models SET primary_file_id = ? WHERE id = ?", (file_id, model_id))
    conn.commit()


@pytest.fixture
def seeded_vault(temp_vault):
    from app.core import db as dbmod
    dbmod.writer().run(_seed)
    return temp_vault


def _keys(sorts: dict) -> list[str]:
    # ``relevance`` needs a search query and is validated separately.
    return [k for k in sorts if k != "relevance"]


@pytest.mark.parametrize("key", _keys(models_query.SORTS))
@pytest.mark.parametrize("direction", ["", "-"])
def test_model_sorts_execute(seeded_vault, key, direction):
    result = models_query.list_models(sort=f"{direction}{key}", limit=10)
    assert isinstance(result.items, list)


def test_model_recently_added_orders_by_created_at(seeded_vault):
    newest = models_query.list_models(sort="-created", limit=10).items
    oldest = models_query.list_models(sort="created", limit=10).items
    assert [m["name"] for m in newest] == ["beta", "alpha"]
    assert [m["name"] for m in oldest] == ["alpha", "beta"]


def test_v_model_list_exposes_created_at(seeded_vault):
    from app.core import db as dbmod
    cols = {r[1] for r in dbmod.get_ro().execute("PRAGMA table_info(v_model_list)")}
    assert "created_at" in cols


@pytest.mark.parametrize("key", _keys(nodes_query.PACKAGE_SORTS))
@pytest.mark.parametrize("direction", ["", "-"])
def test_node_package_sorts_execute(seeded_vault, key, direction):
    nodes_query.list_node_packages(sort=f"{direction}{key}", limit=10)


@pytest.mark.parametrize("key", _keys(nodes_query.CLASS_SORTS))
@pytest.mark.parametrize("direction", ["", "-"])
def test_node_class_sorts_execute(seeded_vault, key, direction):
    nodes_query.list_node_classes(sort=f"{direction}{key}", limit=10)


@pytest.mark.parametrize("key", _keys(workflows_query.SORTS))
@pytest.mark.parametrize("direction", ["", "-"])
def test_workflow_sorts_execute(seeded_vault, key, direction):
    workflows_query.list_workflows(sort=f"{direction}{key}", limit=10)


@pytest.mark.parametrize("key", _keys(outputs_query.SORTS))
@pytest.mark.parametrize("direction", ["", "-"])
def test_output_sorts_execute(seeded_vault, key, direction):
    outputs_query.list_outputs(sort=f"{direction}{key}", limit=10)


def test_models_endpoint_recently_added(hermetic_client):
    r = hermetic_client.get("/api/v1/models", params={"sort": "-created", "limit": 5})
    assert r.status_code == 200, r.text
