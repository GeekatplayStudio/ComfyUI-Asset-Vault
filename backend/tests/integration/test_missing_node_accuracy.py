"""End to end: what a scan is allowed to call a missing node.

The owner's report was "missing nodes incorrect ... all nodes present, it is
mis indexing".  Three separate causes produced it, and a parser-level fix for
each is not enough on its own - the class has to reach ``node_classes``, the
dependency ladder has to resolve it, and ``workflows.missing_node_count`` has to
come out zero.  That whole path is exercised here against a synthetic install.

The last test is the one that keeps the rest honest: a workflow referencing a
package that really is absent must still be reported broken.
"""

from __future__ import annotations

import json
import time

import pytest

from app.core import db as dbmod
from app.indexing.service import get_indexer

TIMEOUT_S = 120

SUBGRAPH_ID = "34d63160-e149-4935-ab1f-892ffe617fec"


def run_scan(mode: str = "full") -> None:
    indexer = get_indexer()
    indexer.start(mode=mode, trigger="test")
    deadline = time.monotonic() + TIMEOUT_S
    while indexer.running():
        if time.monotonic() > deadline:
            indexer.cancel()
            pytest.fail(f"scan did not finish within {TIMEOUT_S}s")
        time.sleep(0.02)


def workflow_row(name: str):
    return dbmod.get_ro().execute(
        "SELECT * FROM workflows WHERE name = ?", (name,)).fetchone()


def missing_names(name: str) -> set[str]:
    conn = dbmod.get_ro()
    return {
        str(r["ref_name"]) for r in conn.execute(
            "SELECT d.ref_name FROM workflow_dependencies d "
            "JOIN workflows w ON w.id = d.workflow_id "
            "WHERE w.name = ? AND d.dep_kind = 'node' AND d.status = 'missing'",
            (name,))
    }


def write_workflow(root, name: str, data: dict, *, bom: bool = False) -> None:
    path = root / "user" / "default" / "workflows" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)


@pytest.fixture
def library(temp_vault, synthetic_comfyui):
    """One workflow per cause, plus a package that registers a node in JS."""
    root = synthetic_comfyui

    # A core ``nodes.py`` so the official package exists, exactly as it does in
    # a real install.  The frontend's virtual nodes are attached to it.
    (root / "nodes.py").write_text(
        "class CheckpointLoaderSimple: pass\n"
        "class VAEDecode: pass\n"
        "NODE_CLASS_MAPPINGS = {\n"
        '    "CheckpointLoaderSimple": CheckpointLoaderSimple,\n'
        '    "VAEDecode": VAEDecode,\n'
        "}\n", encoding="utf-8")

    # Cause 1: a subgraph definition, instantiated twice, saved with a BOM.
    write_workflow(root, "subgraph_case", {
        "nodes": [
            {"id": 1, "type": "CheckpointLoaderSimple",
             "widgets_values": ["sd15-probe.safetensors"]},
            {"id": 2, "type": SUBGRAPH_ID},
            {"id": 3, "type": SUBGRAPH_ID},
        ],
        "links": [],
        "definitions": {"subgraphs": [{
            "id": SUBGRAPH_ID, "name": "Decode",
            "nodes": [{"id": 1, "type": "VAEDecode"}], "links": [],
        }]},
    }, bom=True)

    # Cause 2: the owner's example - the only "missing" node was a sticky note.
    write_workflow(root, "note_case", {
        "nodes": [
            {"id": 1, "type": "Note", "widgets_values": ["read me"]},
            {"id": 2, "type": "MarkdownNote", "widgets_values": ["# read me"]},
            {"id": 3, "type": "Reroute"},
            {"id": 4, "type": "PrimitiveNode", "widgets_values": [7, "fixed"]},
            {"id": 5, "type": "CheckpointLoaderSimple",
             "widgets_values": ["sd15-probe.safetensors"]},
        ],
        "links": [],
    })

    # Cause 3: a package whose node type exists only in its JavaScript.
    pkg = root / "custom_nodes" / "probe-kjnodes"
    (pkg / "web" / "js").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        'class ProbePythonNode:\n'
        '    @classmethod\n'
        '    def INPUT_TYPES(cls): return {"required": {}}\n'
        '    RETURN_TYPES = ("INT",)\n'
        '    FUNCTION = "run"\n'
        '    CATEGORY = "probe"\n'
        'NODE_CLASS_MAPPINGS = {"ProbePythonNode": ProbePythonNode}\n',
        encoding="utf-8")
    (pkg / "web" / "js" / "setgetnodes.js").write_text(
        'import { app } from "../../scripts/app.js";\n'
        'class SetNode extends LiteGraph.LGraphNode {}\n'
        'LiteGraph.registerNodeType("SetNode", SetNode);\n'
        'class GetNode extends LiteGraph.LGraphNode {}\n'
        'LiteGraph.registerNodeType("GetNode", GetNode);\n',
        encoding="utf-8")
    write_workflow(root, "js_case", {
        "nodes": [
            {"id": 1, "type": "SetNode", "widgets_values": ["latent"]},
            {"id": 2, "type": "GetNode", "widgets_values": ["latent"]},
            {"id": 3, "type": "ProbePythonNode"},
        ],
        "links": [],
    })

    # The counterweight: a package that genuinely is not installed.
    write_workflow(root, "genuinely_broken", {
        "nodes": [
            {"id": 1, "type": "CheckpointLoaderSimple",
             "widgets_values": ["sd15-probe.safetensors"]},
            {"id": 2, "type": "Note", "widgets_values": ["still fine"]},
            {"id": 3, "type": "VHS_VideoCombine"},
        ],
        "links": [],
    })
    return root


# ---------------------------------------------------------------------------
# Cause 1 - subgraphs
# ---------------------------------------------------------------------------

def test_a_subgraph_instance_is_never_a_missing_package(library):
    run_scan()
    assert missing_names("subgraph_case") == set(), (
        "a subgraph's own UUID was reported as a node package to install")


def test_the_subgraph_count_is_recorded_for_the_ui(library):
    run_scan()
    row = workflow_row("subgraph_case")
    assert row is not None, "the BOM workflow never parsed"
    assert row["has_subgraphs"] == 1
    assert row["subgraph_count"] == 1


def test_a_subgraph_uuid_never_reaches_the_node_breakdown(library):
    run_scan()
    conn = dbmod.get_ro()
    rows = conn.execute(
        "SELECT wn.class_type FROM workflow_nodes wn JOIN workflows w "
        "ON w.id = wn.workflow_id WHERE w.name = 'subgraph_case'").fetchall()
    assert SUBGRAPH_ID not in {r["class_type"] for r in rows}


# ---------------------------------------------------------------------------
# Cause 2 - frontend virtual nodes
# ---------------------------------------------------------------------------

def test_notes_and_reroutes_are_provided_not_missing(library):
    run_scan()
    assert missing_names("note_case") == set(), (
        "the frontend draws these; no package can ever supply them")
    row = workflow_row("note_case")
    assert row["missing_node_count"] == 0
    assert row["is_runnable"] == 1


def test_the_virtual_nodes_are_recorded_as_frontend_provided(library):
    run_scan()
    conn = dbmod.get_ro()
    rows = conn.execute(
        "SELECT node_id, registration FROM node_classes "
        "WHERE node_id IN ('Note','MarkdownNote','Reroute','PrimitiveNode')"
    ).fetchall()
    assert {r["node_id"] for r in rows} == {
        "Note", "MarkdownNote", "Reroute", "PrimitiveNode"}
    assert {r["registration"] for r in rows} == {"frontend"}, (
        "the UI must be able to tell these apart from a parsed Python class")


# ---------------------------------------------------------------------------
# Cause 3 - JavaScript-registered nodes
# ---------------------------------------------------------------------------

def test_a_javascript_registered_node_is_not_missing(library):
    run_scan()
    assert missing_names("js_case") == set(), (
        "the package is installed and its JavaScript registers the type")


def test_a_javascript_node_is_marked_apart_from_a_python_one(library):
    run_scan()
    conn = dbmod.get_ro()
    rows = {
        r["node_id"]: r["registration"] for r in conn.execute(
            "SELECT nc.node_id, nc.registration FROM node_classes nc "
            "JOIN node_packages p ON p.id = nc.package_id "
            "WHERE p.folder_name = 'probe-kjnodes'")
    }
    assert rows.get("SetNode") == "javascript"
    assert rows.get("GetNode") == "javascript"
    assert rows.get("ProbePythonNode") == "python"


def test_the_dependency_view_says_who_provides_the_class(library):
    run_scan()
    from app.services.queries import workflows_query

    row = workflow_row("js_case")
    deps = workflows_query.workflow_dependencies(int(row["id"]))
    by_name = {d["class_type"]: d for d in deps["nodes"]}
    assert by_name["GetNode"]["status"] == "satisfied"
    assert by_name["GetNode"]["provided_by"] == "javascript"
    assert by_name["ProbePythonNode"]["provided_by"] == "python"


# ---------------------------------------------------------------------------
# The counterweight
# ---------------------------------------------------------------------------

def test_a_genuinely_absent_package_is_still_reported_missing(library):
    run_scan()
    assert missing_names("genuinely_broken") == {"VHS_VideoCombine"}, (
        "detection was suppressed wholesale instead of being made correct")
    row = workflow_row("genuinely_broken")
    assert row["missing_node_count"] == 1
    assert row["is_runnable"] == 0


def test_the_fixture_workflow_that_was_always_broken_stays_broken(library):
    """``probe.json`` from the shared fixture references a node nobody ships."""
    run_scan()
    assert "AbsentThirdPartyNode" in missing_names("probe")
