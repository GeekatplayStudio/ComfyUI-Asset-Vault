"""JSON-RPC 2.0 envelope, MCP error codes, version negotiation and sessions.

Transport-agnostic on purpose: ``http.py`` and ``stdio.py`` both hand raw
decoded messages to :class:`Dispatcher` and only differ in framing.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .. import config as buildcfg

log = logging.getLogger("vault.mcp")

# --- protocol ----------------------------------------------------------------
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = "2025-06-18"

SERVER_NAME = "geekatplay-comfyui-vault"
SERVER_TITLE = "Geekatplay ComfyUI Asset Vault"
#: Read from the build constants rather than repeated: the self-updater compares
#: this app's version against a release tag, so a stale copy is a real defect.
SERVER_VERSION = buildcfg.VERSION

#: DECISIONS C5.3 - replaces the read-only wording MCP_SPEC 3.2 originally had.
INSTRUCTIONS = (
    "Local ComfyUI installation manager: models, node packages and node classes, "
    "workflows, and generated outputs. Use vault_stats first to see scale, "
    "vault_search for open-ended questions, and the list_*/get_* tools for precise "
    "lookups. This server CAN modify the library - rename, move, delete (trash-backed "
    "by default), and organize assets. Destructive operations are logged and "
    "recoverable from trash unless permanent deletion is explicitly confirmed. "
    "Prefer trash over permanent deletion. Never delete more than the user asked for."
)
READ_ONLY_INSTRUCTIONS_SUFFIX = (
    " NOTE: this server is currently running in READ-ONLY mode; every mutating tool "
    "will refuse to run."
)

CAPABILITIES: dict[str, Any] = {
    "tools": {"listChanged": True},
    "resources": {"subscribe": False, "listChanged": True},
    "prompts": {"listChanged": False},
    "logging": {},
    "completions": {},
}

# --- JSON-RPC error codes (MCP_SPEC 3.6) -------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
INVALID_SESSION = -32001
NOT_INITIALIZED = -32002

SESSION_IDLE_TIMEOUT_S = 30 * 60
RATE_LIMIT_CALLS = 120
RATE_LIMIT_WINDOW_S = 60.0

#: SECURITY_REVIEW S-06.  ``initialize`` is unauthenticated by design, so without
#: a ceiling a client could mint sessions until the process ran out of memory:
#: 300 calls produced 300 live sessions, each retained for 30 minutes.  A real
#: deployment has one or two clients; 64 leaves room for reconnect churn.
MAX_SESSIONS = 64

LOG_LEVELS = ("debug", "info", "notice", "warning", "error", "critical", "alert",
              "emergency")


class RpcError(Exception):
    """A JSON-RPC *protocol* error.  Never used for tool failures."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


def rpc_result(msg_id: Any, payload: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": payload}


def rpc_error(msg_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def negotiate(requested: Any) -> str:
    """MCP_SPEC 3.2: an unsupported version answers with the newest we support."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


def is_request(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and msg.get("id") is not None


def is_notification(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and msg.get("id") is None


def validate_envelope(msg: Any) -> None:
    """Raise :class:`RpcError` when ``msg`` is not a usable JSON-RPC message."""
    if not isinstance(msg, dict):
        raise RpcError(INVALID_REQUEST, "A JSON-RPC message must be an object.")
    if msg.get("jsonrpc") != "2.0":
        raise RpcError(INVALID_REQUEST, "Missing or wrong 'jsonrpc' member; "
                                        "expected \"2.0\".")
    method = msg.get("method")
    if method is not None and not isinstance(method, str):
        raise RpcError(INVALID_REQUEST, "'method' must be a string.")
    msg_id = msg.get("id")
    if msg_id is not None and not isinstance(msg_id, (str, int)):
        raise RpcError(INVALID_REQUEST, "'id' must be a string or a number.")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@dataclass
class Session:
    id: str
    transport: str = "http"
    protocol_version: str = LATEST_PROTOCOL_VERSION
    client_info: dict = field(default_factory=dict)
    client_capabilities: dict = field(default_factory=dict)
    initialized: bool = False
    log_level: str = "info"
    created_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    calls: deque = field(default_factory=deque)
    #: asyncio queues fed by the GET SSE stream (HTTP transport only).
    listeners: list = field(default_factory=list)

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def expired(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (now - self.last_seen) > SESSION_IDLE_TIMEOUT_S

    def rate_limited(self) -> int:
        """Return the retry-after in ms, or 0 when the call may proceed."""
        now = time.monotonic()
        while self.calls and (now - self.calls[0]) > RATE_LIMIT_WINDOW_S:
            self.calls.popleft()
        if len(self.calls) >= RATE_LIMIT_CALLS:
            return max(1, int((RATE_LIMIT_WINDOW_S - (now - self.calls[0])) * 1000))
        self.calls.append(now)
        return 0


class SessionStore:
    """In-process session table.  Small, thread-safe, swept lazily."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}

    def create(self, transport: str = "http") -> Session:
        with self._lock:
            self._sweep()
            self._evict_to(MAX_SESSIONS - 1)
            session = Session(id=uuid.uuid4().hex, transport=transport)
            self._sessions[session.id] = session
            return session

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(str(session_id))
            if session is None:
                return None
            if session.expired():
                self._sessions.pop(session.id, None)
                return None
            session.touch()
            return session

    def drop(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(str(session_id), None) is not None

    def count(self) -> int:
        with self._lock:
            self._sweep()
            return len(self._sessions)

    def live(self) -> list[Session]:
        """A snapshot of the non-expired sessions (used to fan notifications out)."""
        with self._lock:
            self._sweep()
            return list(self._sessions.values())

    def _sweep(self) -> None:
        now = time.monotonic()
        for sid in [s.id for s in self._sessions.values() if s.expired(now)]:
            self._sessions.pop(sid, None)

    def _evict_to(self, ceiling: int) -> int:
        """Drop least-recently-seen sessions until at most ``ceiling`` remain.

        Called under ``self._lock``.  Evicting the *oldest* is what makes the cap
        safe to apply: a client that is actually working keeps its session, and
        only the abandoned ones are reclaimed.
        """
        if len(self._sessions) <= ceiling:
            return 0
        ordered = sorted(self._sessions.values(), key=lambda s: s.last_seen)
        dropped = 0
        for session in ordered[:len(self._sessions) - max(ceiling, 0)]:
            self._sessions.pop(session.id, None)
            dropped += 1
        if dropped:
            log.info("mcp: evicted %d idle session(s) at the %d-session cap",
                     dropped, MAX_SESSIONS)
        return dropped


SESSIONS = SessionStore()


def server_info() -> dict:
    return {"name": SERVER_NAME, "title": SERVER_TITLE, "version": SERVER_VERSION}


def initialize_result(protocol_version: str, *, read_only: bool = False) -> dict:
    instructions = INSTRUCTIONS
    if read_only:
        instructions += READ_ONLY_INSTRUCTIONS_SUFFIX
    return {
        "protocolVersion": protocol_version,
        "capabilities": CAPABILITIES,
        "serverInfo": server_info(),
        "instructions": instructions,
    }


# ---------------------------------------------------------------------------
# Argument validation (a draft-07 subset - no third-party dependency)
# ---------------------------------------------------------------------------

class UnknownProperty(Exception):
    """An argument key the schema does not declare.

    MCP_SPEC 4 makes this a hard ``-32602`` on purpose: silently ignoring a
    hallucinated filter is how an agent convinces itself the vault is empty.
    """


class ArgumentError(Exception):
    """A declared argument with a bad value -> ``isError: true`` (MCP_SPEC 10.5)."""


_TYPE_MAP = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
    "array": list, "object": dict,
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    py = _TYPE_MAP.get(expected)
    return isinstance(value, py) if py else True


def _check(value: Any, spec: dict, path: str) -> Any:
    if not isinstance(spec, dict):
        return value

    if "anyOf" in spec:
        for sub in spec["anyOf"]:
            try:
                return _check(value, sub, path)
            except ArgumentError:
                continue
        raise ArgumentError(f"{path}: does not match any accepted shape.")

    types = spec.get("type")
    if types is not None:
        wanted = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(value, t) for t in wanted):
            raise ArgumentError(
                f"{path}: expected {' or '.join(wanted)}, got "
                f"{'null' if value is None else type(value).__name__}.")

    if "enum" in spec and value not in spec["enum"]:
        allowed = ", ".join(str(v) for v in spec["enum"])
        raise ArgumentError(f"{path}: must be one of {allowed}.")

    if isinstance(value, str):
        if "minLength" in spec and len(value) < int(spec["minLength"]):
            raise ArgumentError(f"{path}: must be at least {spec['minLength']} "
                                "characters.")
        if "maxLength" in spec and len(value) > int(spec["maxLength"]):
            raise ArgumentError(f"{path}: must be at most {spec['maxLength']} "
                                "characters.")
        pattern = spec.get("pattern")
        if pattern:
            import re

            if not re.match(pattern, value):
                raise ArgumentError(f"{path}: '{value[:80]}' does not match {pattern}.")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in spec and value < spec["minimum"]:
            raise ArgumentError(f"{path}: must be >= {spec['minimum']}.")
        if "maximum" in spec and value > spec["maximum"]:
            raise ArgumentError(f"{path}: must be <= {spec['maximum']}.")

    if isinstance(value, list):
        if "minItems" in spec and len(value) < int(spec["minItems"]):
            raise ArgumentError(f"{path}: needs at least {spec['minItems']} item(s).")
        if "maxItems" in spec and len(value) > int(spec["maxItems"]):
            raise ArgumentError(
                f"{path}: a single call may carry at most {spec['maxItems']} items; "
                f"{len(value)} were supplied. Page the call instead.")
        item_spec = spec.get("items")
        if isinstance(item_spec, dict):
            return [_check(v, item_spec, f"{path}[{i}]")
                    for i, v in enumerate(value)]

    return value


def validate_arguments(input_schema: dict, arguments: Any) -> dict:
    """Validate + apply defaults.  Raises :class:`UnknownProperty` / :class:`ArgumentError`."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ArgumentError("'arguments' must be a JSON object.")
    properties = (input_schema or {}).get("properties") or {}

    if (input_schema or {}).get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise UnknownProperty(
                f"Unknown argument(s): {', '.join(unknown)}. "
                f"Accepted: {', '.join(sorted(properties)) or '(none)'}.")

    out: dict[str, Any] = {}
    for key, value in arguments.items():
        spec = properties.get(key, {})
        out[key] = _check(value, spec, key)

    for key in (input_schema or {}).get("required") or []:
        if key not in out:
            raise ArgumentError(f"Missing required argument '{key}'.")

    for key, spec in properties.items():
        if key not in out and isinstance(spec, dict) and "default" in spec:
            out[key] = spec["default"]
    return out


# ---------------------------------------------------------------------------
# Dispatcher - shared by both transports
# ---------------------------------------------------------------------------

PAGE_SIZE = 100

IGNORED_NOTIFICATIONS = frozenset({
    "notifications/cancelled", "notifications/progress",
    "notifications/roots/list_changed",
})


def _cursor_offset(cursor: Any) -> int:
    if cursor in (None, ""):
        return 0
    try:
        offset = int(str(cursor))
    except (TypeError, ValueError) as exc:
        raise RpcError(INVALID_PARAMS, f"Invalid cursor {cursor!r}.") from exc
    if offset < 0:
        raise RpcError(INVALID_PARAMS, f"Invalid cursor {cursor!r}.")
    return offset


def _page(items: list, cursor: Any, key: str) -> dict:
    offset = _cursor_offset(cursor)
    window = items[offset:offset + PAGE_SIZE]
    out: dict[str, Any] = {key: window}
    if offset + len(window) < len(items):
        out["nextCursor"] = str(offset + len(window))
    return out


def tool_error(message: str, structured: dict | None = None) -> dict:
    """A tool-level failure: a SUCCESSFUL result carrying ``isError: true``."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": message}],
                              "isError": True}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def _progress_callable(params: dict, sink: Any):
    """Bind ``notifications/progress`` to the token the client supplied."""
    if sink is None:
        return None
    meta = params.get("_meta")
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    if token is None:
        return None

    def emit(progress: float, total: float | None = None,
             message: str | None = None) -> None:
        payload: dict[str, Any] = {"progressToken": token, "progress": progress}
        if total:
            payload["total"] = total
        if message:
            payload["message"] = message
        sink({"jsonrpc": "2.0", "method": "notifications/progress",
              "params": payload})

    return emit


class Dispatcher:
    """Routes one decoded JSON-RPC message to a result, an error, or nothing."""

    def __init__(self, transport: str = "http", *,
                 force_read_only: bool = False) -> None:
        self.transport = transport
        self.force_read_only = bool(force_read_only)

    # -- posture ----------------------------------------------------------
    def read_only(self) -> bool:
        if self.force_read_only:
            return True
        try:
            from ..core import config_service

            return bool(config_service.get_config().mcp_read_only)
        except Exception as exc:  # noqa: BLE001 - never fail a call on a config read
            log.warning("mcp: could not read mcp_read_only (%s); assuming false", exc)
            return False

    # -- entry point ------------------------------------------------------
    def handle(self, msg: Any, session: Session | None, *,
               progress_sink: Any = None) -> dict | None:
        """Return the JSON-RPC response, or ``None`` for a notification."""
        try:
            validate_envelope(msg)
        except RpcError as exc:
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            return rpc_error(msg_id, exc.code, exc.message, exc.data)

        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params")
        if method is None:
            # A JSON-RPC *response* posted back to us; nothing to answer.
            return None
        if params is not None and not isinstance(params, (dict, list)):
            return rpc_error(msg_id, INVALID_PARAMS, "'params' must be an object.")
        params = params if isinstance(params, dict) else {}

        notification = msg_id is None
        try:
            payload = self._route(method, params, session,
                                  progress_sink=progress_sink)
        except RpcError as exc:
            if notification:
                log.info("mcp: notification %s rejected: %s", method, exc.message)
                return None
            return rpc_error(msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:
            log.exception("mcp: unhandled error in %s", method)
            if notification:
                return None
            return rpc_error(msg_id, INTERNAL_ERROR,
                             f"Internal error (request {msg_id}); see server log.",
                             {"detail": type(exc).__name__})
        if notification:
            return None
        return rpc_result(msg_id, payload if payload is not None else {})

    # -- routing ----------------------------------------------------------
    def _route(self, method: str, params: dict, session: Session | None, *,
               progress_sink: Any = None) -> Any:
        if method == "initialize":
            return self._initialize(params, session)
        if method == "ping":
            return {}
        if method == "notifications/initialized":
            if session is not None:
                session.initialized = True
            return None
        if method in IGNORED_NOTIFICATIONS:
            return None

        if session is None:
            raise RpcError(INVALID_SESSION, "No MCP session; call 'initialize' first.")
        if not session.initialized:
            raise RpcError(NOT_INITIALIZED,
                           "Server not initialized: send the "
                           "'notifications/initialized' notification after "
                           "'initialize' before calling anything else.")

        if method == "tools/list":
            return self._tools_list(params)
        if method == "tools/call":
            return self._tools_call(params, session, progress_sink=progress_sink)
        if method == "resources/list":
            return self._resources_list(params)
        if method == "resources/templates/list":
            return self._resource_templates_list(params)
        if method == "resources/read":
            return self._resources_read(params, session)
        if method == "prompts/list":
            return self._prompts_list(params)
        if method == "prompts/get":
            return self._prompts_get(params)
        if method == "logging/setLevel":
            return self._set_level(params, session)
        if method in ("completion/complete", "completions/complete"):
            return self._complete(params, session)
        raise RpcError(METHOD_NOT_FOUND, f"Method '{method}' is not supported.")

    # -- handshake --------------------------------------------------------
    def _initialize(self, params: dict, session: Session | None) -> dict:
        version = negotiate(params.get("protocolVersion"))
        if session is not None:
            session.protocol_version = version
            info = params.get("clientInfo")
            session.client_info = info if isinstance(info, dict) else {}
            caps = params.get("capabilities")
            session.client_capabilities = caps if isinstance(caps, dict) else {}
        return initialize_result(version, read_only=self.read_only())

    # -- tools ------------------------------------------------------------
    def _tools_list(self, params: dict) -> dict:
        from . import registry

        return _page([t.as_dict() for t in registry.TOOLS], params.get("cursor"),
                     "tools")

    def _tools_call(self, params: dict, session: Session, *,
                    progress_sink: Any = None) -> dict:
        from . import handlers, registry

        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RpcError(INVALID_PARAMS,
                           "'params.name' must be the tool name (a string).")
        raw_arguments = params.get("arguments")
        if raw_arguments is not None and not isinstance(raw_arguments, dict):
            raise RpcError(INVALID_PARAMS, "'params.arguments' must be an object.")

        tool = registry.BY_NAME.get(name)
        if tool is None:
            hint = registry.ALIAS_HINTS.get(name)
            message = f"Unknown tool '{name}'."
            message += (f" Did you mean '{hint}'?" if hint else "")
            message += (f" Call tools/list for the {len(registry.TOOLS)} tools this "
                        "server exposes.")
            return tool_error(message)

        retry_after = session.rate_limited()
        if retry_after:
            return tool_error(
                f"Rate limit reached ({RATE_LIMIT_CALLS} tool calls per minute). "
                f"Retry in {retry_after} ms.",
                structured={"retry_after_ms": retry_after})

        read_only = self.read_only()
        if tool.mutating and read_only:
            return tool_error(
                f"'{name}' modifies the vault and this server is running in read-only "
                "mode (mcp_read_only=true / --read-only). Nothing was changed.")

        try:
            arguments = validate_arguments(tool.input_schema, raw_arguments)
        except UnknownProperty as exc:
            # MCP_SPEC 4: a hallucinated argument key is a hard protocol error.
            raise RpcError(INVALID_PARAMS, str(exc)) from exc
        except ArgumentError as exc:
            return tool_error(str(exc))

        ctx = handlers.Ctx(transport=self.transport, session_id=session.id,
                           read_only=read_only, request_id=params.get("_meta"),
                           progress=_progress_callable(params, progress_sink))

        started = time.perf_counter()
        outcome, error_code, structured = "ok", None, None
        try:
            text, structured = handlers.HANDLERS[tool.handler](arguments, ctx)
            is_error = False
        except handlers.ToolFailure as exc:
            text, is_error, outcome = exc.message, True, "error"
            error_code, structured = exc.code, None
        except Exception as exc:
            log.exception("mcp: tool %s failed", name)
            text = f"Internal error (request {session.id[:8]}/{name}); see server log."
            is_error, outcome, error_code = True, "error", type(exc).__name__
            structured = None

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if tool.audited:
            handlers.write_audit(tool, arguments, ctx, outcome=outcome,
                                 payload=structured, error_code=error_code,
                                 elapsed_ms=elapsed_ms)
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}],
                                  "isError": is_error}
        if not is_error and structured is not None:
            result["structuredContent"] = structured
        # MCP_SPEC 9: argument VALUES are never logged for a read tool.
        log.info("mcp tool=%s session=%s args=%s elapsed_ms=%s is_error=%s",
                 name, session.id[:8], sorted(arguments), elapsed_ms, is_error)
        return result

    # -- resources --------------------------------------------------------
    def _resources_list(self, params: dict) -> dict:
        from . import resources

        return _page(list(resources.RESOURCES), params.get("cursor"), "resources")

    def _resource_templates_list(self, params: dict) -> dict:
        from . import resources

        return _page(list(resources.RESOURCE_TEMPLATES), params.get("cursor"),
                     "resourceTemplates")

    def _resources_read(self, params: dict, session: Session) -> dict:
        from . import handlers, resources

        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise RpcError(INVALID_PARAMS, "'params.uri' is required.")
        ctx = handlers.Ctx(transport=self.transport, session_id=session.id,
                           read_only=self.read_only())
        try:
            entry = resources.read(uri, ctx)
        except resources.ResourceNotFound as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from exc
        except handlers.ToolFailure as exc:
            raise RpcError(INVALID_PARAMS, exc.message) from exc
        return {"contents": [entry]}

    # -- prompts ----------------------------------------------------------
    def _prompts_list(self, params: dict) -> dict:
        from . import prompts

        return _page(list(prompts.PROMPTS), params.get("cursor"), "prompts")

    def _prompts_get(self, params: dict) -> dict:
        from . import prompts

        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RpcError(INVALID_PARAMS, "'params.name' is required.")
        arguments = params.get("arguments")
        try:
            return prompts.get(name, arguments if isinstance(arguments, dict) else {})
        except prompts.PromptNotFound as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from exc

    # -- misc -------------------------------------------------------------
    def _set_level(self, params: dict, session: Session) -> dict:
        level = str(params.get("level") or "").lower()
        if level not in LOG_LEVELS:
            raise RpcError(INVALID_PARAMS,
                           f"'level' must be one of {', '.join(LOG_LEVELS)}.")
        session.log_level = level
        return {}

    def _complete(self, params: dict, session: Session) -> dict:
        from . import handlers, prompts

        argument = params.get("argument")
        if not isinstance(argument, dict) or not argument.get("name"):
            raise RpcError(INVALID_PARAMS, "'params.argument' must be {name, value}.")
        ctx = handlers.Ctx(transport=self.transport, session_id=session.id,
                           read_only=self.read_only())
        return prompts.complete(str(argument.get("name")),
                                str(argument.get("value") or ""), ctx)
