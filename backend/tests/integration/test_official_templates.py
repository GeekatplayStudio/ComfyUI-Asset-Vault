"""Official ComfyUI templates, indexed read-only and linked (schema v10).

A synthetic install carries the same layout ComfyUI's distributions use:

    <parent>/python_embeded/python.exe
    <parent>/python_embeded/Lib/site-packages/
        comfyui_workflow_templates_json/templates/<name>.json + index.json (+ index.fr.json)
        comfyui_workflow_templates_core/manifest.json
        comfyui_workflow_templates_media_image/templates/<name>-1.webp

and one full scan has to turn that into a template row whose models resolve,
whose saved copy and rendered image point back at it, and which no file
operation can touch.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from builders import write_png_with_prompt

from app.core import db as dbmod
from app.indexing.service import get_indexer

TIMEOUT_S = 120
TPL_UUID = "0c9f6d2e-1111-4222-8333-944455556666"
TPL_NAME = "image_probe_t2i"
CKPT = "sd15-probe.safetensors"
MISSING = "absent-unet.safetensors"


def run_scan(mode: str = "full") -> dict:
    indexer = get_indexer()
    indexer.start(mode=mode, trigger="test")
    deadline = time.monotonic() + TIMEOUT_S
    while indexer.running():
        if time.monotonic() > deadline:
            indexer.cancel()
            pytest.fail(f"scan did not finish within {TIMEOUT_S}s")
        time.sleep(0.02)
    return indexer.status()


def _template_graph() -> dict:
    """A UI graph with a declared model the walk finds, and one inside a subgraph."""
    return {
        "id": TPL_UUID, "revision": 0, "last_node_id": 3, "last_link_id": 0,
        "nodes": [
            {"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": [CKPT],
             "properties": {"models": [{
                 "name": CKPT, "directory": "checkpoints",
                 "url": "https://huggingface.co/probe/resolve/main/" + CKPT}]}},
            {"id": 2, "type": "KSampler", "widgets_values": [1, "fixed", 20, 7.0]},
            {"id": 3, "type": "b5f1c0de-aaaa-4bbb-8ccc-0123456789ab", "widgets_values": []},
        ],
        "links": [],
        "definitions": {"subgraphs": [{
            "id": "b5f1c0de-aaaa-4bbb-8ccc-0123456789ab", "name": "loader",
            "nodes": [{"id": 9, "type": "UNETLoader", "widgets_values": [],
                       "properties": {"models": [
                           {"name": MISSING, "directory": "diffusion_models",
                            "url": "https://huggingface.co/probe/resolve/main/" + MISSING},
                           {"name": "evil.safetensors", "directory": "loras",
                            "url": "http://insecure.example/evil.safetensors"}]}}]}]},
        "extra": {}, "version": 0.4,
    }


@pytest.fixture
def templated(temp_vault, synthetic_comfyui):
    root = synthetic_comfyui
    home = root.parent / "python_embeded"
    site = home / "Lib" / "site-packages"
    home.mkdir(parents=True, exist_ok=True)
    (home / "python.exe").write_bytes(b"")  # located, never executed
    tpl_dir = site / "comfyui_workflow_templates_json" / "templates"
    tpl_dir.mkdir(parents=True)
    (site / "comfyui_workflow_templates_json-0.1.137.dist-info").mkdir()
    (tpl_dir / f"{TPL_NAME}.json").write_text(json.dumps(_template_graph()), encoding="utf-8")
    index = [{"moduleName": "default", "category": "Foundation", "title": "Image",
              "type": "image", "templates": [{
                  "name": TPL_NAME, "title": "Probe: Text to Image",
                  "description": "A probe template for the test suite.",
                  "mediaType": "image", "mediaSubtype": "webp", "tags": ["Text to Image"],
                  "models": ["SD1.5"], "size": 2_000_000_000, "openSource": True,
                  "minComfyUIVersion": "0.3.0", "date": "2026-01-01",
                  "tutorialUrl": "https://docs.comfy.org/probe"}]}]
    (tpl_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
    (tpl_dir / "index.fr.json").write_text(json.dumps(index), encoding="utf-8")
    core = site / "comfyui_workflow_templates_core"
    core.mkdir(parents=True)
    (core / "manifest.json").write_text(json.dumps({"templates": [
        {"id": TPL_NAME, "bundle": "media-image", "assets": []}]}), encoding="utf-8")
    media = site / "comfyui_workflow_templates_media_image" / "templates"
    media.mkdir(parents=True)
    from PIL import Image
    Image.new("RGB", (8, 8), (40, 90, 160)).save(media / f"{TPL_NAME}-1.webp", "WEBP")

    # The owner saved a copy of the template (same graph id) into their folder.
    copy = root / "user" / "default" / "workflows" / "my-probe-copy.json"
    copy.write_text(json.dumps(_template_graph()), encoding="utf-8")
    # An image rendered from it: ComfyUI embeds the UI graph, id included.
    prompt = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}}}
    write_png_with_prompt(root / "output" / "probe_00001_.png", prompt, _template_graph())

    from app.parsers import official_templates
    official_templates.invalidate()
    run_scan("full")
    return root


def _one(sql: str, *args):
    return dbmod.get_ro().execute(sql, args).fetchone()


def _template_id() -> int:
    row = _one("SELECT id FROM workflows WHERE origin = 'official_template'")
    assert row is not None, "the template was not indexed"
    return int(row["id"])


def test_template_is_indexed_read_only_with_its_published_details(templated):
    rows = dbmod.get_ro().execute(
        "SELECT * FROM workflows WHERE origin = 'official_template'").fetchall()
    assert len(rows) == 1, "index*.json must not be indexed as templates"
    w = rows[0]
    assert w["name"] == TPL_NAME and w["title"] == "Probe: Text to Image"
    assert w["root_id"] is None, "a template belongs to no root, so nothing can move it"
    assert w["graph_uuid"] == TPL_UUID
    assert w["preview_path"] and w["preview_path"].endswith(f"{TPL_NAME}-1.webp")
    assert w["description"] == "A probe template for the test suite."
    assert w["description_source"] == "official"
    t = _one("SELECT * FROM workflow_templates WHERE workflow_id = ?", int(w["id"]))
    assert t["category"] == "Image" and t["category_group"] == "Foundation"
    assert json.loads(t["tags_json"]) == ["Text to Image"]
    assert t["required_size"] == 2_000_000_000 and t["open_source"] == 1
    assert t["bundle"] == "media-image" and t["package_version"] == "0.1.137"
    downloads = {m["name"]: m["url"] for m in json.loads(t["model_downloads_json"])}
    assert downloads[CKPT].startswith("https://")
    assert downloads["evil.safetensors"] is None, "a non-https URL is never kept"


def test_declared_models_inside_subgraphs_become_dependencies(templated):
    from app.services.queries import workflows_query

    deps = workflows_query.workflow_dependencies(_template_id())
    by_name = {m["ref_name"]: m for m in deps["models"]}
    assert by_name[CKPT]["status"] == "satisfied"
    assert by_name[MISSING]["status"] == "missing", \
        "a loader inside a subgraph is still a dependency"
    assert by_name[MISSING]["download"]["url"].endswith(MISSING)
    assert by_name[MISSING]["download"]["directory"] == "diffusion_models"
    assert by_name[MISSING]["download"]["host"] == "huggingface.co"


def test_templates_are_listed_only_when_asked_for(templated):
    from app.services.queries import workflows_query

    default = workflows_query.list_workflows({}, limit=200)
    assert all(i["origin"] != "official_template" for i in default.items)
    only = workflows_query.list_workflows({"origin": ["official_template"]}, limit=200)
    assert [i["name"] for i in only.items] == [TPL_NAME]
    item = only.items[0]
    assert item["read_only"] is True
    assert item["template"]["category"] == "Image"
    assert workflows_query.list_workflows(
        {"origin": ["official_template"], "ready": True}).page["total"] == 0, \
        "it still needs one model"
    assert workflows_query.list_workflows(
        {"origin": ["official_template"], "ready": False}).page["total"] == 1


def test_counts_keep_templates_apart(templated):
    m = _one("SELECT m.workflow_count, m.template_count FROM models m "
             "JOIN model_files f ON f.model_id = m.id WHERE f.filename = ?", CKPT)
    # The saved copy and the synthetic probe.json use it; the template is apart.
    assert m["template_count"] == 1
    assert m["workflow_count"] == 2
    stats = dict(_one("SELECT * FROM v_vault_stats"))
    assert stats["templates"] == 1
    total = _one("SELECT COUNT(*) n FROM workflows WHERE origin <> 'official_template' "
                 "AND missing_since IS NULL")["n"]
    assert stats["workflows"] == total


def test_copy_and_image_trace_back_to_the_template(templated):
    from app.services.queries import outputs_query, workflows_query

    tpl_id = _template_id()
    copy = _one("SELECT id FROM workflows WHERE name = 'my-probe-copy'")
    out = _one("SELECT id, workflow_id, workflow_uuid FROM outputs "
               "WHERE filename = 'probe_00001_.png'")
    assert out["workflow_uuid"] == TPL_UUID
    assert out["workflow_id"] == copy["id"], "the owner's own file wins the link"

    detail = workflows_query.get_workflow(int(copy["id"]))
    assert detail["based_on_template"]["uid"] == f"workflow:{tpl_id}"
    assert [o["uid"] for o in detail["outputs_recent"]] == [f"output:{out['id']}"]

    tpl = workflows_query.get_workflow(tpl_id)
    assert [c["uid"] for c in tpl["copies"]] == [f"workflow:{copy['id']}"]
    assert tpl["counts_outputs"] == 1, "a template is credited with renders of its copies"
    assert tpl["actions"]["can_delete"] is False and tpl["actions"]["read_only_reason"]

    o = outputs_query.get_output(int(out["id"]))
    assert o["workflow"]["uid"] == f"workflow:{copy['id']}"
    assert o["template"]["uid"] == f"workflow:{tpl_id}"
    assert outputs_query.list_outputs({"from_workflow_graph": tpl_id}).page["total"] == 1


def test_model_usage_lists_templates_separately(templated):
    from app.services.queries import models_query

    mid = _one("SELECT model_id FROM model_files WHERE filename = ?", CKPT)["model_id"]
    usage = models_query.model_usage(int(mid))
    assert all("probe" in w["name"] for w in usage["workflows"])
    assert [t["name"] for t in usage["templates"]] == [TPL_NAME]
    assert usage["templates"][0]["ready"] is False


def test_no_file_operation_can_touch_a_template(templated):
    from app.services import file_ops

    uid = f"workflow:{_template_id()}"
    path = _one("SELECT abs_path FROM workflows WHERE origin = 'official_template'")["abs_path"]
    before = Path(path).read_bytes()

    renamed = file_ops.rename(uid, "hijacked.json")
    assert not renamed.ok and renamed.details.get("reason") == "official_template_read_only"
    moved = file_ops.move([uid], int(_one("SELECT id FROM roots LIMIT 1")["id"]), "")
    assert not moved[0].ok
    for mode, confirm in (("trash", False), ("permanent", True)):
        deleted = file_ops.delete([uid], mode=mode, confirm=confirm)
        assert not deleted[0].ok
    assert Path(path).read_bytes() == before
    assert _one("SELECT 1 FROM workflows WHERE origin = 'official_template'") is not None

    info = file_ops.resolve_file(uid)
    assert info["read_only"] is True and info["path"] == path


def test_a_template_row_cannot_be_pointed_at_another_file(templated, synthetic_comfyui):
    """resolve_file serves a template only from the located template folder."""
    from app.core.errors import PathNotAllowed
    from app.services import file_ops

    tpl_id = _template_id()
    elsewhere = synthetic_comfyui.parent / "secret.json"
    elsewhere.write_text("{}", encoding="utf-8")

    def _op(conn):
        conn.execute("UPDATE workflows SET abs_path = ? WHERE id = ?", (str(elsewhere), tpl_id))
        conn.commit()
    dbmod.writer().run(_op)
    with pytest.raises(PathNotAllowed):
        file_ops.resolve_file(f"workflow:{tpl_id}")


def test_download_endpoint_serves_the_template_json(templated):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/v1/files/download", params={"uid": f"workflow:{_template_id()}"})
        assert r.status_code == 200
        assert json.loads(r.content)["id"] == TPL_UUID
        listed = client.get("/api/v1/workflows", params={"origin": "official_template"})
        assert listed.status_code == 200 and listed.json()["page"]["total"] == 1
        bad = client.get("/api/v1/workflows", params={"origin": "nonsense"})
        assert bad.status_code == 422


def test_catalogue_skips_index_files_and_links_indexed_rows(templated):
    from app.services import comfyui_service

    cat = comfyui_service.template_catalogue()
    assert cat["available"] is True
    names = [i["id"] for i in cat["items"]]
    assert names == [TPL_NAME], "index.json / index.fr.json are not templates"
    assert cat["items"][0]["uid"] == f"workflow:{_template_id()}"


def test_a_rescan_without_changes_reparses_nothing(templated):
    status = run_scan("incremental")
    wf = (status.get("phases") or {}).get("workflows") or {}
    stats = wf.get("stats") if isinstance(wf, dict) else None
    if isinstance(stats, dict):
        assert stats.get("parsed", 0) == 0
    assert _one("SELECT COUNT(*) n FROM workflows WHERE origin = 'official_template'")["n"] == 1
