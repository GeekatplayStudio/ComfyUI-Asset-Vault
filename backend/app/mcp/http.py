"""Streamable HTTP transport - ``POST``/``GET``/``DELETE`` on ``/api/v1/mcp``.

Mounted by ``app/main.py`` with a single ``include_router`` line; that is the
only cross-boundary touch agreed in BUILD_PLAN 4.

Security posture (MCP_SPEC 9, amended by DECISIONS C5):
  * loopback bind is inherited from uvicorn;
  * ``Origin`` is validated on every request (DNS-rebinding is the canonical
    attack against a loopback MCP server);
  * with ``ALLOW_LAN=1`` a bearer token is mandatory, and without one the
    router refuses to mount at all;
  * mutation is ON by default and switched off with ``mcp_read_only=true``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import threading
import time
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config import API_PREFIX, BACKEND_DIR, DEFAULT_PORT
from . import protocol
from .protocol import SESSIONS, Dispatcher

log = logging.getLogger("vault.mcp")

router = APIRouter(prefix=f"{API_PREFIX}/mcp", tags=["MCP"])

SESSION_HEADER = "mcp-session-id"
VERSION_HEADER = "mcp-protocol-version"
CSRF_HEADER = "x-vault-request"

# --- S-02: the browser-reachability hole, closed three ways -------------------
# A page served by ComfyUI (which runs custom-node JavaScript from 34 third-party
# packages on this machine) used to be able to POST here as a CORS *simple
# request* and reach vault_delete.  Three independent gaps made that possible and
# all three are now shut:
#
#   1. ORIGIN_OK accepted any loopback port, so http://127.0.0.1:8188 - ComfyUI's
#      own origin - passed.  It now accepts only this app's own origin(s).
#   2. POST ignored Content-Type, so text/plain qualified as a simple request and
#      never triggered a preflight.  application/json is now mandatory.
#   3. The endpoint sat outside the X-Vault-Request CSRF dependency that guards
#      every other mutating route.  It is now enforced here too.
#
# Either (2) or (3) alone forces a CORS preflight that this app never approves for
# a foreign origin; (1) rejects the request outright. stdio is untouched by all of
# this - it has no origin, no headers and no browser.

APP_PORT = os.environ.get("VAULT_PORT") or str(DEFAULT_PORT)
#: The Vite dev server, allowed on exactly the same condition main.py enables CORS
#: for it, so a production install (built SPA, no VAULT_DEV) trusts nothing but
#: its own origin.
_DEV_PORTS = ("5173", "3000")
_SPA_INDEX = BACKEND_DIR.parent / "frontend" / "dist" / "index.html"
_DEV_MODE = os.environ.get("VAULT_DEV") == "1" or not _SPA_INDEX.is_file()


def _allowed_origins() -> frozenset[str]:
    hosts = ("127.0.0.1", "localhost", "[::1]")
    ports = [APP_PORT, *(_DEV_PORTS if _DEV_MODE else ())]
    return frozenset(f"http://{h}:{p}" for h in hosts for p in ports)


ALLOWED_ORIGINS = _allowed_origins()

ALLOW_LAN = os.environ.get("ALLOW_LAN") == "1"
BEARER_TOKEN = (os.environ.get("VAULT_MCP_TOKEN") or "").strip()
#: With ALLOW_LAN=1 and no token the transport is *not* mounted (MCP_SPEC 9).
MOUNTED = (not ALLOW_LAN) or bool(BEARER_TOKEN)

SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}
HEARTBEAT_S = 15.0
PROGRESS_POLL_S = 0.05

DISPATCHER = Dispatcher(transport="http")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _json(payload: Any, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=payload, headers=headers or {})


def _rpc(status: int, code: int, message: str, msg_id: Any = None,
         headers: dict | None = None) -> JSONResponse:
    return _json(protocol.rpc_error(msg_id, code, message), status, headers)


def _origin_guard(request: Request) -> JSONResponse | None:
    """Reject any Origin that is not this app's own.

    A *browser* always sends Origin on a cross-origin request, so this is the
    check that stops a page - including one served by ComfyUI on another
    loopback port - from driving the vault.  A native MCP client sends no
    Origin at all and is unaffected.
    """
    origin = (request.headers.get("origin") or "").strip()
    if origin and origin.rstrip("/").lower() not in ALLOWED_ORIGINS:
        log.warning("mcp: rejected Origin %r from %s", origin[:120],
                    getattr(request.client, "host", "?"))
        return _json({"error": {
            "code": "PATH_NOT_ALLOWED",
            "message": "This MCP endpoint only accepts requests from the vault's "
                       "own origin. Another loopback port is not the same origin.",
            "details": {"origin": origin[:200],
                        "allowed": sorted(ALLOWED_ORIGINS)}}}, 403)
    return None


def _csrf_guard(request: Request) -> JSONResponse | None:
    """Require ``X-Vault-Request: 1``, exactly like every other mutating route.

    This header cannot be set by a cross-origin *simple* request, so demanding
    it forces a CORS preflight that this app never approves for a foreign
    origin.  Any real Streamable-HTTP MCP client already sets custom headers
    (``Accept``, ``Mcp-Session-Id``, ``MCP-Protocol-Version``), so one more
    costs a client nothing; stdio clients never see it.
    """
    if request.headers.get(CSRF_HEADER) != "1":
        return _json({"error": {
            "code": "CSRF_HEADER_MISSING",
            "message": "This MCP endpoint requires the header "
                       "'X-Vault-Request: 1' on every request. Add it to your MCP "
                       "client's HTTP headers, or use the stdio transport "
                       "(python -m app.mcp_stdio), which needs no headers.",
            "details": {"header": "X-Vault-Request"}}}, 403)
    return None


def _content_type_guard(request: Request) -> JSONResponse | None:
    """POST bodies must be declared ``application/json``.

    text/plain, form-urlencoded and multipart are the three types a browser can
    send without a preflight; refusing them removes the simple-request path.
    """
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return _json({"error": {
            "code": "VALIDATION_ERROR",
            "message": "MCP requests must be sent with "
                       "'Content-Type: application/json'.",
            "details": {"content_type": ctype or "(none)"}}}, 415)
    return None


def _auth_guard(request: Request) -> JSONResponse | None:
    if not ALLOW_LAN:
        return None
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token or token != BEARER_TOKEN:
        return _json({"error": {
            "code": "PATH_NOT_ALLOWED",
            "message": "ALLOW_LAN=1: this MCP endpoint requires "
                       "'Authorization: Bearer <token>'.",
            "details": {}}}, 403)
    return None


def _version_guard(request: Request) -> JSONResponse | None:
    version = request.headers.get(VERSION_HEADER)
    if version and version not in protocol.SUPPORTED_PROTOCOL_VERSIONS:
        return _rpc(400, protocol.INVALID_REQUEST,
                    f"Unsupported MCP-Protocol-Version {version!r}. This server "
                    f"speaks {', '.join(protocol.SUPPORTED_PROTOCOL_VERSIONS)}.")
    return None


def _guard(request: Request, *, json_body: bool = False) -> JSONResponse | None:
    return (_origin_guard(request) or _csrf_guard(request)
            or (_content_type_guard(request) if json_body else None)
            or _auth_guard(request) or _version_guard(request))


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------

def _is_initialize(messages: list) -> bool:
    return any(isinstance(m, dict) and m.get("method") == "initialize"
               for m in messages)


def _wait_call(messages: list, accept: str) -> dict | None:
    """The one long-running shape that upgrades the response to SSE.

    Only upgraded when the client actually advertised ``text/event-stream``;
    otherwise the call runs inline and answers with plain JSON, so a simple
    client that sets ``Accept: application/json`` is never handed a stream it
    cannot read.
    """
    if len(messages) != 1 or "text/event-stream" not in (accept or "").lower():
        return None
    msg = messages[0]
    if not isinstance(msg, dict) or msg.get("method") != "tools/call":
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    args = params.get("arguments")
    if not isinstance(args, dict) or not args.get("wait"):
        return None
    return msg


@router.post("", summary="MCP Streamable HTTP - client to server JSON-RPC")
async def mcp_post(request: Request) -> Response:
    blocked = _guard(request, json_body=True)
    if blocked is not None:
        return blocked

    raw = await request.body()
    if not raw:
        return _rpc(400, protocol.INVALID_REQUEST, "Empty request body.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return _rpc(400, protocol.PARSE_ERROR, f"Parse error: {exc}")

    batch = isinstance(payload, list)
    messages = list(payload) if batch else [payload]
    if batch and not messages:
        return _rpc(400, protocol.INVALID_REQUEST, "A batch must not be empty.")

    session_id = request.headers.get(SESSION_HEADER)
    session = SESSIONS.get(session_id)
    if session is None:
        if _is_initialize(messages):
            session = SESSIONS.create("http")
        else:
            return _rpc(404, protocol.INVALID_SESSION,
                        "Unknown or expired MCP session. Send 'initialize' to start "
                        "a new one and echo the Mcp-Session-Id header on every "
                        "subsequent request.",
                        headers={"Mcp-Session-Id": ""} if session_id else None)

    headers = {"Mcp-Session-Id": session.id,
               "MCP-Protocol-Version": session.protocol_version}

    streaming = _wait_call(messages, request.headers.get("accept") or "")
    if streaming is not None:
        return _sse_tool_call(streaming, session, headers)

    responses: list[dict] = []
    for msg in messages:
        answer = await run_in_threadpool(DISPATCHER.handle, msg, session)
        if answer is not None:
            responses.append(answer)

    if not responses:
        # Notifications / responses only -> 202 Accepted, empty body (MCP_SPEC 2.1).
        return Response(status_code=202, headers=headers)
    body = responses if batch else responses[0]
    return _json(body, 200, headers)


def _sse_tool_call(msg: dict, session: protocol.Session, headers: dict) -> Response:
    """Run one tool with progress and stream ``notifications/progress`` frames."""
    events: queue.Queue = queue.Queue()
    done: dict[str, Any] = {}

    def sink(notification: dict) -> None:
        events.put(notification)

    def worker() -> None:
        try:
            done["result"] = DISPATCHER.handle(msg, session, progress_sink=sink)
        except Exception as exc:
            log.exception("mcp: streaming tool call failed")
            done["result"] = protocol.rpc_error(
                msg.get("id"), protocol.INTERNAL_ERROR,
                f"Internal error (request {msg.get('id')}); see server log.",
                {"detail": type(exc).__name__})
        finally:
            events.put(None)

    thread = threading.Thread(target=worker, name="mcp-tool-call", daemon=True)
    thread.start()

    async def stream():
        last_beat = time.monotonic()
        while True:
            try:
                item = events.get_nowait()
            except queue.Empty:
                await asyncio.sleep(PROGRESS_POLL_S)
                if time.monotonic() - last_beat > HEARTBEAT_S:
                    last_beat = time.monotonic()
                    yield b": heartbeat\n\n"
                continue
            if item is None:
                break
            yield _sse_frame(item)
        result = done.get("result")
        if result is not None:
            yield _sse_frame(result)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={**SSE_HEADERS, **headers})


def _sse_frame(payload: Any) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: message\ndata: {data}\n\n".encode()


# ---------------------------------------------------------------------------
# GET - server to client notification stream
# ---------------------------------------------------------------------------

@router.get("", summary="MCP Streamable HTTP - server to client SSE stream")
async def mcp_get(request: Request) -> Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked
    accept = (request.headers.get("accept") or "").lower()
    if "text/event-stream" not in accept:
        return _rpc(406, protocol.INVALID_REQUEST,
                    "GET on this endpoint opens an SSE stream; send "
                    "'Accept: text/event-stream'.")
    session = SESSIONS.get(request.headers.get(SESSION_HEADER))
    if session is None:
        return _rpc(404, protocol.INVALID_SESSION,
                    "Unknown or expired MCP session.")

    listener: asyncio.Queue = asyncio.Queue(maxsize=256)
    session.listeners.append(listener)
    _ensure_watcher()

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(listener.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                if item is None:
                    break
                yield _sse_frame(item)
        finally:
            if listener in session.listeners:
                session.listeners.remove(listener)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={**SSE_HEADERS,
                                      "Mcp-Session-Id": session.id})


# ---------------------------------------------------------------------------
# DELETE - terminate the session
# ---------------------------------------------------------------------------

@router.delete("", summary="Terminate the MCP session")
async def mcp_delete(request: Request) -> Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked
    session_id = request.headers.get(SESSION_HEADER)
    session = SESSIONS.get(session_id)
    if session is None:
        return _rpc(404, protocol.INVALID_SESSION,
                    "Unknown or expired MCP session.")
    for listener in list(session.listeners):
        with contextlib.suppress(asyncio.QueueFull, AttributeError):
            listener.put_nowait(None)
    SESSIONS.drop(session.id)
    return Response(status_code=204)


def notify_all(notification: dict) -> int:
    """Push a server-initiated notification to every open GET stream."""
    sent = 0
    for session in SESSIONS.live():
        for listener in list(session.listeners):
            try:
                listener.put_nowait(notification)
                sent += 1
            except asyncio.QueueFull:
                log.debug("mcp: dropped notification for a slow listener")
    return sent


_WATCHER: asyncio.Task | None = None


async def _scan_watcher() -> None:
    """MCP_SPEC 3.5: fire resources/list_changed once a scan completes."""
    from ..indexing.service import get_indexer

    try:
        async for event, _payload in get_indexer().subscribe():
            if event == "done":
                notify_all({"jsonrpc": "2.0",
                            "method": "notifications/resources/list_changed"})
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a dead watcher must not kill the app
        log.info("mcp: scan watcher stopped (%s)", exc)


def _ensure_watcher() -> None:
    global _WATCHER
    if _WATCHER is None or _WATCHER.done():
        with contextlib.suppress(RuntimeError):
            _WATCHER = asyncio.get_running_loop().create_task(_scan_watcher())


if not MOUNTED:  # pragma: no cover - depends on the launch environment
    log.warning("=" * 72)
    log.warning("ALLOW_LAN=1 but VAULT_MCP_TOKEN is not set: refusing to mount "
                "%s/mcp. Generate a token and set VAULT_MCP_TOKEN to enable the "
                "MCP HTTP transport off loopback.", API_PREFIX)
    log.warning("=" * 72)
    router.routes.clear()
