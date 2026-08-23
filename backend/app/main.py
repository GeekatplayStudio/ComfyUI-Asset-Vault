"""Application wiring: lifespan, middleware, v1 routers, static SPA, bind guard.

Binds **127.0.0.1:8127** (DECISIONS D1).  A non-loopback host is refused at
startup unless ``ALLOW_LAN=1`` is set explicitly, and then it is logged loudly
(ARCHITECTURE 8.4).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as buildcfg
from .api import middleware as api_middleware
from .api.deps import require_vault_request
from .api.schemas.common import BASE_ERRORS
from .api.schemas.system import Pong
from .api.v1 import (
    ai_router,
    albums_router,
    comfyui_router,
    embeddings_router,
    enable_router,
    fileops_router,
    files_router,
    hash_router,
    index_router,
    mcp_router,
    models_router,
    nodes_router,
    outputs_router,
    search_router,
    storage_router,
    system_router,
    tags_router,
    workflows_router,
)
from .core import db as dbmod
from .core import shutdown as core_shutdown
from .core import startup as core_startup

logging.basicConfig(
    level=os.environ.get("VAULT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("vault")

DEFAULT_HOST = "127.0.0.1"
DEV_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000",
               "http://localhost:5173", "http://127.0.0.1:5173"]

FRONTEND_DIST = buildcfg.BACKEND_DIR.parent / "frontend" / "dist"
SPA_INDEX = FRONTEND_DIST / "index.html"


def resolve_bind(host: str | None = None, port: int | None = None) -> tuple[str, int]:
    """Loopback unless ``ALLOW_LAN=1``.  Raises rather than binding the LAN."""
    host = host or os.environ.get("VAULT_HOST") or DEFAULT_HOST
    port = int(port or os.environ.get("VAULT_PORT") or buildcfg.DEFAULT_PORT)
    if host not in ("127.0.0.1", "localhost", "::1"):
        if os.environ.get("ALLOW_LAN") != "1":
            raise SystemExit(
                f"Refusing to bind {host}: this app is loopback-only. "
                "Set ALLOW_LAN=1 to override (and understand the risk).")
        log.warning("=" * 72)
        log.warning("ALLOW_LAN=1 - binding %s:%s. The vault is reachable from the "
                    "network with no authentication.", host, port)
        log.warning("=" * 72)
    return host, port


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("%s %s - %s", buildcfg.APP_NAME, buildcfg.VERSION, buildcfg.AUTHOR)
    report = core_startup()
    log.info("startup: configured=%s path=%s migrations=%s auto_reindex=%s",
             report.get("configured"), report.get("comfyui_path"),
             report.get("migrations"), report.get("auto_reindex"))
    for warning in report.get("warnings") or []:
        log.warning("startup: %s", warning)
    try:
        yield
    finally:
        core_shutdown()
        log.info("shutdown complete")


app = FastAPI(
    title=buildcfg.APP_NAME,
    version=buildcfg.VERSION,
    summary=f"{buildcfg.AUTHOR} - local ComfyUI model, node, workflow and output vault.",
    description=(
        "Local-first REST API over an indexed ComfyUI installation. "
        "Every mutating request must send `X-Vault-Request: 1`. "
        "Every non-2xx response uses the stable error envelope."
    ),
    lifespan=lifespan,
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url=None,
)

api_middleware.install(app)

# CORS is for the Vite dev server only.  When the built SPA is served from this
# same origin there is nothing cross-origin left to allow (ARCHITECTURE 8.4).
if os.environ.get("VAULT_DEV") == "1" or not SPA_INDEX.is_file():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Vault-Request", "If-None-Match", "Range"],
        expose_headers=["X-Request-Id", "X-API-Version", "X-Thumb-Source",
                        "Content-Range", "Accept-Ranges", "ETag", "Server-Timing"],
    )

# --- v1 -----------------------------------------------------------------------
v1 = APIRouter(prefix=buildcfg.API_PREFIX,
               dependencies=[Depends(require_vault_request)])

v1.include_router(system_router.router)
v1.include_router(storage_router.router)
v1.include_router(comfyui_router.router)
v1.include_router(index_router.router)
v1.include_router(models_router.router)
v1.include_router(nodes_router.packages_router)
v1.include_router(nodes_router.classes_router)
v1.include_router(workflows_router.router)
v1.include_router(enable_router.workflow_router)
v1.include_router(enable_router.router)
v1.include_router(outputs_router.router)
v1.include_router(search_router.router)
v1.include_router(embeddings_router.router)
v1.include_router(hash_router.router)
v1.include_router(files_router.router)
v1.include_router(fileops_router.router)
v1.include_router(albums_router.router)
v1.include_router(tags_router.router)
v1.include_router(ai_router.router)
# The MCP *activity log*.  The JSON-RPC transport on `/api/v1/mcp` itself is
# mounted further down from `app/mcp/http.py`; this router owns only `/audit`.
v1.include_router(mcp_router.router)


@v1.get("/ping", response_model=Pong, tags=["System"], responses=BASE_ERRORS,
        summary="Boot-retry probe for the frontend")
def ping() -> dict:
    return {"pong": True, "t": dbmod.now_ms()}


app.include_router(v1)

# `/api/v1/mcp` is reserved for the Wave 3 `mcp` agent, which owns
# `app/mcp/http.py`.  Guarded so the app still starts before that module lands.
try:  # pragma: no cover - exercised once the mcp agent ships its module
    from .mcp import http as mcp_http

    app.include_router(mcp_http.router)
    log.info("MCP HTTP transport mounted at %s/mcp", buildcfg.API_PREFIX)
except ImportError:
    log.info("MCP HTTP transport not present yet; %s/mcp is reserved.",
             buildcfg.API_PREFIX)


# --- static SPA ---------------------------------------------------------------
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")),
              name="assets")


@app.get("/", include_in_schema=False)
def index() -> object:
    if SPA_INDEX.is_file():
        return FileResponse(str(SPA_INDEX), media_type="text/html")
    return JSONResponse({
        "app": buildcfg.APP_NAME,
        "author": buildcfg.AUTHOR,
        "version": buildcfg.VERSION,
        "api": buildcfg.API_PREFIX,
        "docs": "/docs",
        "note": "development banner - build the frontend to serve the SPA here",
    })


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> object:
    icon = FRONTEND_DIST / "favicon.ico"
    if icon.is_file():
        return FileResponse(str(icon))
    return JSONResponse(status_code=404, content={})


def run() -> None:  # pragma: no cover - process entry point
    import uvicorn

    host, port = resolve_bind()
    log.info("Listening on http://%s:%s (docs at /docs)", host, port)
    uvicorn.run("app.main:app", host=host, port=port, reload=False,
                log_level=os.environ.get("VAULT_LOG_LEVEL", "info").lower())


if __name__ == "__main__":  # pragma: no cover
    run()
