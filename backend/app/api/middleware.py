"""Error envelope, request id, API version and timing.

API_CONTRACT 0.1 requires **every** non-2xx response to carry the same envelope,
including FastAPI's own validation failures.  That is why the handlers below are
registered for ``RequestValidationError`` and bare ``Exception`` as well as for
the application's own ``AppError`` hierarchy: there is no code path that can
return a naked FastAPI error body, and none that can leak a traceback.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..core.errors import AppError

log = logging.getLogger("vault.api")

API_VERSION = "1"

# --- API_CONTRACT 0.2 error-code registry ------------------------------------
ERROR_STATUS: dict[str, int] = {
    "VALIDATION_ERROR": 422,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "PATH_NOT_ALLOWED": 403,
    "PATH_INVALID": 422,
    "FILE_LOCKED": 423,
    "FILE_MISSING": 410,
    "NOT_CONFIGURED": 409,
    "ROOT_UNAVAILABLE": 503,
    "JOB_ALREADY_RUNNING": 409,
    "JOB_NOT_FOUND": 404,
    "FEATURE_UNAVAILABLE": 503,
    "UPSTREAM_UNAVAILABLE": 502,
    "RATE_LIMITED": 429,
    "SEARCH_SYNTAX": 422,
    "CSRF_HEADER_MISSING": 400,
    "PAYLOAD_TOO_LARGE": 413,
    "INSUFFICIENT_SPACE": 507,
    "INTEGRITY_MISMATCH": 502,
    "INTERNAL": 500,
}

RETRYABLE_CODES = frozenset({
    "UPSTREAM_UNAVAILABLE", "RATE_LIMITED", "ROOT_UNAVAILABLE", "FILE_LOCKED",
})

#: ``core/errors.py`` predates the frozen contract vocabulary in a few places
#: (scan-phase codes, ``INTERNAL_ERROR``).  Translate here rather than in every
#: router so clients only ever branch on registry codes.
CORE_CODE_MAP: dict[str, str] = {
    "INTERNAL_ERROR": "INTERNAL",
    "PERMISSION_DENIED": "PATH_NOT_ALLOWED",
    "PATH_TOO_LONG": "PATH_INVALID",
    "HEADER_TOO_LARGE": "PAYLOAD_TOO_LARGE",
    "UNKNOWN": "INTERNAL",
    "HEADER_INVALID": "INTERNAL",
    "NOT_A_MODEL": "INTERNAL",
    "JSON_INVALID": "INTERNAL",
    "IMAGE_UNREADABLE": "INTERNAL",
    "ENCODING_ERROR": "INTERNAL",
    "AST_SYNTAX_ERROR": "INTERNAL",
}

STATUS_CODE_FALLBACK: dict[int, str] = {
    400: "VALIDATION_ERROR",
    403: "PATH_NOT_ALLOWED",
    404: "NOT_FOUND",
    405: "VALIDATION_ERROR",
    409: "CONFLICT",
    410: "FILE_MISSING",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    423: "FILE_LOCKED",
    429: "RATE_LIMITED",
    502: "UPSTREAM_UNAVAILABLE",
    503: "FEATURE_UNAVAILABLE",
}


def normalize_code(code: str | None, *, default: str = "INTERNAL") -> str:
    if not code:
        return default
    code = CORE_CODE_MAP.get(code, code)
    return code if code in ERROR_STATUS else default


class ApiError(AppError):
    """An error raised by the HTTP layer with a contract error code."""

    def __init__(self, code: str, message: str, *, details: dict | None = None,
                 field_errors: list[dict] | None = None,
                 status: int | None = None, retryable: bool | None = None) -> None:
        code = normalize_code(code)
        super().__init__(message, details=details, code=code,
                         http_status=status or ERROR_STATUS[code])
        self.field_errors = field_errors or []
        self.retryable = RETRYABLE_CODES.__contains__(code) if retryable is None \
            else bool(retryable)


def field_error(field: str, message: str) -> ApiError:
    """The canonical 422 shape: ``field_errors[0].field == '<param>'``."""
    return ApiError("VALIDATION_ERROR", f"{field}: {message}",
                    field_errors=[{"field": field, "message": message}])


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------

def request_id_of(request: Request | None) -> str:
    if request is not None:
        rid = getattr(request.state, "request_id", None)
        if rid:
            return str(rid)
    return uuid.uuid4().hex


def envelope(code: str, message: str, request_id: str, *,
             details: dict | None = None, field_errors: list[dict] | None = None,
             retryable: bool | None = None) -> dict[str, Any]:
    code = normalize_code(code)
    err: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
        "retryable": bool(code in RETRYABLE_CODES if retryable is None else retryable),
        "docs": f"/docs#{code.lower()}",
    }
    if details:
        err["details"] = details
    if field_errors:
        err["field_errors"] = field_errors
    return {"error": err}


def error_response(code: str, message: str, request: Request | None = None, *,
                   status: int | None = None, details: dict | None = None,
                   field_errors: list[dict] | None = None,
                   retryable: bool | None = None) -> JSONResponse:
    code = normalize_code(code)
    rid = request_id_of(request)
    body = envelope(code, message, rid, details=details, field_errors=field_errors,
                    retryable=retryable)
    return JSONResponse(
        status_code=status or ERROR_STATUS[code],
        content=body,
        headers={"X-API-Version": API_VERSION, "X-Request-Id": rid},
    )


# ---------------------------------------------------------------------------
# ASGI middleware (pure ASGI: no BaseHTTPMiddleware, so SSE streams unbuffered)
# ---------------------------------------------------------------------------

class RequestContextMiddleware:
    """Attach ``X-Request-Id`` / ``X-API-Version`` / ``Server-Timing`` to every
    response and expose the request id on ``request.state`` for the handlers."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        started = time.perf_counter()

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "x-request-id" not in headers:
                    headers.append("X-Request-Id", rid)
                if "x-api-version" not in headers:
                    headers.append("X-API-Version", API_VERSION)
                headers.append(
                    "Server-Timing",
                    f"app;dur={(time.perf_counter() - started) * 1000:.1f}")
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

def _flatten_loc(loc) -> str:
    parts = [str(p) for p in (loc or []) if p not in ("body", "query", "path", "header")]
    return ".".join(parts) or "body"


async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.code, exc.message, request, status=exc.http_status,
                          details=exc.details, field_errors=exc.field_errors,
                          retryable=exc.retryable)


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    code = normalize_code(exc.code)
    return error_response(code, exc.message, request, details=exc.details)


async def _validation_handler(request: Request,
                              exc: RequestValidationError) -> JSONResponse:
    field_errors = [
        {"field": _flatten_loc(e.get("loc")), "message": str(e.get("msg", "invalid"))}
        for e in exc.errors()
    ]
    first = field_errors[0]["field"] if field_errors else "body"
    return error_response(
        "VALIDATION_ERROR", f"{first}: {field_errors[0]['message'] if field_errors else 'invalid request'}",
        request, field_errors=field_errors)


async def _http_exception_handler(request: Request,
                                  exc: StarletteHTTPException) -> JSONResponse:
    status = int(exc.status_code)
    if 200 <= status < 400:  # redirects and friends keep their own body
        return JSONResponse(status_code=status, content={"detail": exc.detail})
    code = STATUS_CODE_FALLBACK.get(status, "INTERNAL" if status >= 500 else "VALIDATION_ERROR")
    detail = exc.detail
    message = detail if isinstance(detail, str) else "Request failed."
    details = detail if isinstance(detail, dict) else None
    return error_response(code, message, request, status=status, details=details)


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = request_id_of(request)
    # The traceback goes to the log, never to the client (API_CONTRACT 0.1).
    log.exception("request_id=%s unhandled error on %s %s", rid, request.method,
                  request.url.path)
    return error_response(
        "INTERNAL",
        "An unexpected internal error occurred. Quote the request_id when reporting it.",
        request, details={"request_id": rid})


def install(app: FastAPI) -> None:
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
