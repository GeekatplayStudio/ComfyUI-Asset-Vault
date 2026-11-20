"""Pure helpers behind template indexing, and migration v10's targeted re-read."""

from __future__ import annotations

import sqlite3

import pytest

from app.parsers import official_templates as ot
from app.parsers import workflow_origin


@pytest.mark.parametrize("name, expected", [
    ("index.json", True), ("index.fr.json", True), ("index.mcp.json", True),
    ("INDEX.JSON", True), ("index_probe.json", False), ("image_index.json", False),
    ("flux_dev.json", False),
])
def test_index_files_are_recognised(name, expected):
    assert ot.is_index_file(name) is expected


def test_graph_uuid_accepts_only_id_shaped_values():
    assert ot.graph_uuid({"id": "0C9F6D2E-1111-4222-8333-944455556666"}) == \
        "0c9f6d2e-1111-4222-8333-944455556666"
    assert ot.graph_uuid({"id": "not a uuid; drop table"}) is None
    assert ot.graph_uuid({"id": 42}) is None
    assert ot.graph_uuid(["id"]) is None


def test_declared_models_walks_subgraphs_dedupes_and_drops_insecure_urls():
    graph = {"nodes": [
        {"type": "A", "properties": {"models": [
            {"name": "m.safetensors", "directory": "loras", "url": "https://h/m"},
            {"name": "m.safetensors", "directory": "loras", "url": "https://h/m"}]}}],
        "definitions": {"subgraphs": [{"nodes": [
            {"type": "B", "properties": {"models": [
                {"name": "n.gguf", "directory": "unet", "url": "file:///etc/passwd"}]}}]}]}}
    out = ot.declared_models(graph)
    assert [(m["name"], m["url"], m["via_class"]) for m in out] == [
        ("m.safetensors", "https://h/m", "A"), ("n.gguf", None, "B")]


def test_parse_index_reads_categories_and_tolerates_junk():
    meta = ot.parse_index([
        {"title": "Video", "category": "Foundation", "templates": [
            {"name": "v1", "title": "V1", "openSource": False, "size": 5, "tags": ["a", 3]},
            "junk", {"title": "no name"}]},
        "junk"])
    assert list(meta) == ["v1"]
    v = meta["v1"]
    assert (v.category, v.category_group, v.open_source, v.required_size, v.tags) == \
        ("Video", "Foundation", False, 5, ["a"])
    assert ot.parse_index({"not": "a list"}) == {}


@pytest.mark.parametrize("path, origin", [
    ("C:/py/Lib/site-packages/comfyui_workflow_templates_json/templates/x.json",
     "official_template"),
    ("/py/lib/python3.12/site-packages/comfyui_workflow_templates/templates/x.json",
     "official_template"),
    ("D:/ComfyUI/user/default/workflows/templates/x.json", "user"),
    ("D:/ComfyUI/custom_nodes/Pack/example_workflows/x.json", "bundled"),
])
def test_origin_classifier_knows_template_paths(path, origin):
    assert workflow_origin.classify(path, path)[0] == origin


def test_migration_v10_only_queues_outputs_that_carry_a_graph():
    from app.core.migrations import m010_official_templates as m010

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE workflows (id INTEGER PRIMARY KEY, origin TEXT, is_runnable INT,
            missing_since INT);
        CREATE TABLE outputs (id INTEGER PRIMARY KEY, parser_version INT,
            metadata_format TEXT, workflow_id INT, missing_since INT, size INT);
        CREATE TABLE models (id INTEGER PRIMARY KEY, missing_since INT, integrity TEXT);
        CREATE TABLE model_files (id INTEGER PRIMARY KEY, missing_since INT, size INT,
            hash_state TEXT);
        CREATE TABLE node_packages (id INTEGER PRIMARY KEY, missing_since INT);
        CREATE TABLE node_classes (id INTEGER PRIMARY KEY);
        CREATE TABLE embeddings (id INTEGER PRIMARY KEY);
        INSERT INTO outputs(id, parser_version, metadata_format) VALUES
            (1, 4, 'comfy_prompt'), (2, 4, 'comfy_workflow'), (3, 4, 'a1111'),
            (4, 4, 'none'), (5, 4, NULL), (6, 3, 'comfy_prompt');
    """)
    m010.up(conn)
    m010.up(conn)  # re-running is a no-op
    got = dict(conn.execute("SELECT id, parser_version FROM outputs").fetchall())
    assert got == {1: 4, 2: 4, 3: 5, 4: 5, 5: 5, 6: 3}
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(outputs)")}
    assert "workflow_uuid" in cols
    assert conn.execute("SELECT templates FROM v_vault_stats").fetchone()[0] == 0
