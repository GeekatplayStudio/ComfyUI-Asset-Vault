"""API_CONTRACT.md section 0.1 / 0.2 -- the error envelope, as actually produced.

The contract says: *"Every non-2xx response, without exception"* carries

.. code-block:: json

    {"error": {"code": ..., "message": ..., "details": ..., "field_errors": ...,
               "request_id": ..., "retryable": ..., "docs": ...}}

with ``details`` and ``field_errors`` optional.  This module drives the real
application through ``fastapi.testclient.TestClient`` -- no server, no ComfyUI,
no network -- and asserts the promise holds on every failure path a client can
reach: an unknown route, a bad path-parameter type, a bad query-parameter type,
a malformed JSON body, a missing CSRF header, a wrong method, and a well-formed
request for an id that does not exist.

**Actual behaviour, verified here rather than assumed.**  ``app/api/middleware.py``
registers handlers for ``RequestValidationError``, ``StarletteHTTPException``,
``AppError``/``ApiError`` and bare ``Exception``.  The consequence, and the thing
this module pins down, is that FastAPI's raw ``{"detail": ...}`` body **never**
reaches a client on a 4xx/5xx: a 422 validation failure is re-shaped into the
app envelope with ``code == "VALIDATION_ERROR"`` and a populated
``field_errors[]``.  The single deliberate exception is the JSON-RPC transport
at ``/api/v1/mcp``, whose error format section 14 delegates to ``MCP_SPEC.md``;
it is named in ``JSONRPC_TRANSPORT_PATHS`` below.

The one place the raw ``{"detail": ...}`` shape survives is a 2xx/3xx
``StarletteHTTPException`` (redirects), which is not an error response and is
outside section 0.1.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

BACKEND_DIR = Path(__file__).resolve().parents[2]
API_CONTRACT_MD = BACKEND_DIR.parent / "docs" / "API_CONTRACT.md"

#: Keys section 0.1 shows and marks as always present.
REQUIRED_ENVELOPE_KEYS = frozenset({
    "code", "message", "request_id", "retryable", "docs",
})

#: Keys section 0.1 shows and explicitly allows to be omitted.
OPTIONAL_ENVELOPE_KEYS = frozenset({"details", "field_errors"})

#: Nothing else may appear inside ``error``.  A new key is an unversioned
#: contract change and must fail.
ALLOWED_ENVELOPE_KEYS = REQUIRED_ENVELOPE_KEYS | OPTIONAL_ENVELOPE_KEYS

#: Section 0 requires these on *every* response, error responses included.
REQUIRED_ERROR_HEADERS = ("X-Request-Id", "X-API-Version")

#: Paths that speak JSON-RPC 2.0 rather than the REST envelope.  Section 14 of
#: API_CONTRACT.md lists the MCP endpoint only "so no agent invents a second MCP
#: path" and defers its wire format to ``docs/MCP_SPEC.md``; a plain ``GET`` on
#: it returns ``406`` with a ``{"jsonrpc": "2.0", "error": {...}}`` body.  This
#: is a deliberate, documented deviation, not a defect.
JSONRPC_TRANSPORT_PATHS = frozenset({"/api/v1/mcp"})

#: ``AppError`` subclasses whose class-level ``http_status`` disagrees with the
#: section 0.2 registry.
#:
#: ``SearchSyntaxError`` declares ``http_status = 400`` while the registry (and
#: ``middleware.ERROR_STATUS``) says ``SEARCH_SYNTAX`` is ``422``.  The wire is
#: currently correct by accident: ``_app_error_handler`` ignores
#: ``exc.http_status`` and looks the status up in ``ERROR_STATUS``, so clients
#: really do see 422.  The stale attribute is a landmine -- the sibling
#: ``_api_error_handler`` *does* pass ``status=exc.http_status`` -- and it should
#: be corrected to 422 in ``app/core/errors.py``.
#: Owner: the backend-core agent that owns ``app/core/errors.py``; contract
#: reference API_CONTRACT.md section 0.2.
KNOWN_STATUS_DIVERGENCES: frozenset[tuple[str, str, int, int]] = frozenset({
    # (class name, error code, class attribute, registry value)
    ("SearchSyntaxError", "SEARCH_SYNTAX", 400, 422),
})

_UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")
_FENCE = "`" * 3


# ---------------------------------------------------------------------------
# Reading the contract document
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _contract_text() -> str:
    assert API_CONTRACT_MD.is_file(), f"contract document missing: {API_CONTRACT_MD}"
    return API_CONTRACT_MD.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def documented_envelope_sample() -> dict:
    """The literal JSON sample under 'Error envelope (stable, versioned)'."""
    text = _contract_text()
    marker = "### 0.1"
    assert marker in text, "API_CONTRACT.md no longer has a section 0.1"
    section = text.split(marker, 1)[1].split("### 0.2", 1)[0]
    match = re.search(rf"{_FENCE}json\n(.*?)\n{_FENCE}", section, re.DOTALL)
    assert match, "section 0.1 no longer contains a fenced json sample"
    return json.loads(match.group(1))


@lru_cache(maxsize=1)
def documented_error_registry() -> dict[str, int]:
    """Section 0.2's ``code -> HTTP status`` table."""
    text = _contract_text()
    section = text.split("### 0.2", 1)[1].split("### 0.3", 1)[0]
    registry = {
        m.group(1): int(m.group(2))
        for m in (re.match(r"\|\s*`([A-Z_]+)`\s*\|\s*(\d+)\s*\|", line)
                  for line in section.splitlines())
        if m
    }
    assert registry, "section 0.2 error-code table did not parse"
    return registry


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """A ``TestClient`` over the real app, deliberately without lifespan.

    Constructing the client outside a ``with`` block skips ``app.lifespan``, so
    no startup scan, migration or ComfyUI probe runs.  Every case below fails
    inside routing or validation, before any handler touches the database.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


VAULT_HEADERS = {"X-Vault-Request": "1"}
JSON_HEADERS = {**VAULT_HEADERS, "Content-Type": "application/json"}


def assert_envelope(response, *, expected_status: int, expected_code: str) -> dict:
    """Assert one response is a section 0.1 envelope, and return the error body."""
    where = f"{response.request.method} {response.request.url.path}"
    assert response.status_code == expected_status, (
        f"{where}: expected HTTP {expected_status}, got {response.status_code} "
        f"-- body {response.text[:300]}"
    )

    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/json"), (
        f"{where}: error responses must be JSON, got {content_type!r}"
    )

    body = response.json()
    assert set(body) == {"error"}, (
        f"{where}: the envelope must be exactly one 'error' key, got {sorted(body)}"
    )
    err = body["error"]
    assert isinstance(err, dict), f"{where}: 'error' must be an object"

    keys = set(err)
    assert keys >= REQUIRED_ENVELOPE_KEYS, (
        f"{where}: envelope missing required keys "
        f"{sorted(REQUIRED_ENVELOPE_KEYS - keys)}"
    )
    assert keys <= ALLOWED_ENVELOPE_KEYS, (
        f"{where}: envelope has undocumented keys "
        f"{sorted(keys - ALLOWED_ENVELOPE_KEYS)}"
    )

    assert err["code"] == expected_code, (
        f"{where}: expected code {expected_code!r}, got {err['code']!r}"
    )
    assert err["code"] in documented_error_registry(), (
        f"{where}: code {err['code']!r} is not in the section 0.2 registry"
    )
    assert isinstance(err["message"], str) and err["message"], (
        f"{where}: 'message' must be a non-empty string"
    )
    assert isinstance(err["request_id"], str) and err["request_id"], (
        f"{where}: 'request_id' must be a non-empty string"
    )
    assert isinstance(err["retryable"], bool), (
        f"{where}: 'retryable' must be a JSON boolean, got {err['retryable']!r}"
    )
    assert err["docs"] == f"/docs#{err['code'].lower()}", (
        f"{where}: 'docs' must be /docs#<lowercased code>, got {err['docs']!r}"
    )

    if "details" in err:
        assert isinstance(err["details"], dict), f"{where}: 'details' must be an object"
    if "field_errors" in err:
        assert isinstance(err["field_errors"], list) and err["field_errors"], (
            f"{where}: 'field_errors' must be a non-empty array when present"
        )
        for entry in err["field_errors"]:
            assert set(entry) == {"field", "message"}, (
                f"{where}: field_errors entries are {{field, message}}, got "
                f"{sorted(entry)}"
            )

    for header in REQUIRED_ERROR_HEADERS:
        assert response.headers.get(header), (
            f"{where}: error response is missing the {header} header"
        )
    assert response.headers["X-Request-Id"] == err["request_id"], (
        f"{where}: X-Request-Id header and body request_id disagree"
    )
    assert response.headers["X-API-Version"] == "1", (
        f"{where}: X-API-Version must be '1', got "
        f"{response.headers['X-API-Version']!r}"
    )
    return err


# ---------------------------------------------------------------------------
# The documented shape is the shape the app produces
# ---------------------------------------------------------------------------

def test_documented_envelope_keys_are_the_keys_this_module_enforces() -> None:
    """Tie the constants above to the literal sample in section 0.1.

    If the architect adds a field to the documented envelope, this fails first
    and points at the constant that has to grow with it.
    """
    sample = documented_envelope_sample()
    assert set(sample) == {"error"}, (
        f"section 0.1 sample is no longer a single 'error' key: {sorted(sample)}"
    )
    assert set(sample["error"]) == ALLOWED_ENVELOPE_KEYS, (
        "section 0.1 sample keys drifted from ALLOWED_ENVELOPE_KEYS: "
        f"doc-only {sorted(set(sample['error']) - ALLOWED_ENVELOPE_KEYS)}, "
        f"test-only {sorted(ALLOWED_ENVELOPE_KEYS - set(sample['error']))}"
    )


#: (case id, method, path, request kwargs, expected status, expected code).
ERROR_CASES = [
    ("unknown_v1_route", "GET", "/api/v1/definitely-not-a-route", {}, 404, "NOT_FOUND"),
    ("unknown_root_route", "GET", "/definitely-not-a-route", {}, 404, "NOT_FOUND"),
    ("bad_path_param_type", "GET", "/api/v1/models/not-an-int", {}, 422,
     "VALIDATION_ERROR"),
    ("bad_query_param_type", "GET", "/api/v1/models", {"params": {"limit": "twelve"}},
     422, "VALIDATION_ERROR"),
    ("malformed_json_body", "POST", "/api/v1/tags",
     {"content": b"{not valid json", "headers": JSON_HEADERS}, 422,
     "VALIDATION_ERROR"),
    ("missing_csrf_header", "POST", "/api/v1/tags", {"json": {"name": "probe"}}, 400,
     "CSRF_HEADER_MISSING"),
    ("method_not_allowed", "DELETE", "/api/v1/ping", {"headers": VAULT_HEADERS}, 405,
     "VALIDATION_ERROR"),
]


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status", "code"),
    [case[1:] for case in ERROR_CASES],
    ids=[case[0] for case in ERROR_CASES],
)
def test_error_responses_use_the_documented_envelope(
    client, method: str, path: str, kwargs: dict, status: int, code: str,
) -> None:
    """Every reachable failure mode returns the section 0.1 envelope."""
    response = client.request(method, path, **kwargs)
    assert_envelope(response, expected_status=status, expected_code=code)


def test_nonexistent_id_returns_the_documented_envelope(hermetic_client) -> None:
    """A well-formed request for a row that is not there is a 404 envelope.

    Runs against a freshly migrated, empty vault so the id is guaranteed absent
    without depending on the owner's library.
    """
    response = hermetic_client.get("/api/v1/models/999999999")
    err = assert_envelope(response, expected_status=404, expected_code="NOT_FOUND")
    assert err.get("details", {}).get("uid") == "model:999999999", (
        "a 404 for a known entity type should name the uid in details, got "
        f"{err.get('details')!r}"
    )


def test_validation_error_is_wrapped_not_raw_fastapi_detail(client) -> None:
    """422s are the app envelope, never FastAPI's ``{"detail": [...]}``.

    ``middleware.install`` registers ``_validation_handler`` for
    ``RequestValidationError``, which rebuilds the body as
    ``VALIDATION_ERROR`` with one ``field_errors`` entry per pydantic error and
    a ``message`` of ``"<first field>: <first message>"``.  That is the actual
    behaviour and it is what is asserted here.
    """
    response = client.get("/api/v1/models/not-an-int")
    body = response.json()
    assert "detail" not in body, (
        "FastAPI's raw validation body leaked to the client: "
        f"{response.text[:300]}"
    )
    err = assert_envelope(response, expected_status=422,
                          expected_code="VALIDATION_ERROR")
    assert err["field_errors"], "a 422 must populate field_errors (section 0.2)"
    first = err["field_errors"][0]
    assert first["field"] == "model_id", (
        f"field_errors should name the offending parameter, got {first['field']!r}"
    )
    assert err["message"].startswith(f"{first['field']}: "), (
        "message should lead with the offending field name, got "
        f"{err['message']!r}"
    )
    assert err["retryable"] is False, "a validation error is never retryable"


def test_mcp_transport_is_the_only_envelope_exemption(client) -> None:
    """``/api/v1/mcp`` answers in JSON-RPC 2.0, as section 14 delegates.

    Pinned so the deviation stays deliberate: if the MCP router ever starts
    emitting the REST envelope (or another path starts emitting JSON-RPC), this
    test says so.
    """
    # The S-02 guards (CSRF header, Origin) run before the JSON-RPC layer, so a
    # request must clear them to reach the transport whose error shape is pinned
    # here.  Their own rejections are deliberately REST-envelope shaped: the
    # request never became a JSON-RPC message.
    response = client.get("/api/v1/mcp", headers={"X-Vault-Request": "1"})
    assert response.status_code == 406, (
        f"a plain GET on the MCP endpoint should be 406, got {response.status_code}"
    )
    body = response.json()
    assert body.get("jsonrpc") == "2.0", (
        f"the MCP transport must answer in JSON-RPC 2.0, got {response.text[:200]}"
    )
    assert "error" in body and isinstance(body["error"], dict)
    assert "code" in body["error"] and isinstance(body["error"]["code"], int), (
        "a JSON-RPC error carries an integer code, unlike the REST envelope"
    )
    assert set(JSONRPC_TRANSPORT_PATHS) == {"/api/v1/mcp"}, (
        "JSONRPC_TRANSPORT_PATHS grew; every entry needs its own justification "
        "in API_CONTRACT.md"
    )


# ---------------------------------------------------------------------------
# The error-code vocabulary itself
# ---------------------------------------------------------------------------

def _error_code_constants() -> dict[str, str]:
    """Module-level string constants in ``app/core/errors.py``."""
    from app.core import errors

    return {
        name: value for name, value in vars(errors).items()
        if not name.startswith("_") and name.isupper() and isinstance(value, str)
    }


def test_every_error_code_constant_is_upper_snake_and_unique() -> None:
    """The code vocabulary is what clients branch on, so it must be disciplined."""
    codes = _error_code_constants()
    assert codes, "app/core/errors.py exposes no error-code constants"

    bad_shape = {
        name: value for name, value in codes.items()
        if not value or not _UPPER_SNAKE.match(value)
    }
    assert not bad_shape, (
        f"error codes must be non-empty UPPER_SNAKE strings: {sorted(bad_shape.items())}"
    )

    by_name = {name: value for name, value in codes.items() if name != value}
    assert not by_name, (
        "each constant should be named after its own value so grepping a code "
        f"finds its definition: {sorted(by_name.items())}"
    )

    duplicated = [value for value, n in Counter(codes.values()).items() if n > 1]
    assert not duplicated, f"duplicate error-code values: {sorted(duplicated)}"


def test_runtime_error_registry_matches_the_documented_registry() -> None:
    """``middleware.ERROR_STATUS`` is section 0.2, code for code and status for
    status."""
    from app.api.middleware import ERROR_STATUS

    documented = documented_error_registry()
    assert set(ERROR_STATUS) == set(documented), (
        "error-code registry drifted from API_CONTRACT.md section 0.2:\n"
        f"  in code, not documented: {sorted(set(ERROR_STATUS) - set(documented))}\n"
        f"  documented, not in code: {sorted(set(documented) - set(ERROR_STATUS))}"
    )
    mismatched = {
        code: (status, ERROR_STATUS[code])
        for code, status in documented.items() if ERROR_STATUS[code] != status
    }
    assert not mismatched, (
        f"HTTP status disagrees with section 0.2 (doc, code): {mismatched}"
    )


def test_app_error_subclass_statuses_match_the_registry() -> None:
    """Every ``AppError`` subclass agrees with section 0.2, bar the known gap.

    Asserted for equality against ``KNOWN_STATUS_DIVERGENCES`` so a *new*
    disagreement fails the suite rather than joining a tolerated pile.
    """
    from app.api.middleware import ERROR_STATUS
    from app.core import errors

    observed: set[tuple[str, str, int, int]] = set()
    for name, obj in vars(errors).items():
        if not isinstance(obj, type) or not issubclass(obj, errors.AppError):
            continue
        if obj is errors.AppError:
            continue
        registry_status = ERROR_STATUS.get(obj.code)
        if registry_status is not None and registry_status != obj.http_status:
            observed.add((name, obj.code, obj.http_status, registry_status))

    assert observed == set(KNOWN_STATUS_DIVERGENCES), (
        "AppError subclass http_status divergences changed.\n"
        f"  new: {sorted(observed - set(KNOWN_STATUS_DIVERGENCES))}\n"
        f"  fixed (drop it from KNOWN_STATUS_DIVERGENCES): "
        f"{sorted(set(KNOWN_STATUS_DIVERGENCES) - observed)}"
    )


def test_no_error_path_can_return_a_naked_body() -> None:
    """The handlers that make "without exception" true are all installed.

    Losing any one of these silently reopens a hole: an unhandled exception
    would return a traceback, a ``RequestValidationError`` would return
    ``{"detail": [...]}``.
    """
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.api.middleware import ApiError
    from app.core.errors import AppError
    from app.main import app

    for exc_type in (ApiError, AppError, RequestValidationError,
                     StarletteHTTPException, Exception):
        assert exc_type in app.exception_handlers, (
            f"no exception handler registered for {exc_type.__name__}; the "
            f"section 0.1 'without exception' guarantee is broken"
        )
