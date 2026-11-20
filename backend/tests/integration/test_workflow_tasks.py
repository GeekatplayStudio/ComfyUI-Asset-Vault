"""Browse workflows by what they do (schema v11): index, filter, group, count."""

from __future__ import annotations

import json
import time

import pytest

from app.core import db as dbmod
from app.indexing.service import get_indexer

TIMEOUT_S = 120


def run_scan(mode: str = "full") -> None:
    indexer = get_indexer()
    indexer.start(mode=mode, trigger="test")
    deadline = time.monotonic() + TIMEOUT_S
    while indexer.running():
        if time.monotonic() > deadline:
            indexer.cancel()
            pytest.fail(f"scan did not finish within {TIMEOUT_S}s")
        time.sleep(0.02)


def _graph(*classes: str) -> dict:
    return {"nodes": [{"id": i + 1, "type": c, "widgets_values": []}
                      for i, c in enumerate(classes)], "links": []}


GRAPHS = {
    "rembg.json": _graph("LoadImage", "BiRefNetRMBG", "SaveImage"),
    "upscale_4x.json": _graph("LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel",
                              "SaveImage"),
    "kling_i2v.json": _graph("LoadImage", "KlingImage2VideoNode", "SaveVideo"),
}


@pytest.fixture
def tasked(temp_vault, synthetic_comfyui):
    folder = synthetic_comfyui / "user" / "default" / "workflows"
    for name, graph in GRAPHS.items():
        (folder / name).write_text(json.dumps(graph), encoding="utf-8")
    run_scan("full")
    return synthetic_comfyui


def _names(client, **params) -> set[str]:
    r = client.get("/api/v1/workflows", params={"limit": 200, **params})
    assert r.status_code == 200, r.text
    return {i["name"] for i in r.json()["items"]}


def test_scan_stores_tasks_and_runs_on(tasked, hermetic_client):
    items = {i["name"]: i for i in
             hermetic_client.get("/api/v1/workflows", params={"limit": 200}).json()["items"]}
    assert items["rembg"]["primary_task"] == "remove_background"
    assert items["rembg"]["primary_task_label"] == "Remove background"
    assert items["upscale_4x"]["tasks"][0] == "upscale"
    assert items["kling_i2v"]["primary_task"] == "image_to_video"
    assert items["kling_i2v"]["runs_on"] == "api"
    assert items["probe"]["runs_on"] == "local"


def test_filter_by_task_and_runs_on(tasked, hermetic_client):
    assert _names(hermetic_client, task="remove_background") == {"rembg"}
    assert _names(hermetic_client, task=["upscale", "remove_background"]) == \
        {"rembg", "upscale_4x"}
    assert _names(hermetic_client, runs_on="api") == {"kling_i2v"}
    assert "kling_i2v" not in _names(hermetic_client, runs_on="local")


def test_unknown_task_or_runs_on_is_422(tasked, hermetic_client):
    for params in ({"task": "teleport"}, {"runs_on": "cloud"}):
        r = hermetic_client.get("/api/v1/workflows", params=params)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_group_by_task_is_ordered_labelled_and_contiguous(tasked, hermetic_client):
    body = hermetic_client.get("/api/v1/workflows",
                               params={"group": "task", "limit": 200}).json()
    keys = [g["key"] for g in body["groups"]]
    from app.parsers.workflow_tasks import TASK_KEYS
    assert keys == [k for k in TASK_KEYS if k in keys], "groups follow the task list"
    labels = {g["key"]: g["label"] for g in body["groups"]}
    assert labels["upscale"] == "Upscale"
    seen, current = [], None
    for item in body["items"]:
        if item["primary_task"] != current:
            current = item["primary_task"]
            assert current not in seen, "each task's rows arrive as one run"
            seen.append(current)


def test_sort_by_task(tasked, hermetic_client):
    r = hermetic_client.get("/api/v1/workflows", params={"sort": "task", "limit": 200})
    assert r.status_code == 200
    from app.parsers.workflow_tasks import TASK_KEYS
    order = [TASK_KEYS.index(i["primary_task"]) for i in r.json()["items"]]
    assert order == sorted(order)


def test_task_counts_ignore_their_own_filter(tasked, hermetic_client):
    body = hermetic_client.get("/api/v1/workflows/tasks",
                               params={"task": "upscale"}).json()
    counts = {t["key"]: t for t in body["tasks"]}
    assert len(body["tasks"]) == 15, "every task is listed, zeros included"
    assert counts["remove_background"]["count"] == 1, "other chips still say what is there"
    assert counts["image_to_video"]["api"] == 1
    runs = {r["key"]: r["count"] for r in body["runs_on"]}
    assert runs["api"] == 0, "runs_on counts respect the task filter in force"


def test_migration_backfill_reproduces_what_the_indexer_wrote(tasked):
    from app.core.migrations import m011_workflow_tasks

    def snapshot(conn):
        return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT name, primary_task, runs_on, tasks_json FROM workflows")}

    def wipe_and_backfill(conn):
        before = snapshot(conn)
        conn.execute("UPDATE workflows SET tasks_json = NULL, primary_task = NULL, "
                     "runs_on = NULL")
        conn.execute("DELETE FROM workflow_tasks")
        m011_workflow_tasks.backfill(conn)
        conn.commit()
        return before, snapshot(conn)

    before, after = dbmod.writer().run(wipe_and_backfill)
    assert after == before
    assert after["rembg"][0] == "remove_background"
