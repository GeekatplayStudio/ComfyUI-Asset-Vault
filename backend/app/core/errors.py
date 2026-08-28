"""Stable error-code vocabulary and the AppError hierarchy."""

from __future__ import annotations

import errno

# --- scan / parse error codes (ARCHITECTURE 3.6) ------------------------------
HEADER_INVALID = "HEADER_INVALID"
HEADER_TOO_LARGE = "HEADER_TOO_LARGE"
NOT_A_MODEL = "NOT_A_MODEL"
JSON_INVALID = "JSON_INVALID"
IMAGE_UNREADABLE = "IMAGE_UNREADABLE"
PERMISSION_DENIED = "PERMISSION_DENIED"
FILE_LOCKED = "FILE_LOCKED"
PATH_TOO_LONG = "PATH_TOO_LONG"
AST_SYNTAX_ERROR = "AST_SYNTAX_ERROR"
ENCODING_ERROR = "ENCODING_ERROR"
REPARSE_POINT_SKIPPED = "REPARSE_POINT_SKIPPED"
UNKNOWN = "UNKNOWN"

SCAN_ERROR_CODES = frozenset({
    HEADER_INVALID, HEADER_TOO_LARGE, NOT_A_MODEL, JSON_INVALID, IMAGE_UNREADABLE,
    PERMISSION_DENIED, FILE_LOCKED, PATH_TOO_LONG, AST_SYNTAX_ERROR, ENCODING_ERROR,
    REPARSE_POINT_SKIPPED, UNKNOWN,
})

# --- API error codes (API_CONTRACT 0.2) ---------------------------------------
VALIDATION_ERROR = "VALIDATION_ERROR"
NOT_FOUND = "NOT_FOUND"
CONFLICT = "CONFLICT"
PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
FEATURE_UNAVAILABLE = "FEATURE_UNAVAILABLE"
RATE_LIMITED = "RATE_LIMITED"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
SEARCH_SYNTAX = "SEARCH_SYNTAX"
NOT_CONFIGURED = "NOT_CONFIGURED"
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
INSUFFICIENT_SPACE = "INSUFFICIENT_SPACE"
INTEGRITY_MISMATCH = "INTEGRITY_MISMATCH"
INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    """Base class for every error the app raises deliberately."""

    code = INTERNAL_ERROR
    http_status = 500

    def __init__(self, message: str, *, details: dict | None = None,
                 code: str | None = None, http_status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(AppError):
    code = VALIDATION_ERROR
    http_status = 422


class NotFoundError(AppError):
    code = NOT_FOUND
    http_status = 404


class ConflictError(AppError):
    code = CONFLICT
    http_status = 409


class PathNotAllowed(AppError):
    code = PATH_NOT_ALLOWED
    http_status = 403


class FeatureUnavailable(AppError):
    code = FEATURE_UNAVAILABLE
    http_status = 503


class RateLimited(AppError):
    code = RATE_LIMITED
    http_status = 429


class PayloadTooLarge(AppError):
    code = PAYLOAD_TOO_LARGE
    http_status = 413


class SearchSyntaxError(AppError):
    code = SEARCH_SYNTAX
    http_status = 400


class NotConfigured(AppError):
    code = NOT_CONFIGURED
    http_status = 409


class UpstreamUnavailable(AppError):
    """An allowlisted host refused, redirected somewhere it may not, or died."""

    code = UPSTREAM_UNAVAILABLE
    http_status = 502


class InsufficientSpace(AppError):
    """SECURITY_REVIEW R6 - the target volume cannot hold what was asked for."""

    code = INSUFFICIENT_SPACE
    http_status = 507


class IntegrityMismatch(AppError):
    """SECURITY_REVIEW R4 - what arrived is not what the source advertised."""

    code = INTEGRITY_MISMATCH
    http_status = 502


def classify_os_error(exc: BaseException) -> str:
    """Map an OS-level exception onto the stable scan error vocabulary."""
    if isinstance(exc, PermissionError):
        return PERMISSION_DENIED
    if isinstance(exc, UnicodeDecodeError):
        return ENCODING_ERROR
    if isinstance(exc, SyntaxError):
        return AST_SYNTAX_ERROR
    if isinstance(exc, ValueError) and "json" in type(exc).__name__.lower():
        return JSON_INVALID
    winerr = getattr(exc, "winerror", None)
    if winerr in (32, 33):
        return FILE_LOCKED
    if winerr in (3, 206):
        return PATH_TOO_LONG
    if isinstance(exc, OSError):
        if exc.errno == errno.EACCES:
            return PERMISSION_DENIED
        if exc.errno == errno.ENAMETOOLONG:  # 36 on Linux, 63 on macOS
            return PATH_TOO_LONG
    return UNKNOWN
