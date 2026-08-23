"""MCP posture under DECISIONS C5.

C5 grants the MCP server the full file-operation set **including delete**, and
that decision stands.  What is audited here is whether the six safety rails C5
made a condition of that grant actually hold at runtime:

1. trash-backed default,
2. ``confirm:true`` required for ``mode:"permanent"``,
3. an ``mcp_audit`` row - with argument values - for every mutating call,
4. a 200-item batch cap,
5. uid-only input, every mutation through ``core/pathsafe`` and ``file_ops``,
6. the ``mcp_read_only`` switch.

Plus the MCP_SPEC 9 posture: Origin validation, session handling, rate limiting,
no path input, no SSRF pivot.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.mcp

PROTOCOL = "2025-06-18"

#: DECISIONS C5.2: 13 read + 8 file-operation + 3 job-control, plus the 2
#: workflow "Enable" tools REQUIREMENTS_R2 C9.9 requires.
EXPECTED_TOOL_COUNT = 26

MUTATING_TOOLS = {
    "vault_reindex", "vault_hash_enqueue", "vault_hash_cancel",
    "vault_embeddings_rebuild", "vault_rename", "vault_move", "vault_delete",
    "vault_trash_restore", "vault_trash_empty", "vault_create_folder",
    "vault_assign_tags", "enable_workflow_fetch",
}


class Mcp:
    """A minimal MCP client over the HTTP transport."""

    def __init__(self, client) -> None:
        self.client = client
        self.session = None
        self._id = 0

    def initialize(self, **headers):
        response = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "security-suite", "version": "1"}}},
            **headers)
        self.session = response.headers.get("mcp-session-id")
        if self.session:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def _post(self, body, **headers):
        # X-Vault-Request is required since the S-02 fix; tests that
        # deliberately omit it pass X_Vault_Request=None via **headers.
        head = {"Content-Type": "application/json",
                "X-Vault-Request": "1", **headers}
        head = {k: v for k, v in head.items() if v is not None}
        if self.session:
            head["Mcp-Session-Id"] = self.session
        return self.client.post("/api/v1/mcp", content=json.dumps(body), headers=head)

    def rpc(self, method, params=None):
        self._id += 1
        return self._post({"jsonrpc": "2.0", "id": self._id, "method": method,
                           "params": params or {}}).json()

    def call(self, name, arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})

    def tools(self):
        return self.rpc("tools/list")["result"]["tools"]


@pytest.fixture
def mcp(client):
    session = Mcp(client)
    assert session.initialize().status_code == 200
    return session


def _text(result: dict) -> str:
    return result.get("result", {}).get("content", [{}])[0].get("text", "")


def _is_error(result: dict) -> bool:
    return bool(result.get("result", {}).get("isError"))


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------

def test_the_full_c5_tool_surface_is_present(mcp):
    names = {t["name"] for t in mcp.tools()}
    assert len(names) == EXPECTED_TOOL_COUNT
    assert names >= MUTATING_TOOLS, sorted(MUTATING_TOOLS - names)


def test_destructive_tools_are_annotated_as_destructive(mcp):
    by_name = {t["name"]: t for t in mcp.tools()}
    for name in ("vault_delete", "vault_move", "vault_rename", "vault_trash_empty"):
        annotations = by_name[name]["annotations"]
        assert annotations["readOnlyHint"] is False, name
        assert annotations["destructiveHint"] is True, name


def test_the_instructions_warn_the_agent_about_deletion(mcp):
    result = mcp.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {}})
    instructions = result["result"]["instructions"].lower()
    assert "trash" in instructions
    assert "never delete more than the user asked for" in instructions


# ---------------------------------------------------------------------------
# Rail 5: uid-only input, no filesystem path anywhere
# ---------------------------------------------------------------------------

def test_no_tool_accepts_a_filesystem_path_or_a_url(mcp):
    offenders = []
    for tool in mcp.tools():
        properties = (tool.get("inputSchema") or {}).get("properties") or {}
        for name in properties:
            lowered = name.lower()
            if "path" in lowered or "url" in lowered or lowered in ("file", "dir"):
                offenders.append((tool["name"], name))
    assert not offenders, f"MCP tools taking a path/url: {offenders}"


def test_every_tool_schema_is_closed(mcp):
    open_schemas = [t["name"] for t in mcp.tools()
                    if (t.get("inputSchema") or {}).get("additionalProperties")
                    is not False]
    assert not open_schemas, f"schemas allowing extra properties: {open_schemas}"


def test_an_injected_path_argument_is_a_hard_protocol_error(mcp):
    result = mcp.call("vault_delete", {"uids": ["model:1"], "path": r"C:\Windows"})
    assert result["error"]["code"] == -32602
    assert "path" in result["error"]["message"]


def test_a_malformed_uid_never_reaches_the_filesystem(mcp):
    for uid in ("../../Windows/win.ini", r"C:\Windows\win.ini", "model:../1",
                "node_class:1"):
        result = mcp.call("vault_delete", {"uids": [uid]})
        assert _is_error(result) or "fail" in _text(result).lower(), uid


def test_there_is_no_sql_under_app_mcp(app_dir):
    """BUILD_PLAN 4: every MCP handler goes through services/queries."""
    offenders = []
    for path in (app_dir / "mcp").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            upper = line.upper()
            if ("SELECT " in upper and "FROM" in upper) or "INSERT INTO" in upper \
                    or "DELETE FROM" in upper or ("UPDATE " in upper and " SET " in upper):
                offenders.append((path.name, lineno, line.strip()[:70]))
    assert not offenders, f"SQL inside app/mcp: {offenders}"


# ---------------------------------------------------------------------------
# Rails 1 and 2: trash by default, confirm for permanent
# ---------------------------------------------------------------------------

def test_delete_defaults_to_trash(mcp):
    schema = next(t for t in mcp.tools() if t["name"] == "vault_delete")
    assert schema["inputSchema"]["properties"]["mode"]["default"] == "trash"


def test_permanent_delete_without_confirm_is_refused(mcp):
    result = mcp.call("vault_delete", {"uids": ["model:1"], "mode": "permanent"})
    assert _is_error(result)
    assert "confirm=true" in _text(result)
    assert "Nothing was deleted" in _text(result)


def test_permanent_delete_with_confirm_false_is_refused(mcp):
    result = mcp.call("vault_delete", {"uids": ["model:1"], "mode": "permanent",
                                       "confirm": False})
    assert _is_error(result)
    assert "confirm=true" in _text(result)


def test_trash_empty_demands_confirm(mcp):
    assert _is_error(mcp.call("vault_trash_empty", {}))
    result = mcp.call("vault_trash_empty", {"confirm": False})
    assert _is_error(result)
    assert "confirm=true" in _text(result)


def test_a_real_delete_is_recoverable_from_trash(indexed_client):
    """The rail that matters: the default really is reversible."""
    session = Mcp(indexed_client)
    session.initialize()
    model = indexed_client.get("/api/v1/models").json()["items"][0]
    from pathlib import Path

    original = Path(model["abs_path"])
    assert original.is_file()

    result = session.call("vault_delete", {"uids": [model["uid"]]})
    assert not _is_error(result)
    assert not original.exists()

    listed = session.call("vault_trash_list", {})
    trash_id = listed["result"]["structuredContent"]["items"][0]["id"]
    restored = session.call("vault_trash_restore", {"ids": [trash_id]})
    assert not _is_error(restored)
    assert original.is_file(), "trash-backed delete was not reversible"


# ---------------------------------------------------------------------------
# Rail 4: the 200-item batch cap
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,field", [
    ("vault_delete", "uids"),
    ("vault_move", "uids"),
    ("vault_assign_tags", "uids"),
    ("vault_trash_restore", "ids"),
])
def test_batch_cap_is_declared_in_the_schema(mcp, tool, field):
    schema = next(t for t in mcp.tools() if t["name"] == tool)
    assert schema["inputSchema"]["properties"][field]["maxItems"] == 200


def test_a_201_item_call_is_refused_before_anything_happens(mcp):
    result = mcp.call("vault_delete", {"uids": [f"model:{i}" for i in range(201)]})
    assert _is_error(result)
    assert "at most 200 items" in _text(result)
    assert "Page the call" in _text(result)


# ---------------------------------------------------------------------------
# Rail 3: the audit log
# ---------------------------------------------------------------------------

def test_every_mutating_call_writes_an_audit_row_with_argument_values(mcp):
    from app.core import db as dbmod
    from app.services import mcp_audit

    before = dbmod.scalar(dbmod.get_ro(), "SELECT COUNT(*) FROM mcp_audit") or 0
    mcp.call("vault_rename", {"uid": "model:424242", "new_name": "probe.safetensors"})
    after = mcp_audit.recent(limit=5)

    assert (dbmod.scalar(dbmod.get_ro(), "SELECT COUNT(*) FROM mcp_audit") or 0) > before
    row = after["items"][0]
    assert row["tool"] == "vault_rename"
    assert row["transport"] == "http"
    assert row["session_id"]
    # C5 rail 3: argument VALUES are logged for mutations.
    arguments = row["arguments"] if isinstance(row["arguments"], dict) \
        else json.loads(row["arguments"])
    assert arguments["new_name"] == "probe.safetensors"
    assert arguments["uid"] == "model:424242"


def test_read_tools_do_not_write_audit_rows(mcp):
    from app.core import db as dbmod

    before = dbmod.scalar(dbmod.get_ro(), "SELECT COUNT(*) FROM mcp_audit") or 0
    mcp.call("vault_stats", {})
    mcp.call("list_models", {"limit": 5})
    after = dbmod.scalar(dbmod.get_ro(), "SELECT COUNT(*) FROM mcp_audit") or 0
    assert after == before


def test_tool_call_logging_records_argument_keys_not_values_for_reads(mcp, caplog):
    with caplog.at_level("INFO", logger="vault.mcp"):
        mcp.call("vault_search", {"q": "a-sensitive-prompt-string"})
    joined = " ".join(r.message % r.args if r.args else r.message
                      for r in caplog.records)
    assert "a-sensitive-prompt-string" not in joined


# ---------------------------------------------------------------------------
# Rail 6: the read-only switch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,arguments", [
    ("vault_delete", {"uids": ["model:1"]}),
    ("vault_rename", {"uid": "model:1", "new_name": "x.safetensors"}),
    ("vault_move", {"uids": ["model:1"], "target_root_id": 1, "target_folder": "a"}),
    ("vault_trash_empty", {"confirm": True}),
    ("vault_create_folder", {"root_id": 1, "folder": "a"}),
    ("vault_assign_tags", {"uids": ["model:1"], "add": ["x"]}),
    ("vault_reindex", {}),
    ("vault_hash_enqueue", {}),
    ("vault_embeddings_rebuild", {}),
    ("enable_workflow_fetch", {"workflow_uid": "workflow:1", "plan_token": "x" * 12,
                               "item_ids": ["mode_0000000000000000"], "confirm": True}),
])
def test_read_only_mode_refuses_every_mutating_tool(client, mcp, tool, arguments):
    client.patch("/api/v1/system/config", json={"mcp_read_only": True})
    try:
        result = mcp.call(tool, arguments)
        assert _is_error(result), tool
        assert "read-only mode" in _text(result), tool
        assert "Nothing was changed" in _text(result), tool
    finally:
        client.patch("/api/v1/system/config", json={"mcp_read_only": False})


def test_read_only_mode_still_serves_read_tools(client, mcp):
    client.patch("/api/v1/system/config", json={"mcp_read_only": True})
    try:
        assert not _is_error(mcp.call("vault_stats", {}))
    finally:
        client.patch("/api/v1/system/config", json={"mcp_read_only": False})


def test_the_stdio_read_only_flag_forces_read_only():
    from app.mcp.protocol import Dispatcher

    assert Dispatcher(transport="stdio", force_read_only=True).read_only() is True


# ---------------------------------------------------------------------------
# MCP_SPEC 9 posture
# ---------------------------------------------------------------------------

def test_origin_validation(naked_client):
    for origin in ("https://evil.example", "http://attacker.test",
                   "null", "http://127.0.0.1.evil.test"):
        session = Mcp(naked_client)
        assert session.initialize(Origin=origin).status_code == 403, origin


def test_only_the_vaults_own_origin_is_accepted(naked_client):
    """S-02 regression.

    Before the fix any loopback *port* was accepted, so a page served by
    ComfyUI itself (127.0.0.1:8188, which runs third-party custom-node
    JavaScript) could call vault_delete.  Only the vault's own origin is
    allowed now; a different loopback port is a different origin.
    """
    for origin in ("http://127.0.0.1:8127", "http://localhost:8127"):
        assert Mcp(naked_client).initialize(Origin=origin).status_code == 200, origin

    # ComfyUI's own port is the attack that S-02 described, and an arbitrary
    # loopback port stands for the general case.  The Vite dev ports are
    # deliberately NOT asserted here: http.py allows them only while the app is
    # in dev mode, so their verdict depends on whether frontend/dist exists.
    for origin in ("http://127.0.0.1:8188",   # ComfyUI serves custom-node JS
                   "http://127.0.0.1:9999"):
        assert Mcp(naked_client).initialize(Origin=origin).status_code == 403, origin


def test_an_unknown_session_is_rejected(naked_client):
    response = naked_client.post(
        "/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Mcp-Session-Id": "0" * 32, "X-Vault-Request": "1"})
    assert response.status_code == 404


def test_deleting_the_session_terminates_it(client):
    session = Mcp(client)
    session.initialize()
    assert client.delete("/api/v1/mcp",
                         headers={"Mcp-Session-Id": session.session}
                         ).status_code == 204
    assert client.post("/api/v1/mcp",
                       json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
                       headers={"Mcp-Session-Id": session.session}
                       ).status_code == 404


def test_rate_limiting_is_enforced_per_session(mcp):
    from app.mcp.protocol import RATE_LIMIT_CALLS

    limited = False
    for _ in range(RATE_LIMIT_CALLS + 5):
        result = mcp.call("vault_stats", {})
        if _is_error(result) and "Rate limit" in _text(result):
            limited = True
            break
    assert limited, f"no rate limit after {RATE_LIMIT_CALLS + 5} tool calls"


# S-06 fixed by ``SessionStore`` MAX_SESSIONS + least-recently-seen eviction.
# A failure here means the store is unbounded again.
def test_session_creation_is_capped(naked_client):
    from app.mcp.protocol import SESSIONS

    start = SESSIONS.count()
    for _ in range(250):
        Mcp(naked_client).initialize()
    assert SESSIONS.count() - start < 250


def test_the_session_store_never_grows_past_its_cap(client):
    """Executed: 300 initialize calls, and the table stays at the ceiling."""
    from app.mcp.protocol import MAX_SESSIONS, SESSIONS

    live = [Mcp(client) for _ in range(300)]
    for mcp_client in live:
        mcp_client.initialize()
    assert SESSIONS.count() <= MAX_SESSIONS
    # The most recent session is the one that survived, so a working client is
    # never evicted by a flood that arrived before it.
    assert SESSIONS.get(live[-1].session) is not None


def test_no_tool_performs_network_egress_on_the_agents_behalf(app_dir):
    """MCP_SPEC 9: an agent cannot use the vault as an SSRF pivot."""
    handlers = (app_dir / "mcp" / "handlers.py").read_text(encoding="utf-8")
    for needle in ("httpx", "requests", "urllib.request", "civitai_service",
                   "ollama_service", "socket."):
        assert needle not in handlers, f"{needle} reachable from an MCP handler"


def test_no_tool_serves_file_content(mcp):
    """MCP_SPEC 9: there is no read_file tool and no way to exfiltrate a model."""
    names = {t["name"] for t in mcp.tools()}
    for forbidden in ("read_file", "get_file", "download", "vault_read",
                      "fetch", "vault_download", "run", "exec", "update_comfyui"):
        assert forbidden not in names


def test_the_updater_is_not_exposed_through_mcp(mcp):
    """C8.3: never from MCP without confirmation - it is simply not a tool."""
    joined = json.dumps(mcp.tools()).lower()
    assert "update_comfyui" not in joined
    assert "run_updater" not in joined
