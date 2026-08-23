"""MCP_SPEC section 10 conformance: checks 1-12 and 14, the catalogue sweep, and
the streamable-HTTP transport extras.

Three layers live here, split by what each one genuinely needs.

*In-process* (no server, no network, no ComfyUI): the tool catalogue, the JSON
Schema shapes, the resource/prompt tables, and every ``Dispatcher`` error code.
These are ordinary tests -- they run in a clean checkout and are the reason the
schema contract cannot rot silently between live runs.

*Live HTTP* (``live`` + ``mcp``): the streamable-HTTP transport at
``/api/v1/mcp`` -- handshake, sessions, batching, Origin guard, SSE upgrade,
cursor pagination, rate limiting -- plus the sweep that calls every read tool
and validates its ``structuredContent`` against the ``outputSchema`` the server
itself published in ``tools/list``.

*Live stdio* (``live`` + ``mcp``): ``python -m app.mcp_stdio`` driven as a real
subprocess.  Check 11 (nothing but JSON-RPC ever reaches stdout) and check 14
(cold start to first response).  The subprocess is pointed at a private
*snapshot* of the vault DB, never at the live file.

Nothing here modifies the owner's library.  The sweep calls only the read tools
plus the three job-control tools with a scope that matches nothing; the eight
file-operation tools are exercised -- and their output schemas validated -- in
``test_mcp_mutations.py``, on a disposable probe file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.contract

BACKEND_DIR = Path(__file__).resolve().parents[2]

MCP_PATH = "/api/v1/mcp"
PROTOCOL_VERSION = "2025-06-18"
JSON_HEADERS = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                # Required since the S-02 fix: /api/v1/mcp is guarded by the
                # same CSRF header as every other mutating route.
                "X-Vault-Request": "1"}
#: Client identity sent on the wire.  Deliberately generic: the server must not
#: behave differently for one client than for another.
CLIENT_NAME = "conformance"

#: MCP_SPEC 10 check 14.  Measured as "process start -> first byte of the
#: initialize response", best of three, so one scheduler hiccup cannot fail it.
COLD_START_BUDGET_MS = 800

#: The whole catalogue: the 13 read tools of MCP_SPEC 5, the 8 file-operation
#: tools and the 3 promoted job-control tools pinned by DECISIONS C5.2, plus the
#: 2 workflow "Enable" tools required by REQUIREMENTS_R2 C9.9.  13+8+3+2 = 26.
EXPECTED_TOOL_COUNT = 26
#: Tools that declare ``readOnlyHint: false`` -- i.e. every mutating tool.
EXPECTED_WRITABLE_TOOLS = 12
#: Tools that declare ``destructiveHint: true``.  A tool joining this set is a
#: safety-relevant change and must be a deliberate edit to this list.
EXPECTED_DESTRUCTIVE_TOOLS = frozenset({
    "vault_rename", "vault_move", "vault_delete", "vault_trash_empty",
})
#: C5.2 promoted these from "an argument on another tool" to first-class
#: entries, because discoverability through ``tools/list`` is the point.
PROMOTED_JOB_TOOLS = frozenset({
    "vault_hash_enqueue", "vault_hash_cancel", "vault_embeddings_rebuild",
})

EXPECTED_RESOURCES = 5
EXPECTED_RESOURCE_TEMPLATES = 4
EXPECTED_PROMPTS = 4

DRAFT_07 = "http://json-schema.org/draft-07/schema#"
#: ``ToolDef.as_dict`` injects ``title`` alongside the four hints, so a
#: serialized annotations block carries five keys, not four.
TOOL_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

#: Read tools swept over the live transport.  Arguments that need a real id are
#: resolved at run time (see the ``sweep_args`` fixture) so the sweep does not
#: depend on any particular row surviving in the owner's vault.
SWEEP_TOOLS = (
    "vault_search", "list_models", "get_model", "list_node_packages",
    "list_node_classes", "get_node_class", "list_workflows", "inspect_workflow",
    "find_model_usage", "query_outputs", "vault_stats", "get_index_status",
    "vault_trash_list", "vault_hash_enqueue", "vault_hash_cancel",
    "vault_embeddings_rebuild", "enable_workflow_plan",
)
#: A scope that deliberately matches nothing, so the job-control tools can be
#: shape-checked without starting real work.  Their end-to-end behaviour is
#: exercised in ``test_mcp_mutations.py``.
NO_SUCH_CATEGORY = "zz_no_such_category"
NO_SUCH_BATCH = "zz-no-such-batch"


# =============================================================================
# A minimal draft-07 validator
# =============================================================================
# Carried over from the standalone sweep harness rather than pulling in
# ``jsonschema``: the suite must not grow a dependency just to assert a
# contract, and the subset the MCP schemas actually use is small and explicit.

def _type_ok(value: object, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return True


def schema_errors(value: object, schema: dict, path: str = "$",
                  errs: list[str] | None = None) -> list[str]:
    """Validate ``value`` against the draft-07 ``schema``; return every problem.

    Supports the keywords the MCP schemas use: ``type`` (including union
    types), ``enum``, ``properties``, ``required``, ``additionalProperties:
    false``, ``items`` and ``pattern``.  ``additionalProperties: false`` is the
    important one -- it is what turns "the payload has a shape" into "the
    payload has *exactly* this shape".
    """
    errs = [] if errs is None else errs
    if not isinstance(schema, dict):
        return errs
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            if not schema_errors(value, sub, path, []):
                return errs
        errs.append(f"{path}: matches no anyOf branch")
        return errs
    types = schema.get("type")
    if types is not None:
        wanted = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(value, t) for t in wanted):
            got = "null" if value is None else type(value).__name__
            errs.append(f"{path}: expected {wanted}, got {got}")
            return errs
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                errs.append(f"{path}.{req}: required but missing")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                errs.append(f"{path}: undeclared key(s) {extra}")
        for key, sub_value in value.items():
            if key in props:
                schema_errors(sub_value, props[key], f"{path}.{key}", errs)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value[:50]):
            schema_errors(item, schema["items"], f"{path}[{i}]", errs)
    if isinstance(value, str) and schema.get("pattern") and \
            not re.match(schema["pattern"], value):
        errs.append(f"{path}: {value!r} !~ {schema['pattern']}")
    return errs


def walk_schema(schema: dict, path: str = "$"):
    """Yield ``(path, subschema)`` for every object subschema in ``schema``."""
    if not isinstance(schema, dict):
        return
    yield path, schema
    for name, sub in (schema.get("properties") or {}).items():
        yield from walk_schema(sub, f"{path}.{name}")
    items = schema.get("items")
    if isinstance(items, dict):
        yield from walk_schema(items, f"{path}[]")
    for i, sub in enumerate(schema.get("anyOf") or []):
        yield from walk_schema(sub, f"{path}|anyOf[{i}]")


# =============================================================================
# In-process helpers (no server)
# =============================================================================

def tool_dicts() -> list[dict]:
    """The catalogue exactly as ``tools/list`` serializes it."""
    from app.mcp import registry

    return [t.as_dict() for t in registry.TOOLS]


def by_name(name: str) -> dict:
    return next(t for t in tool_dicts() if t["name"] == name)


@pytest.fixture
def dispatcher(monkeypatch):
    """A writable dispatcher whose read-only posture never touches the DB."""
    from app.mcp import protocol

    monkeypatch.setattr(protocol.Dispatcher, "read_only", lambda self: False)
    return protocol.Dispatcher(transport="test")


@pytest.fixture
def read_only_dispatcher():
    """``force_read_only`` short-circuits before any config/DB read."""
    from app.mcp import protocol

    return protocol.Dispatcher(transport="test", force_read_only=True)


@pytest.fixture
def session():
    from app.mcp import protocol

    return protocol.SESSIONS.create(transport="test")


def handshake(dispatcher, session) -> dict:
    """``initialize`` + ``notifications/initialized``, in the required order."""
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
                    "clientInfo": {"name": "mcp-client", "version": "1.0"}}},
        session)
    assert dispatcher.handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, session) is None
    return reply["result"]


# =============================================================================
# 1. The tool catalogue (in-process)
# =============================================================================

def test_catalogue_exposes_exactly_26_tools():
    assert len(tool_dicts()) == EXPECTED_TOOL_COUNT


def test_tool_names_are_unique_and_snake_case():
    names = [t["name"] for t in tool_dicts()]
    assert len(set(names)) == len(names), "duplicate tool name"
    bad = [n for n in names if not re.fullmatch(r"[a-z][a-z0-9_]*", n)]
    assert not bad, f"non snake_case tool name(s): {bad}"


@pytest.mark.parametrize("field", ["name", "title", "description", "inputSchema",
                                   "outputSchema", "annotations"])
def test_every_tool_declares_every_field(field):
    missing = [t.get("name") for t in tool_dicts() if not t.get(field)]
    assert not missing, f"tool(s) missing {field}: {missing}"


@pytest.mark.parametrize("key", ["inputSchema", "outputSchema"])
def test_every_schema_is_draft_07_and_closed(key):
    """MCP_SPEC 4: both schemas carry ``$schema`` and forbid extra properties."""
    problems = []
    for tool in tool_dicts():
        schema = tool[key]
        if schema.get("$schema") != DRAFT_07:
            problems.append(f"{tool['name']}.{key}: $schema={schema.get('$schema')!r}")
        if schema.get("type") != "object":
            problems.append(f"{tool['name']}.{key}: type={schema.get('type')!r}")
        if schema.get("additionalProperties") is not False:
            problems.append(f"{tool['name']}.{key}: additionalProperties is not false")
    assert not problems


@pytest.mark.parametrize("key", ["inputSchema", "outputSchema"])
def test_every_object_subschema_closes_itself(key):
    """A nested object that forgets ``additionalProperties: false`` silently
    re-opens the contract, so the check has to be recursive, not top level."""
    problems = []
    for tool in tool_dicts():
        for path, sub in walk_schema(tool[key], f"{tool['name']}.{key}"):
            if sub.get("type") == "object" and sub.get("properties") is not None \
                    and sub.get("additionalProperties") is not False:
                problems.append(path)
    assert not problems, f"open object subschema(s): {problems}"


@pytest.mark.parametrize("key", ["inputSchema", "outputSchema"])
def test_required_properties_are_declared(key):
    problems = []
    for tool in tool_dicts():
        for path, sub in walk_schema(tool[key], f"{tool['name']}.{key}"):
            props = sub.get("properties") or {}
            problems.extend(f"{path}.{req}" for req in sub.get("required") or []
                            if req not in props)
    assert not problems, f"required but undeclared: {problems}"


def test_annotations_carry_the_four_hints_plus_the_injected_title():
    """``ToolDef.as_dict`` merges ``title`` into the annotations block, so the
    serialized object has five keys.  All four hints must be real booleans."""
    problems = []
    for tool in tool_dicts():
        ann = tool["annotations"]
        if len(ann) != len(TOOL_HINTS) + 1:
            problems.append(f"{tool['name']}: {sorted(ann)}")
        if ann.get("title") != tool["title"]:
            problems.append(f"{tool['name']}: annotations.title != title")
        problems.extend(f"{tool['name']}.{hint}={ann.get(hint)!r}" for hint in TOOL_HINTS
                        if not isinstance(ann.get(hint), bool))
    assert not problems


def test_exactly_eleven_tools_are_writable():
    writable = [t["name"] for t in tool_dicts()
                if t["annotations"]["readOnlyHint"] is False]
    assert len(writable) == EXPECTED_WRITABLE_TOOLS, writable


def test_readonly_hint_agrees_with_the_mutating_flag():
    """The hint an agent reads and the flag the dispatcher enforces are two
    different fields; they must never disagree."""
    from app.mcp import registry

    mismatched = [t.name for t in registry.TOOLS
                  if t.mutating is t.as_dict()["annotations"]["readOnlyHint"]]
    assert not mismatched


def test_every_mutating_tool_is_audited():
    from app.mcp import registry

    assert not [t.name for t in registry.TOOLS if t.mutating and not t.audited]


def test_exactly_four_tools_are_destructive():
    destructive = {t["name"] for t in tool_dicts()
                   if t["annotations"]["destructiveHint"] is True}
    assert destructive == EXPECTED_DESTRUCTIVE_TOOLS


def test_promoted_job_control_tools_are_first_class():
    names = {t["name"] for t in tool_dicts()}
    assert names >= PROMOTED_JOB_TOOLS
    for name in sorted(PROMOTED_JOB_TOOLS):
        ann = by_name(name)["annotations"]
        assert ann["readOnlyHint"] is False, name
        assert ann["destructiveHint"] is False, name
        assert ann["openWorldHint"] is False, name
        # Enqueueing more hashing work twice queues more work; cancelling or
        # rebuilding twice does not.
        assert ann["idempotentHint"] is (name != "vault_hash_enqueue"), name


def test_vault_reindex_keeps_its_spec_schema():
    """The job-control tools were promoted *out* of ``vault_reindex``; it must
    not have grown a ``job``/``action`` argument to smuggle them back in."""
    props = by_name("vault_reindex")["inputSchema"]["properties"]
    assert sorted(props) == ["mode", "phases", "wait"]


def test_alias_hints_all_point_at_real_tools():
    from app.mcp import registry

    unknown = {alias: target for alias, target in registry.ALIAS_HINTS.items()
               if target not in registry.BY_NAME}
    assert not unknown


def test_every_tool_has_a_registered_handler():
    from app.mcp import handlers, registry

    assert not [t.name for t in registry.TOOLS if t.handler not in handlers.HANDLERS]


# =============================================================================
# 2. Resources and prompts (in-process)
# =============================================================================

def test_resource_and_prompt_counts():
    from app.mcp import prompts, resources

    assert len(resources.RESOURCES) == EXPECTED_RESOURCES
    assert len(resources.RESOURCE_TEMPLATES) == EXPECTED_RESOURCE_TEMPLATES
    assert len(prompts.PROMPTS) == EXPECTED_PROMPTS


def test_resources_are_uniquely_addressed_and_typed():
    from app.mcp import resources

    uris = [r["uri"] for r in resources.RESOURCES]
    assert len(set(uris)) == len(uris)
    for entry in resources.RESOURCES:
        assert entry["uri"].startswith("vault://")
        assert entry["mimeType"] == "application/json"
        assert entry["name"] and entry["title"] and entry["description"]
    for entry in resources.RESOURCE_TEMPLATES:
        assert "{" in entry["uriTemplate"], entry["uriTemplate"]
        assert entry["name"] and entry["title"] and entry["description"]


def test_prompts_declare_their_arguments():
    from app.mcp import prompts

    names = [p["name"] for p in prompts.PROMPTS]
    assert len(set(names)) == len(names)
    for prompt in prompts.PROMPTS:
        assert prompt["title"] and prompt["description"]
        for arg in prompt["arguments"]:
            assert arg["name"] and arg["description"]
            assert isinstance(arg.get("required", False), bool)


# =============================================================================
# 3. Dispatcher semantics (in-process, no server)
# =============================================================================

def test_initialize_returns_version_capabilities_serverinfo_instructions(
        dispatcher, session):
    from app.mcp import protocol

    result = handshake(dispatcher, session)
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == protocol.SERVER_NAME
    assert result["serverInfo"]["version"] == protocol.SERVER_VERSION
    for capability in ("tools", "resources", "prompts", "logging", "completions"):
        assert capability in result["capabilities"], capability
    # DECISIONS C5.3: the instructions must tell an agent it can write.
    assert "CAN modify the library" in result["instructions"]


def test_initialize_negotiates_an_unsupported_version_down_to_the_latest(
        dispatcher, session):
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1999-01-01", "capabilities": {},
                    "clientInfo": {"name": "mcp-client", "version": "1"}}},
        session)
    assert "error" not in reply
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION


@pytest.mark.parametrize("version", ["2025-06-18", "2025-03-26", "2024-11-05"])
def test_supported_versions_are_echoed_back(dispatcher, session, version):
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": version, "capabilities": {},
                    "clientInfo": {"name": "mcp-client", "version": "1"}}},
        session)
    assert reply["result"]["protocolVersion"] == version


def test_a_method_before_the_initialized_notification_is_32002(dispatcher, session):
    dispatcher.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "mcp-client", "version": "1"}}},
        session)
    assert session.initialized is False
    reply = dispatcher.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                              session)
    assert reply["error"]["code"] == -32002


@pytest.mark.parametrize("method", ["ping", "initialize", "notifications/cancelled"])
def test_only_the_initialized_notification_flips_the_session(dispatcher, session,
                                                             method):
    dispatcher.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
                      session)
    assert session.initialized is False
    dispatcher.handle({"jsonrpc": "2.0", "method": "notifications/initialized"},
                      session)
    assert session.initialized is True


def test_a_method_without_a_session_is_32001(dispatcher):
    reply = dispatcher.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None)
    assert reply["error"]["code"] == -32001


def test_unknown_jsonrpc_method_is_32601(dispatcher, session):
    handshake(dispatcher, session)
    reply = dispatcher.handle({"jsonrpc": "2.0", "id": 2, "method": "vault/nope"},
                              session)
    assert reply["error"]["code"] == -32601


def test_unknown_tool_is_a_tool_error_not_32601(dispatcher, session):
    """MCP_SPEC 3.6: an unknown *tool* is a tool-level failure, not a protocol
    error -- the agent must be able to read the message and retry."""
    handshake(dispatcher, session)
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "definitely_not_a_tool", "arguments": {}}}, session)
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "definitely_not_a_tool" in reply["result"]["content"][0]["text"]


def test_a_guessed_tool_name_gets_pointed_at_the_real_one(dispatcher, session):
    from app.mcp import registry

    handshake(dispatcher, session)
    alias, target = next(iter(registry.ALIAS_HINTS.items()))
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": alias, "arguments": {}}}, session)
    assert target in reply["result"]["content"][0]["text"]


def test_a_bad_argument_value_is_a_tool_error(dispatcher, session):
    handshake(dispatcher, session)
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "list_models", "arguments": {"limit": 9999}}}, session)
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "200" in reply["result"]["content"][0]["text"]


def test_an_unknown_argument_property_is_32602(dispatcher, session):
    """MCP_SPEC 4: a hallucinated argument key is a hard protocol error, because
    silently ignoring it would answer a question nobody asked."""
    handshake(dispatcher, session)
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "list_models",
                    "arguments": {"hallucinated_filter": 1}}}, session)
    assert reply["error"]["code"] == -32602
    assert "hallucinated_filter" in reply["error"]["message"]


@pytest.mark.parametrize("msg", [
    {"id": 1, "method": "ping"},                       # no jsonrpc member
    {"jsonrpc": "1.0", "id": 1, "method": "ping"},     # wrong version
    {"jsonrpc": "2.0", "id": 1, "method": 42},         # method is not a string
    {"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"},   # id is not scalar
])
def test_a_broken_envelope_is_32600(dispatcher, msg):
    assert dispatcher.handle(msg, None)["error"]["code"] == -32600


def test_non_object_params_is_32602(dispatcher, session):
    handshake(dispatcher, session)
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": "nope"}, session)
    assert reply["error"]["code"] == -32602


def test_a_notification_never_gets_a_response(dispatcher, session):
    handshake(dispatcher, session)
    assert dispatcher.handle(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
        session) is None
    # Even a *rejected* notification stays silent: JSON-RPC forbids answering one.
    assert dispatcher.handle({"jsonrpc": "2.0", "method": "vault/nope"},
                             session) is None


def test_tools_list_honours_a_cursor(dispatcher, session):
    handshake(dispatcher, session)
    page = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
         "params": {"cursor": "20"}}, session)["result"]
    assert len(page["tools"]) == EXPECTED_TOOL_COUNT - 20
    assert "nextCursor" not in page


def test_tools_list_rejects_a_bad_cursor(dispatcher, session):
    handshake(dispatcher, session)
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
         "params": {"cursor": "not-a-number"}}, session)
    assert reply["error"]["code"] == -32602


def test_logging_setlevel_rejects_an_unknown_level(dispatcher, session):
    handshake(dispatcher, session)
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "logging/setLevel",
         "params": {"level": "shout"}}, session)
    assert reply["error"]["code"] == -32602


def test_logging_setlevel_records_the_level(dispatcher, session):
    handshake(dispatcher, session)
    reply = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "logging/setLevel",
         "params": {"level": "debug"}}, session)
    assert reply["result"] == {}
    assert session.log_level == "debug"


# --- read-only posture -------------------------------------------------------

def test_read_only_mode_says_so_in_the_instructions(read_only_dispatcher, session):
    result = handshake(read_only_dispatcher, session)
    assert "READ-ONLY" in result["instructions"]


def test_read_only_mode_refuses_every_mutating_tool(read_only_dispatcher, session):
    """Refusal happens before argument validation and before the handler, so a
    read-only server cannot touch the disk even by accident."""
    from app.mcp import registry

    handshake(read_only_dispatcher, session)
    for tool in registry.TOOLS:
        if not tool.mutating:
            continue
        reply = read_only_dispatcher.handle(
            {"jsonrpc": "2.0", "id": 99, "method": "tools/call",
             "params": {"name": tool.name, "arguments": {}}}, session)
        assert "error" not in reply, tool.name
        assert reply["result"]["isError"] is True, tool.name
        assert "read-only" in reply["result"]["content"][0]["text"], tool.name


def test_read_only_mode_still_serves_the_read_tools(read_only_dispatcher, session):
    handshake(read_only_dispatcher, session)
    reply = read_only_dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    assert len(reply["result"]["tools"]) == EXPECTED_TOOL_COUNT


# --- rate limiting (in-process, no clock dependency) -------------------------

def test_the_rate_limiter_trips_after_the_configured_budget(session):
    from app.mcp import protocol

    for _ in range(protocol.RATE_LIMIT_CALLS):
        assert session.rate_limited() == 0
    retry_after = session.rate_limited()
    assert retry_after > 0
    assert retry_after <= protocol.RATE_LIMIT_WINDOW_S * 1000


# =============================================================================
# Live streamable-HTTP transport
# =============================================================================

class McpHttp:
    """A minimal MCP client over the streamable-HTTP transport."""

    def __init__(self, base_url: str, *, timeout: float = 180.0,
                 client_name: str = CLIENT_NAME) -> None:
        self.base_url = base_url.rstrip("/")
        self.url = self.base_url + MCP_PATH
        self.client_name = client_name
        self.client = httpx.Client(timeout=timeout)
        self.session_id: str | None = None
        self._next_id = 0

    # -- plumbing ---------------------------------------------------------
    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def headers(self, extra: dict | None = None, *, with_session: bool = True) -> dict:
        head = dict(JSON_HEADERS)
        if with_session and self.session_id:
            head["Mcp-Session-Id"] = self.session_id
        if extra:
            head.update(extra)
        return head

    def post(self, body, *, extra: dict | None = None,
             with_session: bool = True) -> httpx.Response:
        return self.client.post(
            self.url, headers=self.headers(extra, with_session=with_session),
            content=json.dumps(body).encode("utf-8"))

    def send(self, method: str, params: dict | None = None, *,
             extra: dict | None = None, with_session: bool = True) -> httpx.Response:
        msg: dict = {"jsonrpc": "2.0", "id": self.next_id(), "method": method}
        if params is not None:
            msg["params"] = params
        return self.post(msg, extra=extra, with_session=with_session)

    def rpc(self, method: str, params: dict | None = None) -> dict:
        return self.send(method, params).json()

    def result(self, method: str, params: dict | None = None) -> dict:
        body = self.rpc(method, params)
        assert "error" not in body, f"{method} -> {body['error']}"
        return body["result"]

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """``tools/call`` at the protocol level; tool failures are returned."""
        return self.result("tools/call",
                           {"name": name, "arguments": arguments or {}})

    def call_ok(self, name: str, arguments: dict | None = None) -> dict:
        result = self.call(name, arguments)
        assert result.get("isError") is not True, result["content"][0]["text"]
        return result

    def structured(self, name: str, arguments: dict | None = None) -> dict:
        return self.call_ok(name, arguments)["structuredContent"]

    # -- handshake --------------------------------------------------------
    def initialize(self, protocol_version: str = PROTOCOL_VERSION) -> httpx.Response:
        response = self.post(
            {"jsonrpc": "2.0", "id": self.next_id(), "method": "initialize",
             "params": {"protocolVersion": protocol_version,
                        "capabilities": {"roots": {"listChanged": True},
                                         "sampling": {}},
                        "clientInfo": {"name": self.client_name, "version": "1.0"}}},
            with_session=False)
        self.session_id = response.headers.get("mcp-session-id")
        return response

    def initialized(self) -> httpx.Response:
        return self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def open(self) -> McpHttp:
        self.initialize()
        self.initialized()
        return self

    def terminate(self) -> httpx.Response:
        return self.client.request("DELETE", self.url, headers=self.headers())

    def close(self) -> None:
        self.client.close()


@pytest.fixture
def mcp_raw(running_server):
    """An un-opened client, for the tests that drive the handshake themselves."""
    client = McpHttp(running_server)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="module")
def mcp(running_server):
    """One initialized session shared by the read-only live tests."""
    client = McpHttp(running_server).open()
    try:
        yield client
    finally:
        client.close()


# --- checks 1, 2, 3 ----------------------------------------------------------

@pytest.mark.live
@pytest.mark.mcp
def test_check_1_initialize_over_http(mcp_raw):
    from app.mcp import protocol

    response = mcp_raw.initialize()
    assert response.status_code == 200
    assert mcp_raw.session_id, "no Mcp-Session-Id header on the initialize response"
    result = response.json()["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == protocol.SERVER_NAME
    assert result["serverInfo"]["version"] == protocol.SERVER_VERSION
    for capability in ("tools", "resources", "prompts", "logging", "completions"):
        assert capability in result["capabilities"], capability
    assert "CAN modify the library" in result["instructions"]


@pytest.mark.live
@pytest.mark.mcp
def test_check_3_method_before_initialized_is_32002_over_http(mcp_raw):
    mcp_raw.initialize()
    assert mcp_raw.rpc("tools/list")["error"]["code"] == -32002


@pytest.mark.live
@pytest.mark.mcp
def test_check_2_initialized_notification_returns_202_and_no_body(mcp_raw):
    mcp_raw.initialize()
    response = mcp_raw.initialized()
    assert response.status_code == 202
    assert len(response.content) == 0


# --- check 4 -----------------------------------------------------------------

@pytest.mark.live
@pytest.mark.mcp
def test_check_4_tools_list_serves_the_whole_catalogue(mcp):
    served = mcp.result("tools/list")["tools"]
    assert len(served) == EXPECTED_TOOL_COUNT
    # The wire copy must be byte-identical to what the registry declares.
    assert {t["name"]: t for t in served} == {t["name"]: t for t in tool_dicts()}


@pytest.mark.live
@pytest.mark.mcp
def test_check_4_annotations_survive_serialization(mcp):
    served = mcp.result("tools/list")["tools"]
    writable = [t["name"] for t in served
                if t["annotations"]["readOnlyHint"] is False]
    destructive = {t["name"] for t in served
                   if t["annotations"]["destructiveHint"] is True}
    assert len(writable) == EXPECTED_WRITABLE_TOOLS
    assert destructive == EXPECTED_DESTRUCTIVE_TOOLS


# --- checks 5, 6, 7 ----------------------------------------------------------

@pytest.mark.live
@pytest.mark.mcp
def test_check_5_a_bad_argument_is_a_200_with_iserror(mcp):
    response = mcp.send("tools/call",
                        {"name": "list_models", "arguments": {"limit": 9999}})
    body = response.json()
    assert response.status_code == 200
    assert "error" not in body
    assert body["result"]["isError"] is True


@pytest.mark.live
@pytest.mark.mcp
def test_check_6_an_unknown_tool_is_iserror_not_32601(mcp):
    body = mcp.rpc("tools/call",
                   {"name": "definitely_not_a_tool", "arguments": {}})
    assert "error" not in body
    assert body["result"]["isError"] is True


@pytest.mark.live
@pytest.mark.mcp
def test_check_7_an_unknown_method_is_32601(mcp):
    assert mcp.rpc("vault/nope")["error"]["code"] == -32601


@pytest.mark.live
@pytest.mark.mcp
def test_an_unknown_argument_property_is_32602_over_http(mcp):
    body = mcp.rpc("tools/call", {"name": "list_models",
                                  "arguments": {"hallucinated_filter": 1}})
    assert body["error"]["code"] == -32602


# --- check 8 -----------------------------------------------------------------

@pytest.mark.live
@pytest.mark.mcp
def test_check_8_a_batch_answers_as_an_array_and_omits_notifications(mcp):
    body = mcp.post([
        {"jsonrpc": "2.0", "id": "a", "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "b", "method": "tools/list"},
        {"jsonrpc": "2.0", "id": "c", "method": "prompts/list"},
    ]).json()
    assert isinstance(body, list)
    assert sorted(msg["id"] for msg in body) == ["a", "b", "c"]
    assert all(msg["jsonrpc"] == "2.0" for msg in body)


# --- check 9 -----------------------------------------------------------------

@pytest.mark.live
@pytest.mark.mcp
def test_check_9_a_missing_session_is_404_and_32001(mcp_raw):
    response = mcp_raw.send("tools/list", with_session=False)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32001


@pytest.mark.live
@pytest.mark.mcp
def test_check_9_an_unknown_session_is_404_and_32001(mcp_raw):
    mcp_raw.session_id = "deadbeef" * 4
    response = mcp_raw.send("tools/list")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32001


# --- check 10 ----------------------------------------------------------------

@pytest.mark.live
@pytest.mark.mcp
def test_check_10_a_foreign_origin_is_rejected(mcp):
    """DNS rebinding is the canonical attack on a loopback MCP server."""
    response = mcp.send("ping", extra={"Origin": "http://evil.test"})
    assert response.status_code == 403


@pytest.mark.live
@pytest.mark.mcp
# The vault's own origin, in the spellings a browser may send. The Vite dev
# ports are allowed only while the app is in dev mode (http.py::_allowed_origins),
# so they are not asserted here -- see test_only_the_vaults_own_origin_is_accepted.
@pytest.mark.parametrize("origin", ["http://127.0.0.1:8127", "http://localhost:8127"])
def test_check_10_a_loopback_origin_is_allowed(mcp, origin):
    response = mcp.send("ping", extra={"Origin": origin})
    assert response.status_code == 200


# --- check 12 ----------------------------------------------------------------

@pytest.mark.live
@pytest.mark.mcp
def test_check_12_delete_terminates_the_session(mcp_raw):
    mcp_raw.open()
    response = mcp_raw.terminate()
    assert response.status_code in (200, 204)
    assert mcp_raw.send("ping").status_code == 404


# --- resources, prompts, completion, logging over the wire -------------------

@pytest.mark.live
@pytest.mark.mcp
def test_resources_prompts_and_templates_are_listed(mcp):
    assert len(mcp.result("resources/list")["resources"]) == EXPECTED_RESOURCES
    assert len(mcp.result("resources/templates/list")["resourceTemplates"]) == \
        EXPECTED_RESOURCE_TEMPLATES
    assert len(mcp.result("prompts/list")["prompts"]) == EXPECTED_PROMPTS


@pytest.mark.live
@pytest.mark.mcp
def test_prompts_get_returns_a_usable_message(mcp):
    workflows = mcp.structured("list_workflows", {"limit": 1})["workflows"]
    if not workflows:
        pytest.skip("no workflow indexed")
    result = mcp.result("prompts/get", {"name": "diagnose_workflow",
                                        "arguments": {"workflow": workflows[0]["uid"]}})
    assert len(result["messages"]) == 1
    assert "inspect_workflow" in result["messages"][0]["content"]["text"]


@pytest.mark.live
@pytest.mark.mcp
def test_completion_complete_offers_values(mcp):
    completion = mcp.result(
        "completion/complete",
        {"ref": {"type": "ref/prompt", "name": "recommend_model"},
         "argument": {"name": "base_model", "value": ""}})["completion"]
    assert isinstance(completion["values"], list)
    assert completion["values"], "no completion values for base_model"


@pytest.mark.live
@pytest.mark.mcp
def test_logging_setlevel_over_http(mcp):
    assert mcp.result("logging/setLevel", {"level": "debug"}) == {}


# =============================================================================
# The sweep: every read tool, validated against its own outputSchema
# =============================================================================

@pytest.fixture(scope="module")
def output_schemas(mcp) -> dict:
    return {t["name"]: t["outputSchema"] for t in mcp.result("tools/list")["tools"]}


@pytest.fixture(scope="module")
def sweep_args(mcp) -> dict:
    """Side-effect-free arguments, with every id resolved from the live vault."""
    models = mcp.structured("list_models", {"limit": 1})["models"]
    workflows = mcp.structured("list_workflows", {"limit": 1})["workflows"]
    classes = [c for c in mcp.structured("list_node_classes", {"limit": 50})["classes"]
               if c.get("category")]
    if not (models and workflows and classes):
        pytest.skip("the vault has no models / workflows / node classes indexed")
    model_uid = models[0]["uid"]
    workflow_uid = workflows[0]["uid"]
    node_id = classes[0]["node_id"]
    category = classes[0]["category"]
    return {
        "vault_search": {"query": "lora", "limit": 5},
        "list_models": {"limit": 5, "sort": "-size"},
        "get_model": {"uid": model_uid},
        "list_node_packages": {"limit": 5},
        "list_node_classes": {"limit": 5, "category": [category]},
        "get_node_class": {"node_id": node_id},
        "list_workflows": {"limit": 5},
        "inspect_workflow": {"uid": workflow_uid},
        "find_model_usage": {"uid": model_uid, "limit": 5},
        "query_outputs": {"limit": 5},
        "vault_stats": {"breakdown": ["category", "base_model", "media_kind"]},
        "get_index_status": {},
        "vault_trash_list": {"limit": 5},
        # Job control, called with a scope that matches nothing so no work
        # actually starts; the real behaviour lives in test_mcp_mutations.py.
        "vault_hash_enqueue": {"scope": "category", "category": NO_SUCH_CATEGORY},
        "vault_hash_cancel": {"batch_id": NO_SUCH_BATCH},
        "vault_embeddings_rebuild": {"force": False},
        # C9: the report is read-only and downloads nothing.  Fetching needs the
        # plan_token this call returns plus confirm=true, so the sweep can call
        # the planner safely and never the fetcher.
        "enable_workflow_plan": {"workflow_uid": workflow_uid},
    }


def assert_result_within_caps(result: dict) -> None:
    from app.mcp import handlers

    for block in result["content"]:
        assert len(block["text"]) <= handlers.TEXT_CAP, "text block over the cap"
    structured = result.get("structuredContent")
    if structured is not None:
        size = len(json.dumps(structured, default=str).encode("utf-8"))
        assert size <= handlers.STRUCTURED_CAP, f"structuredContent {size} B"


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.parametrize("tool_name", SWEEP_TOOLS)
def test_sweep_structured_content_matches_the_output_schema(
        mcp, sweep_args, output_schemas, tool_name):
    """Every tool's own published ``outputSchema`` is the oracle here, so a
    handler that grows a field without declaring it fails immediately."""
    result = mcp.call(tool_name, sweep_args[tool_name])
    if result.get("isError"):
        # ``vault_embeddings_rebuild`` is the one tool whose honest answer may
        # be a failure: the ONNX model is an optional install.
        assert tool_name == "vault_embeddings_rebuild", \
            result["content"][0]["text"]
        pytest.skip(f"{tool_name}: {result['content'][0]['text'][:120]}")
    structured = result.get("structuredContent")
    assert structured is not None, f"{tool_name} returned no structuredContent"
    assert not schema_errors(structured, output_schemas[tool_name])
    assert_result_within_caps(result)


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.parametrize("uri", ["vault://stats", "vault://models/index",
                                 "vault://nodes/index", "vault://workflows/index",
                                 "vault://health"])
def test_every_static_resource_reads_as_json(mcp, uri):
    entry = mcp.result("resources/read", {"uri": uri})["contents"][0]
    assert entry["uri"] == uri
    assert entry["mimeType"] == "application/json"
    assert json.loads(entry["text"])


@pytest.mark.live
@pytest.mark.mcp
def test_every_resource_template_resolves(mcp, sweep_args):
    model_id = sweep_args["get_model"]["uid"].split(":")[1]
    workflow_id = sweep_args["inspect_workflow"]["uid"].split(":")[1]
    node_id = sweep_args["get_node_class"]["node_id"]
    for uri in (f"vault://model/{model_id}",
                f"vault://workflow/{workflow_id}",
                f"vault://workflow/{workflow_id}/graph",
                f"vault://node-class/{node_id}"):
        entry = mcp.result("resources/read", {"uri": uri})["contents"][0]
        assert entry["mimeType"] == "application/json"
        assert json.loads(entry["text"]), uri


@pytest.mark.live
@pytest.mark.mcp
def test_an_unknown_resource_uri_is_32602(mcp):
    body = mcp.rpc("resources/read", {"uri": "vault://definitely-not-a-resource"})
    assert body["error"]["code"] == -32602


# =============================================================================
# Transport extras: version header, pagination, parse errors, SSE, rate limit
# =============================================================================

@pytest.mark.live
@pytest.mark.mcp
def test_an_unsupported_protocol_version_negotiates_instead_of_failing(mcp_raw):
    body = mcp_raw.initialize(protocol_version="1999-01-01").json()
    assert "error" not in body
    assert body["result"]["protocolVersion"] == PROTOCOL_VERSION


@pytest.mark.live
@pytest.mark.mcp
def test_a_bad_protocol_version_header_is_400_and_32600(mcp):
    response = mcp.send("ping", extra={"MCP-Protocol-Version": "1999-01-01"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


@pytest.mark.live
@pytest.mark.mcp
def test_a_supported_older_protocol_version_header_is_accepted(mcp):
    response = mcp.send("ping", extra={"MCP-Protocol-Version": "2025-03-26"})
    assert response.status_code == 200


@pytest.mark.live
@pytest.mark.mcp
def test_tools_list_honours_a_cursor_over_http(mcp):
    page = mcp.result("tools/list", {"cursor": "20"})
    assert len(page["tools"]) == EXPECTED_TOOL_COUNT - 20
    assert "nextCursor" not in page


@pytest.mark.live
@pytest.mark.mcp
def test_a_bad_cursor_is_32602_over_http(mcp):
    assert mcp.rpc("tools/list",
                   {"cursor": "not-a-number"})["error"]["code"] == -32602


@pytest.mark.live
@pytest.mark.mcp
def test_malformed_json_is_32700(mcp):
    response = mcp.client.post(mcp.url, headers=mcp.headers(), content=b"{not json")
    assert response.json()["error"]["code"] == -32700


@pytest.mark.live
@pytest.mark.mcp
def test_get_opens_a_notification_stream(mcp):
    with mcp.client.stream("GET", mcp.url,
                           headers={"Accept": "text/event-stream",
                                    "X-Vault-Request": "1",
                                    "Mcp-Session-Id": mcp.session_id}) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")


@pytest.mark.live
@pytest.mark.mcp
def test_get_without_the_sse_accept_header_is_406(mcp):
    response = mcp.client.get(mcp.url, headers={"Accept": "application/json",
                                                "X-Vault-Request": "1",
                                                "Mcp-Session-Id": mcp.session_id})
    assert response.status_code == 406


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.slow
def test_wait_true_upgrades_the_response_to_sse_with_progress(mcp, running_server):
    """``vault_reindex(wait=true)`` is the one long call; the transport has to
    switch to SSE and carry progress notifications plus the final response."""
    deadline = time.time() + 300
    while mcp.client.get(f"{running_server}/api/v1/index/status").json().get("active"):
        assert time.time() < deadline, "a scan was still running after 5 minutes"
        time.sleep(0.5)

    msg_id = mcp.next_id()
    frames = []
    with mcp.client.stream(
            "POST", mcp.url, headers=mcp.headers(),
            content=json.dumps(
                {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                 "params": {"name": "vault_reindex",
                            "arguments": {"mode": "incremental", "wait": True},
                            "_meta": {"progressToken": "probe-token"}}})) as response:
        content_type = response.headers.get("content-type", "")
        frames.extend(json.loads(line[6:]) for line in response.iter_lines()
                      if line.startswith("data: "))

    assert "text/event-stream" in content_type
    final = [f for f in frames if f.get("id") == msg_id]
    assert len(final) == 1, f"{len(frames)} frame(s), no single final response"
    assert final[0]["result"]["structuredContent"]["started"] is True
    progress = [f for f in frames if f.get("method") == "notifications/progress"]
    assert all(f["params"]["progressToken"] == "probe-token" for f in progress)


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.slow
def test_the_rate_limit_trips_on_its_own_session(running_server):
    """Burns a whole per-minute budget, so it runs on a session of its own."""
    from app.mcp import protocol

    client = McpHttp(running_server, client_name="mcp-client").open()
    try:
        tripped = None
        for i in range(protocol.RATE_LIMIT_CALLS + 10):
            result = client.call("get_index_status")
            if result.get("isError") and "Rate limit" in result["content"][0]["text"]:
                tripped = (i + 1, result)
                break
        assert tripped is not None, "the rate limiter never tripped"
        count, result = tripped
        assert count == protocol.RATE_LIMIT_CALLS + 1
        assert result["structuredContent"]["retry_after_ms"] > 0
    finally:
        client.close()


# =============================================================================
# Check 11 + check 14: the stdio transport as a real subprocess
# =============================================================================

STDIO_TIMEOUT_S = 300


def stdio_run(db_path: Path, messages: list, *, extra_args: tuple[str, ...] = (),
              log_level: str = "INFO") -> subprocess.CompletedProcess:
    env = dict(os.environ, VAULT_DB=str(db_path), VAULT_LOG_LEVEL=log_level)
    payload = "".join(json.dumps(m) + "\n" for m in messages).encode("utf-8")
    return subprocess.run(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, "-m", "app.mcp_stdio", *extra_args],
        cwd=str(BACKEND_DIR), env=env, input=payload, capture_output=True,
        timeout=STDIO_TIMEOUT_S, check=False)


@pytest.fixture(scope="module")
def stdio_session(live_db, live_workflow_uid) -> dict:
    """One full stdio conversation, replayed once for every check below.

    ``VAULT_DB`` points at the private snapshot, so nothing the subprocess does
    can reach the owner's database.
    """
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
                    "clientInfo": {"name": "mcp-client", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "vault_stats", "arguments": {"breakdown": ["category"]}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "list_models", "arguments": {"limit": 3, "sort": "-size"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "inspect_workflow", "arguments": {"uid": live_workflow_uid}}},
        {"jsonrpc": "2.0", "id": 7, "method": "resources/read",
         "params": {"uri": "vault://nodes/index"}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "definitely_not_a_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 9, "method": "vault/nope"},
        [{"jsonrpc": "2.0", "id": "x", "method": "ping"},
         {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
         {"jsonrpc": "2.0", "id": "y", "method": "prompts/list"}],
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
         "params": {"name": "vault_trash_list", "arguments": {"limit": 5}}},
    ]
    proc = stdio_run(live_db, messages)
    lines = [ln for ln in proc.stdout.decode("utf-8").splitlines() if ln.strip()]
    parsed = [json.loads(ln) for ln in lines]
    replies: dict = {}
    for msg in parsed:
        for one in (msg if isinstance(msg, list) else [msg]):
            if "id" in one:
                replies[one["id"]] = one
    return {"proc": proc, "lines": lines, "parsed": parsed, "replies": replies,
            "stderr": proc.stderr.decode("utf-8", "replace")}


@pytest.fixture(scope="module")
def live_workflow_uid(live_db) -> str:
    import sqlite3

    conn = sqlite3.connect(f"file:{live_db.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT id FROM workflows ORDER BY id LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None:
        pytest.skip("no workflow indexed")
    return f"workflow:{row[0]}"


@pytest.mark.live
@pytest.mark.mcp
def test_check_11_stdout_carries_nothing_but_jsonrpc(stdio_session):
    """The whole point of the stdio transport: one stray ``print`` anywhere in
    the import graph would corrupt the stream, so every line must parse."""
    for line in stdio_session["lines"]:
        message = json.loads(line)
        for one in (message if isinstance(message, list) else [message]):
            assert one.get("jsonrpc") == "2.0", line[:120]


@pytest.mark.live
@pytest.mark.mcp
def test_check_11_every_request_gets_exactly_one_reply(stdio_session):
    # 11 requests + one batch answered as a single array; the two notifications
    # are answered with silence.
    assert len(stdio_session["parsed"]) == 11
    assert set(stdio_session["replies"]) == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, "x", "y"}


@pytest.mark.live
@pytest.mark.mcp
def test_check_11_stdio_serves_the_same_catalogue_as_http(stdio_session):
    from app.mcp import protocol

    replies = stdio_session["replies"]
    assert replies[1]["result"]["serverInfo"]["name"] == protocol.SERVER_NAME
    assert replies[2]["result"] == {}
    assert len(replies[3]["result"]["tools"]) == EXPECTED_TOOL_COUNT


@pytest.mark.live
@pytest.mark.mcp
def test_check_11_stdio_tool_calls_return_structured_content(stdio_session):
    replies = stdio_session["replies"]
    assert replies[4]["result"]["structuredContent"]["counts"]["models"] >= 0
    assert replies[5]["result"]["isError"] is False
    assert replies[5]["result"]["structuredContent"]["returned"] <= 3
    assert "dependencies" in replies[6]["result"]["structuredContent"]
    assert json.loads(replies[7]["result"]["contents"][0]["text"])["rows"]
    assert replies[10]["result"]["isError"] is False


@pytest.mark.live
@pytest.mark.mcp
def test_check_11_stdio_reports_errors_the_same_way_http_does(stdio_session):
    replies = stdio_session["replies"]
    assert "error" not in replies[8]
    assert replies[8]["result"]["isError"] is True
    assert replies[9]["error"]["code"] == -32601


@pytest.mark.live
@pytest.mark.mcp
def test_check_11_a_batch_over_stdio_omits_notifications(stdio_session):
    batches = [m for m in stdio_session["parsed"] if isinstance(m, list)]
    assert len(batches) == 1
    assert sorted(m["id"] for m in batches[0]) == ["x", "y"]
    assert stdio_session["replies"]["x"]["result"] == {}
    assert len(stdio_session["replies"]["y"]["result"]["prompts"]) == EXPECTED_PROMPTS


@pytest.mark.live
@pytest.mark.mcp
def test_check_11_payload_caps_hold_over_stdio(stdio_session):
    from app.mcp import handlers

    for msg in stdio_session["parsed"]:
        for one in (msg if isinstance(msg, list) else [msg]):
            result = one.get("result")
            if not isinstance(result, dict):
                continue
            for block in result.get("content") or []:
                assert len(block.get("text", "")) <= handlers.TEXT_CAP
            structured = result.get("structuredContent")
            if structured is not None:
                size = len(json.dumps(structured, default=str).encode("utf-8"))
                assert size <= handlers.STRUCTURED_CAP


@pytest.mark.live
@pytest.mark.mcp
def test_check_11_logs_go_to_stderr_not_stdout(stdio_session):
    assert "vault.mcp" in stdio_session["stderr"], "the run produced no stderr log"
    assert "vault.mcp:" not in "".join(stdio_session["lines"])


@pytest.mark.live
@pytest.mark.mcp
def test_read_only_flag_blocks_a_mutating_tool_over_stdio(live_db):
    """The uid deliberately does not exist: if the read-only gate ever regressed
    this test would still not be able to touch a real file."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "mcp-client", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "vault_delete",
                    "arguments": {"uids": ["model:999999999"], "mode": "trash"}}},
    ]
    proc = stdio_run(live_db, messages, extra_args=("--read-only",))
    replies = [json.loads(ln) for ln in proc.stdout.decode().splitlines() if ln.strip()]
    call = next(m for m in replies if m.get("id") == 2)
    assert call["result"]["isError"] is True
    assert "read-only" in call["result"]["content"][0]["text"]


@pytest.mark.live
@pytest.mark.mcp
def test_read_only_flag_shows_up_in_the_instructions(live_db):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "mcp-client", "version": "1"}}},
    ]
    proc = stdio_run(live_db, messages, extra_args=("--read-only",))
    reply = json.loads(proc.stdout.decode().splitlines()[0])
    assert "READ-ONLY" in reply["result"]["instructions"]


@pytest.mark.live
@pytest.mark.mcp
@pytest.mark.perf
def test_check_14_cold_start_stays_within_budget(live_db):
    """Process start to the first byte of the initialize response.  Best of
    three: an agent launcher pays this on every single session."""
    line = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "mcp-client", "version": "1"}}}) + "\n"
    env = dict(os.environ, VAULT_DB=str(live_db), VAULT_LOG_LEVEL="WARNING")
    samples = []
    for _ in range(3):
        started = time.perf_counter()
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.mcp_stdio"], cwd=str(BACKEND_DIR), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        try:
            proc.stdin.write(line.encode("utf-8"))
            proc.stdin.flush()
            first = proc.stdout.readline()
            samples.append((time.perf_counter() - started) * 1000)
            assert json.loads(first)["result"]["protocolVersion"] == PROTOCOL_VERSION
        finally:
            proc.stdin.close()
            proc.wait(timeout=30)
    assert min(samples) < COLD_START_BUDGET_MS, \
        f"cold start {min(samples):.0f} ms, budget {COLD_START_BUDGET_MS} ms"
