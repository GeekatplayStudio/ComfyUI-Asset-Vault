"""MCP server for the Geekatplay ComfyUI Asset Vault.

Protocol revision ``2025-06-18``.  Two transports over one tool registry:

* ``app/mcp/http.py``  - Streamable HTTP at ``/api/v1/mcp`` (POST / GET / DELETE)
* ``app/mcp/stdio.py`` - newline-delimited JSON-RPC, launched by
  ``python -m app.mcp_stdio``

``protocol.py`` owns the JSON-RPC envelope, version negotiation, sessions and
the transport-agnostic :class:`~app.mcp.protocol.Dispatcher`; ``registry.py``
is pure tool data; ``handlers.py`` is the only place that touches the service
layer - and it contains no SQL, by design (BUILD_PLAN 4).

Importing this package must stay cheap: ``http`` (FastAPI) is deliberately NOT
imported here so the stdio transport keeps its sub-second cold start.
"""

from __future__ import annotations

__all__ = ["SERVER_NAME", "SERVER_VERSION", "SESSIONS", "Dispatcher"]


def __getattr__(name: str):  # pragma: no cover - thin lazy re-export
    if name in __all__:
        from . import protocol

        return getattr(protocol, name)
    raise AttributeError(name)
