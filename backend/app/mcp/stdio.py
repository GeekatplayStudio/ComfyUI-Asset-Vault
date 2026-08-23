"""stdio transport - newline-delimited JSON-RPC on stdin/stdout.

**Nothing but JSON-RPC is ever written to stdout.**  ``app/mcp_stdio.py``
reconfigures logging to stderr and re-points ``sys.stdout`` at stderr *before*
importing this module, then hands the real stdout handle to :func:`serve`.
That is the single most common way a stdio MCP server breaks, so it is
enforced structurally rather than by convention.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, TextIO

from .protocol import (
    INVALID_REQUEST,
    PARSE_ERROR,
    SESSIONS,
    Dispatcher,
    rpc_error,
)

log = logging.getLogger("vault.mcp")

USAGE = """geekatplay-comfyui-vault MCP server (stdio transport)

  python -m app.mcp_stdio [--read-only]

  --read-only   Refuse every mutating tool for this process.  Mutation is ON by
                default (DECISIONS C5); mcp_read_only=true in the app config has
                the same effect persistently.
"""


class _Writer:
    """Serializes JSON-RPC messages onto the one real stdout handle."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    def send(self, payload: Any) -> None:
        line = json.dumps(payload, ensure_ascii=False, default=str,
                          separators=(",", ":"))
        self.stream.write(line + "\n")
        self.stream.flush()


def serve(argv: list[str] | None = None, *, stdin: TextIO | None = None,
          stdout: TextIO | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--help" in argv or "-h" in argv:
        sys.stderr.write(USAGE)
        return 0
    read_only = "--read-only" in argv
    unknown = [a for a in argv if a not in ("--read-only",)]
    if unknown:
        sys.stderr.write(f"Unknown argument(s): {', '.join(unknown)}\n{USAGE}")
        return 2

    stdin = stdin or sys.stdin
    writer = _Writer(stdout or sys.__stdout__)
    dispatcher = Dispatcher(transport="stdio", force_read_only=read_only)
    session = SESSIONS.create("stdio")

    log.info("mcp stdio ready (read_only=%s, session=%s)", read_only,
             session.id[:8])

    while True:
        try:
            line = stdin.readline()
        except KeyboardInterrupt:  # pragma: no cover - Ctrl-C on a console
            return 0
        except (OSError, ValueError) as exc:
            log.info("mcp stdio: stdin closed (%s)", exc)
            return 0
        if not line:
            log.info("mcp stdio: EOF, shutting down")
            return 0
        line = line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except ValueError as exc:
            writer.send(rpc_error(None, PARSE_ERROR, f"Parse error: {exc}"))
            continue

        batch = isinstance(payload, list)
        messages = list(payload) if batch else [payload]
        if batch and not messages:
            writer.send(rpc_error(None, INVALID_REQUEST, "A batch must not be empty."))
            continue

        responses: list[dict] = []
        for msg in messages:
            answer = dispatcher.handle(msg, session, progress_sink=writer.send)
            if answer is not None:
                responses.append(answer)
        if not responses:
            continue  # notifications produce no output at all
        writer.send(responses if batch else responses[0])


def shutdown() -> None:
    """Best-effort release of the DB connections this process opened."""
    try:
        from ..core import db as dbmod

        dbmod.close_thread_connections()
        dbmod.shutdown_writer()
    except Exception as exc:  # noqa: BLE001 - shutdown is best effort
        log.debug("mcp stdio shutdown: %s", exc)
