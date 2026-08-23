"""A node that is present must never be reported missing.

Three families of false positive shipped in the same report, and each one is
pinned here at the parser level:

1. **Subgraph instances.**  A workflow declares its reusable subgraphs under
   ``definitions.subgraphs``; a node that instantiates one carries the
   definition's UUID as its ``type``.  Those UUIDs were being indexed as node
   classes and then reported as missing packages - eight of them in one file.

2. **Frontend virtual nodes.**  ``Note``, ``MarkdownNote``, ``Reroute`` and
   ``PrimitiveNode`` are drawn by the web client and have no Python class in any
   install.  ``MarkdownNote`` alone accounted for 117 "missing" rows.

3. **JavaScript-registered nodes.**  ``ComfyUI-KJNodes`` registers ``GetNode``
   and ``SetNode`` from ``web/js/setgetnodes.js`` and defines neither in Python.

The counterweight is asserted in every section: a class that genuinely is not
installed still has to come out missing.  The bug is not fixed by believing
every workflow.
"""

from __future__ import annotations

import json

from app.parsers import node_js, workflow_graph

UUID_DECODE = "34d63160-e149-4935-ab1f-892ffe617fec"
UUID_LATENTS = "2979f3eb-1c4c-45ff-8387-f1f4addb72f4"
UUID_NESTED = "07609e03-45f4-49c7-a233-a933ebf95094"


def subgraph_workflow(*, nested: bool = False) -> dict:
    """The shape of the owner's Video Editor Suite examples, in miniature."""
    inner = {
        "id": UUID_LATENTS, "name": "Process Latents", "version": 1,
        "nodes": [{"id": 1, "type": "KSampler", "widgets_values": [1, "fixed", 20]}],
        "links": [],
    }
    if nested:
        inner["definitions"] = {"subgraphs": [{
            "id": UUID_NESTED, "name": "length (seconds)",
            "nodes": [{"id": 1, "type": "PrimitiveInt", "widgets_values": [5]}],
            "links": [],
        }]}
        inner["nodes"].append({"id": 2, "type": UUID_NESTED})
    return {
        "id": "wf", "last_node_id": 9, "version": 0.4,
        "nodes": [
            {"id": 1, "type": "CheckpointLoaderSimple",
             "widgets_values": ["sd15.safetensors"]},
            {"id": 2, "type": UUID_DECODE},
            {"id": 3, "type": UUID_LATENTS},
            {"id": 4, "type": UUID_LATENTS},
            {"id": 5, "type": "AbsentThirdPartyNode"},
        ],
        "links": [],
        "definitions": {"subgraphs": [
            {"id": UUID_DECODE, "name": "Decode",
             "nodes": [{"id": 1, "type": "VAEDecode"}], "links": []},
            inner,
        ]},
    }


def write(tmp_path, data: dict, *, bom: bool = False, name: str = "wf.json"):
    path = tmp_path / name
    text = json.dumps(data, ensure_ascii=False)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


# ---------------------------------------------------------------------------
# 1. Subgraphs
# ---------------------------------------------------------------------------

def test_a_subgraph_id_is_not_a_node_class(tmp_path):
    result = workflow_graph.analyze(write(tmp_path, subgraph_workflow()))
    assert result.ok
    assert UUID_DECODE not in result.node_types, (
        "the subgraph's own UUID was recorded as a node class, which is what "
        "made the workflow ask for a package that does not exist")
    assert UUID_LATENTS not in result.node_types


def test_a_subgraph_is_counted_rather_than_dropped(tmp_path):
    result = workflow_graph.analyze(write(tmp_path, subgraph_workflow()))
    assert result.has_subgraphs is True
    assert result.subgraph_count == 2
    assert result.subgraph_defs[UUID_DECODE] == "Decode"
    assert result.subgraph_types == {UUID_DECODE: 1, UUID_LATENTS: 2}


def test_a_subgraph_declared_inside_a_subgraph_still_resolves(tmp_path):
    result = workflow_graph.analyze(write(tmp_path, subgraph_workflow(nested=True)))
    assert result.subgraph_count == 3
    assert UUID_NESTED in result.subgraph_defs
    assert UUID_NESTED not in result.node_types


def test_a_workflow_saved_with_a_utf8_bom_still_parses(tmp_path):
    """The Video Editor Suite examples ship with a BOM; ``json.load`` alone
    refuses them, which would have failed the file silently."""
    path = write(tmp_path, subgraph_workflow(), bom=True, name="bom.json")
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf"
    result = workflow_graph.analyze(path)
    assert result.ok, f"BOM file rejected: {result.error_code} {result.error_message}"
    assert result.subgraph_count == 2


def test_a_real_node_in_a_subgraph_workflow_is_still_reported(tmp_path):
    result = workflow_graph.analyze(write(tmp_path, subgraph_workflow()))
    assert "AbsentThirdPartyNode" in result.node_types, (
        "suppressing subgraph ids must not suppress genuine node classes")


def test_a_uuid_that_is_not_declared_is_left_alone(tmp_path):
    """Only ids the file itself declares are internal references."""
    data = subgraph_workflow()
    data["definitions"]["subgraphs"] = []
    result = workflow_graph.analyze(write(tmp_path, data))
    assert UUID_DECODE in result.node_types
    assert result.subgraph_count == 0


# ---------------------------------------------------------------------------
# 2. Frontend virtual nodes
# ---------------------------------------------------------------------------

def test_the_core_virtual_nodes_are_the_four_the_client_draws():
    assert {e[0] for e in node_js.CORE_VIRTUAL_NODES} == {
        "Note", "MarkdownNote", "Reroute", "PrimitiveNode"}


def test_a_note_workflow_parses_its_notes_as_normal_nodes(tmp_path):
    """The parser still records them; it is the index that must call them
    provided.  Dropping them here would hide them from the node breakdown."""
    data = {"nodes": [
        {"id": 1, "type": "Note", "widgets_values": ["a reminder"]},
        {"id": 2, "type": "MarkdownNote", "widgets_values": ["# heading"]},
        {"id": 3, "type": "Reroute"},
        {"id": 4, "type": "CheckpointLoaderSimple",
         "widgets_values": ["sd15.safetensors"]},
    ], "links": []}
    result = workflow_graph.analyze(write(tmp_path, data, name="notes.json"))
    assert result.ok
    assert set(result.node_types) == {"Note", "MarkdownNote", "Reroute",
                                      "CheckpointLoaderSimple"}


# ---------------------------------------------------------------------------
# 3. JavaScript-registered nodes
# ---------------------------------------------------------------------------

KJ_SOURCE = """
import { app } from "../../scripts/app.js";

app.registerExtension({
    name: "SetNode",
    registerCustomNodes() {
        class SetNode extends LiteGraph.LGraphNode {}
        LiteGraph.registerNodeType("SetNode", SetNode);
        SetNode.category = "KJNodes";
    },
});
"""

GET_SOURCE = """
const NODE_NAME = "GetNode";
class GetNode extends LiteGraph.LGraphNode {}
LiteGraph.registerNodeType(NODE_NAME, GetNode);
"""

#: A consumer, not a registrar: matching ``===`` would invent node types out of
#: every package that merely reacts to one.
CONSUMER_SOURCE = """
app.registerExtension({
    async beforeRegisterNodeDef(nodeType) {
        if (nodeType.comfyClass === "NotDeclaredHere") { return; }
        if (nodeType.comfyClass == 'AlsoNotDeclared') { return; }
    },
});
"""


def js_package(tmp_path, name: str, sources: dict[str, str]):
    pkg = tmp_path / name
    web = pkg / "web" / "js"
    web.mkdir(parents=True)
    for filename, text in sources.items():
        (web / filename).write_text(text, encoding="utf-8")
    return pkg


def test_a_node_registered_only_in_javascript_is_discovered(tmp_path):
    pkg = js_package(tmp_path, "KJNodes",
                     {"setgetnodes.js": KJ_SOURCE, "get.js": GET_SOURCE})
    found = {n.node_id for n in node_js.scan_package(pkg)}
    assert found == {"SetNode", "GetNode"}


def test_the_registering_file_and_line_are_recorded(tmp_path):
    pkg = js_package(tmp_path, "KJNodes", {"setgetnodes.js": KJ_SOURCE})
    node = next(n for n in node_js.scan_package(pkg) if n.node_id == "SetNode")
    assert node.source_file == "web/js/setgetnodes.js"
    assert node.source_lineno > 0


def test_a_comparison_against_comfyclass_registers_nothing(tmp_path):
    pkg = js_package(tmp_path, "consumer", {"ext.js": CONSUMER_SOURCE})
    assert node_js.scan_package(pkg) == []


def test_a_package_with_no_client_code_yields_nothing(tmp_path):
    pkg = tmp_path / "plain"
    (pkg / "web").mkdir(parents=True)
    (pkg / "web" / "style.css").write_text("body{}", encoding="utf-8")
    assert node_js.scan_package(pkg) == []


def test_the_scanner_never_leaves_the_package(tmp_path):
    """Only ``web``/``js``/``dist`` are read, and only from inside the package."""
    pkg = tmp_path / "pkg"
    (pkg / "src").mkdir(parents=True)
    (pkg / "src" / "elsewhere.js").write_text(
        'LiteGraph.registerNodeType("ShouldNotBeFound", X);', encoding="utf-8")
    assert node_js.scan_package(pkg) == []


def test_a_url_or_filename_is_never_mistaken_for_a_node_type(tmp_path):
    pkg = js_package(tmp_path, "noisy", {"x.js": (
        'registerNodeType("https://example.invalid/x", A);\n'
        'registerNodeType("./relative/thing.js", B);\n'
        'registerNodeType("RealNode", C);\n')})
    assert {n.node_id for n in node_js.scan_package(pkg)} == {"RealNode"}
