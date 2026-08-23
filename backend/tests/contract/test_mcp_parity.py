"""MCP_SPEC section 10 check 13 / BUILD_PLAN 4.3: MCP and REST must agree.

The two surfaces are separate presentation layers over one query layer.  The
promise is that an agent reading the vault through ``tools/call`` and a browser
reading it through ``/api/v1`` see the *same numbers* -- not "roughly the same
list", but the same value in every field of every row, in the same order.

Each field is its own test, so a divergence names the field that diverged
instead of dumping a diff.  The comparison itself is deliberately one-way: the
REST payload is the reference shape (it is what ``docs/API_CONTRACT.md``
freezes), and the map from an MCP key to the REST value it must equal is a
module constant, reviewable on its own.

The maps are guarded by an in-process test of their own: every MCP key named
here has to exist in that tool's published ``outputSchema``, so a renamed field
fails loudly instead of quietly comparing ``None`` to ``None``.

Everything else needs both surfaces answering at once and is therefore
``live`` + ``mcp``.  Nothing here writes: every call is a read.
"""

from __future__ import annotations

import json

import httpx
import pytest

pytestmark = pytest.mark.contract

MCP_PATH = "/api/v1/mcp"
PROTOCOL_VERSION = "2025-06-18"
JSON_HEADERS = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                # Required since the S-02 fix: /api/v1/mcp is guarded by the
                # same CSRF header as every other mutating route.
                "X-Vault-Request": "1"}
CLIENT_NAME = "parity"

PAGE_LIMIT = 5


# =============================================================================
# The field maps: MCP key -> the REST value it must equal
# =============================================================================

#: ``list_models`` row.  MCP flattens the REST sub-objects (``hash.autov2`` ->
#: ``autov2``) because a tool result is read by a model, not by a UI.
LIST_MODELS_FIELDS = {
    "uid": lambda r: r["uid"],
    "name": lambda r: r["name"],
    "filename": lambda r: r["filename"],
    "category": lambda r: r["category"],
    "role": lambda r: r["role"],
    "base_model": lambda r: r["base_model"]["family"],
    "base_model_confidence": lambda r: r["base_model"]["confidence"],
    "modality": lambda r: r["modality"],
    "precision": lambda r: r["precision"],
    "params_display": lambda r: r["params"]["display"],
    "size_bytes": lambda r: r["size"],
    "hash_state": lambda r: r["hash"]["state"],
    "autov2": lambda r: r["hash"]["autov2"],
    "integrity": lambda r: r["integrity"],
    "has_update": lambda r: bool(r["civitai"]["has_update"]),
    "workflow_count": lambda r: r["counts"]["workflows"],
    "output_count": lambda r: r["counts"]["outputs"],
    "rel_path": lambda r: r["rel_path"],
}

LIST_NODE_CLASSES_FIELDS = {
    "uid": lambda r: r["uid"],
    "node_id": lambda r: r["node_id"],
    "display_name": lambda r: r["display_name"],
    "class_name": lambda r: r["class_name"],
    "category": lambda r: r["category"],
    "description": lambda r: r["description"],
    "inputs": lambda r: {"required": r["inputs"]["required"],
                         "optional": r["inputs"]["optional"]},
    "outputs": lambda r: {"types": r["outputs"]["types"],
                          "names": r["outputs"]["names"]},
    "output_node": lambda r: bool(r["output_node"]),
    "confidence": lambda r: r["confidence"],
    "workflow_count": lambda r: r["counts"]["workflows"],
}

QUERY_OUTPUTS_FIELDS = {
    "uid": lambda r: r["uid"],
    "filename": lambda r: r["filename"],
    "rel_path": lambda r: r["rel_path"],
    "media_kind": lambda r: r["media_kind"],
    "width": lambda r: r["width"],
    "height": lambda r: r["height"],
    "duration_ms": lambda r: r["duration_ms"],
    "size_bytes": lambda r: r["size"],
    "created_at": lambda r: r["created_at"],
    "positive_prompt": lambda r: r["positive_prompt"],
    "seed": lambda r: r["seed"],
    "steps": lambda r: r["steps"],
    "cfg": lambda r: r["cfg"],
    "sampler": lambda r: r["sampler"],
    "scheduler": lambda r: r["scheduler"],
    "model_name": lambda r: r["model_name"],
    "model_uid": lambda r: r["model_uid"],
    "workflow_uid": lambda r: r["workflow_uid"],
}

#: ``vault_stats.counts`` keys that ``/api/v1/system/stats`` also publishes.
STATS_COUNT_KEYS = ("models", "model_files", "models_hashed", "node_packages",
                    "node_classes", "workflows", "workflows_broken", "outputs",
                    "embedded", "integrity_issues")

#: Which list tool each field map belongs to, and the key its rows live under.
FIELD_MAPS = {
    "list_models": ("models", LIST_MODELS_FIELDS),
    "list_node_classes": ("classes", LIST_NODE_CLASSES_FIELDS),
    "query_outputs": ("outputs", QUERY_OUTPUTS_FIELDS),
}


# =============================================================================
# In-process: the field maps must describe the real catalogue
# =============================================================================

@pytest.mark.parametrize("tool_name", sorted(FIELD_MAPS))
def test_field_map_only_names_declared_output_fields(tool_name):
    """A parity map that names a field the tool no longer returns would compare
    ``None`` against ``None`` forever and pass while the surfaces drifted."""
    from app.mcp import registry

    list_key, fields = FIELD_MAPS[tool_name]
    schema = registry.BY_NAME[tool_name].output_schema
    declared = set(schema["properties"][list_key]["items"]["properties"])
    assert set(fields) <= declared, f"undeclared: {sorted(set(fields) - declared)}"


def test_stats_count_keys_are_declared():
    from app.mcp import registry

    declared = set(registry.BY_NAME["vault_stats"]
                   .output_schema["properties"]["counts"]["properties"])
    assert set(STATS_COUNT_KEYS) <= declared


# =============================================================================
# Live plumbing
# =============================================================================

class McpHttp:
    """A minimal MCP client over the streamable-HTTP transport."""

    def __init__(self, base_url: str, *, timeout: float = 180.0) -> None:
        self.url = base_url.rstrip("/") + MCP_PATH
        self.client = httpx.Client(timeout=timeout)
        self.session_id: str | None = None
        self._next_id = 0

    def _post(self, body: dict, *, with_session: bool = True) -> httpx.Response:
        headers = dict(JSON_HEADERS)
        if with_session and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return self.client.post(self.url, headers=headers,
                                content=json.dumps(body).encode("utf-8"))

    def open(self) -> McpHttp:
        self._next_id += 1
        response = self._post(
            {"jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                        "clientInfo": {"name": CLIENT_NAME, "version": "1.0"}}},
            with_session=False)
        self.session_id = response.headers["mcp-session-id"]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """``tools/call`` that must succeed; returns ``structuredContent``."""
        self._next_id += 1
        body = self._post({"jsonrpc": "2.0", "id": self._next_id,
                           "method": "tools/call",
                           "params": {"name": name,
                                      "arguments": arguments or {}}}).json()
        assert "error" not in body, body["error"]
        result = body["result"]
        assert result.get("isError") is not True, result["content"][0]["text"]
        return result["structuredContent"]

    def close(self) -> None:
        self.client.close()


@pytest.fixture(scope="module")
def mcp(running_server):
    client = McpHttp(running_server).open()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="module")
def rest(running_server):
    with httpx.Client(base_url=running_server.rstrip("/") + "/api/v1",
                      timeout=120.0) as client:
        yield client


def rest_json(rest, path: str, params=None) -> dict:
    response = rest.get(path, params=params)
    assert response.status_code == 200, f"{path} -> {response.status_code}"
    return response.json()


def assert_same_length(mcp_rows: list, rest_rows: list, label: str) -> None:
    assert len(mcp_rows) == len(rest_rows), \
        f"{label}: MCP returned {len(mcp_rows)} row(s), REST {len(rest_rows)}"
    assert mcp_rows, f"{label}: nothing indexed to compare"


# =============================================================================
# list_models
# =============================================================================

@pytest.fixture(scope="module")
def models_pair(mcp, rest):
    args = {"limit": PAGE_LIMIT, "sort": "-size"}
    structured = mcp.call("list_models", args)
    payload = rest_json(rest, "/models", {"limit": PAGE_LIMIT, "sort": "-size"})
    if not structured["models"]:
        pytest.skip("no model indexed")
    return structured, payload


@pytest.mark.live
@pytest.mark.mcp
def test_list_models_total_matches_rest(models_pair):
    structured, payload = models_pair
    assert structured["total"] == payload["page"]["total"]


@pytest.mark.live
@pytest.mark.mcp
def test_list_models_returns_the_same_rows_in_the_same_order(models_pair):
    structured, payload = models_pair
    assert_same_length(structured["models"], payload["items"], "list_models")
    assert [row["uid"] for row in structured["models"]] == \
        [item["uid"] for item in payload["items"]]


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.parametrize("field", sorted(LIST_MODELS_FIELDS))
def test_list_models_field_matches_rest(models_pair, field):
    structured, payload = models_pair
    extract = LIST_MODELS_FIELDS[field]
    for i, (row, item) in enumerate(zip(structured["models"], payload["items"], strict=False)):
        assert row.get(field) == extract(item), f"row {i} ({row.get('uid')})"


# =============================================================================
# list_node_classes
# =============================================================================

@pytest.fixture(scope="module")
def node_classes_pair(mcp, rest):
    seed = mcp.call("list_node_classes", {"limit": 50})["classes"]
    categories = [c["category"] for c in seed if c.get("category")]
    if not categories:
        pytest.skip("no categorised node class indexed")
    category = categories[0]
    structured = mcp.call("list_node_classes",
                          {"category": [category], "limit": 20})
    payload = rest_json(rest, "/node-classes",
                        [("category", category), ("limit", 20),
                         ("deprecated", "false")])
    return structured, payload


@pytest.mark.live
@pytest.mark.mcp
def test_list_node_classes_total_matches_rest(node_classes_pair):
    structured, payload = node_classes_pair
    assert structured["total"] == payload["page"]["total"]


@pytest.mark.live
@pytest.mark.mcp
def test_list_node_classes_returns_the_same_rows(node_classes_pair):
    structured, payload = node_classes_pair
    assert_same_length(structured["classes"], payload["items"],
                       "list_node_classes")
    assert [row["uid"] for row in structured["classes"]] == \
        [item["uid"] for item in payload["items"]]


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.parametrize("field", sorted(LIST_NODE_CLASSES_FIELDS))
def test_list_node_classes_field_matches_rest(node_classes_pair, field):
    structured, payload = node_classes_pair
    extract = LIST_NODE_CLASSES_FIELDS[field]
    for i, (row, item) in enumerate(zip(structured["classes"], payload["items"], strict=False)):
        assert row.get(field) == extract(item), f"row {i} ({row.get('node_id')})"


# =============================================================================
# query_outputs
# =============================================================================

@pytest.fixture(scope="module")
def outputs_pair(mcp, rest):
    args = {"limit": 6, "sort": "-created"}
    structured = mcp.call("query_outputs", args)
    payload = rest_json(rest, "/outputs", {"limit": 6, "sort": "-created"})
    if not structured["outputs"]:
        pytest.skip("no output indexed")
    return structured, payload


@pytest.mark.live
@pytest.mark.mcp
def test_query_outputs_total_matches_rest(outputs_pair):
    structured, payload = outputs_pair
    assert structured["total"] == payload["page"]["total"]


@pytest.mark.live
@pytest.mark.mcp
def test_query_outputs_returns_the_same_rows(outputs_pair):
    structured, payload = outputs_pair
    assert_same_length(structured["outputs"], payload["items"], "query_outputs")
    assert [row["uid"] for row in structured["outputs"]] == \
        [item["uid"] for item in payload["items"]]


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.parametrize("field", sorted(QUERY_OUTPUTS_FIELDS))
def test_query_outputs_field_matches_rest(outputs_pair, field):
    structured, payload = outputs_pair
    extract = QUERY_OUTPUTS_FIELDS[field]
    for i, (row, item) in enumerate(zip(structured["outputs"], payload["items"], strict=False)):
        assert row.get(field) == extract(item), f"row {i} ({row.get('uid')})"


@pytest.mark.live
@pytest.mark.mcp
def test_query_outputs_detail_only_fields_match_the_detail_endpoint(outputs_pair, rest):
    """``query_outputs`` folds two fields into the list row that REST only
    serves from ``/outputs/{id}``; they still have to agree."""
    structured, _payload = outputs_pair
    for row in structured["outputs"][:3]:
        detail = rest_json(rest, f"/outputs/{row['uid'].split(':')[1]}")
        assert row["negative_prompt"] == detail["negative_prompt"], row["uid"]
        assert [lora["name"] for lora in row["loras"]] == \
            [lora["name"] for lora in detail["loras"]], row["uid"]


# =============================================================================
# inspect_workflow
# =============================================================================

@pytest.fixture(scope="module")
def workflow_triple(mcp, rest):
    workflows = mcp.call("list_workflows", {"limit": 1})["workflows"]
    if not workflows:
        pytest.skip("no workflow indexed")
    uid = workflows[0]["uid"]
    workflow_id = uid.split(":")[1]
    return (mcp.call("inspect_workflow", {"uid": uid}),
            rest_json(rest, f"/workflows/{workflow_id}"),
            rest_json(rest, f"/workflows/{workflow_id}/dependencies"))


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.parametrize("field,rest_key", [
    ("uid", "uid"), ("name", "name"), ("rel_path", "rel_path"),
    ("is_runnable", "is_runnable"), ("capability_tags", "capability_tags"),
])
def test_inspect_workflow_identity_matches_rest(workflow_triple, field, rest_key):
    structured, detail, _deps = workflow_triple
    assert structured["workflow"][field] == detail[rest_key]


@pytest.mark.live
@pytest.mark.mcp
def test_inspect_workflow_node_count_matches_rest(workflow_triple):
    structured, detail, _deps = workflow_triple
    assert structured["workflow"]["node_count"] == detail["counts"]["nodes"]
    assert len(structured["nodes"]) == len(detail["node_breakdown"])


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.parametrize("field,rest_key", [
    ("positive", "positive_prompt"), ("negative", "negative_prompt"),
    ("unresolved_count", "unresolved_inputs"),
])
def test_inspect_workflow_prompts_match_rest(workflow_triple, field, rest_key):
    structured, detail, _deps = workflow_triple
    assert structured["prompts"][field] == detail[rest_key]


@pytest.mark.live
@pytest.mark.mcp
def test_inspect_workflow_dependency_summary_matches_rest(workflow_triple):
    structured, _detail, deps = workflow_triple
    shared = {k: v for k, v in structured["dependencies"]["summary"].items()
              if k in deps["summary"]}
    assert shared == deps["summary"]


@pytest.mark.live
@pytest.mark.mcp
def test_inspect_workflow_dependency_lists_match_rest(workflow_triple):
    structured, _detail, deps = workflow_triple
    assert [d["ref_name"] for d in structured["dependencies"]["models"]] == \
        [d["ref_name"] for d in deps["models"]]
    assert [d["class_type"] for d in structured["dependencies"]["nodes"]] == \
        [d["class_type"] for d in deps["nodes"]]


@pytest.mark.live
@pytest.mark.mcp
def test_inspect_workflow_install_hint_maps_the_rest_registry_hint(workflow_triple):
    """``install_hint`` is the MCP rendering of REST's ``registry_hint``: the
    same ComfyUI-Manager registry row, named for what an agent does with it."""
    structured, _detail, deps = workflow_triple
    for node, dep in zip(structured["dependencies"]["nodes"], deps["nodes"], strict=False):
        hint = node.get("install_hint") or {}
        assert hint.get("repo_url") == (dep.get("registry_hint") or {}).get("repo_url"), \
            node.get("class_type")


# =============================================================================
# vault_stats and get_model
# =============================================================================

@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.parametrize("key", STATS_COUNT_KEYS)
def test_vault_stats_count_matches_system_stats(mcp, rest, key):
    counts = mcp.call("vault_stats", {})["counts"]
    assert counts[key] == rest_json(rest, "/system/stats")[key]


@pytest.fixture(scope="module")
def model_pair(mcp, rest):
    listed = mcp.call("list_models", {"limit": 1})["models"]
    if not listed:
        pytest.skip("no model indexed")
    uid = listed[0]["uid"]
    return (mcp.call("get_model", {"uid": uid}),
            rest_json(rest, f"/models/{uid.split(':')[1]}"))


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.parametrize("path,rest_path", [
    (("identity", "name"), ("name",)),
    (("identity", "size_bytes"), ("size",)),
    (("hash", "state"), ("hash", "state")),
    (("abs_path",), ("abs_path",)),
    (("usage", "workflow_count"), ("usage", "workflow_count")),
])
def test_get_model_matches_the_detail_endpoint(model_pair, path, rest_path):
    structured, detail = model_pair

    def dig(payload, keys):
        for key in keys:
            payload = payload[key]
        return payload

    assert dig(structured, path) == dig(detail, rest_path)


@pytest.mark.live
@pytest.mark.mcp
def test_get_model_by_name_resolves_to_the_same_row(model_pair, mcp):
    """An agent that only knows a filename must land on the same row a uid
    lookup would return; otherwise every downstream mutation is aimed wrong."""
    structured, detail = model_pair
    by_name = mcp.call("get_model", {"name": detail["filename"]})
    assert by_name["identity"]["uid"] == structured["identity"]["uid"]
