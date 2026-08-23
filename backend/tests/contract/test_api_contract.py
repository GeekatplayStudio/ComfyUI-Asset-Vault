"""API_CONTRACT.md <-> FastAPI route table conformance.

``docs/API_CONTRACT.md`` is the frozen source of truth for v1.  This module
parses it and compares the result against the *live* route table taken from
``app.main.app`` -- no server, no ComfyUI, no network.

The document uses three notations for an endpoint, so the parser has to handle
all of them:

* **A** -- ``### `GET /api/v1/system/info` `` headings.  Several endpoints may
  share one heading, joined by ``U+00B7``; headings carry trailing annotations
  (``(SSE)``, ``-> 202``) and sometimes an illustrative query string.
* **B** -- section 12 (albums & tags) is a markdown table only.  One row is
  ``| `PATCH`/`DELETE` | `/api/v1/tags/{id}` |`` and therefore yields *two*
  endpoints.
* **C** -- prose and bullets: section 14 (MCP), section 15 (static & docs) and
  the ``node-packages/update-status`` mention in section 4.

Matching an inline ``VERB /path`` span anywhere in the document (after fenced
code blocks are removed) covers A and C; a per-row table pass covers B.  The
verb guard is what keeps headings such as ``### Workflow `origin` on every
workflow row`` out of the result.

Every known divergence between doc and code is a *named module constant* below
so it is visible in review rather than buried in a fuzzy assertion.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import pytest
from starlette.routing import Mount

pytestmark = pytest.mark.contract

BACKEND_DIR = Path(__file__).resolve().parents[2]
API_CONTRACT_MD = BACKEND_DIR.parent / "docs" / "API_CONTRACT.md"

#: The verbs the contract actually uses.  ``HEAD`` and ``OPTIONS`` are never
#: documented because Starlette synthesises them for every ``GET`` route and for
#: CORS preflight; they are filtered out of the implemented side rather than
#: exempted one by one.
HTTP_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# --- triaged divergence 1: path-parameter names -------------------------------
#: The document writes every path parameter as ``{id}``; the implementation uses
#: a typed name (``{model_id}``, ``{tag_id}``, ...).  This is a naming-style
#: difference, not a routing difference, so both sides are normalised to a
#: ``{}`` sentinel before the sets are compared.  The full list is pinned in
#: PATH_PARAM_NAME_DIVERGENCES below and asserted separately, so the day a doc
#: path stops matching a real route the divergence list catches it.
PARAM_SENTINEL = re.compile(r"\{[^}]*\}")

#: (method, documented path, implemented path) for every endpoint whose only
#: difference is the parameter *name*.  22 method/path pairs over 17 distinct
#: paths.  Informational: this list passing means the divergence is exactly the
#: known naming style and nothing else.
PATH_PARAM_NAME_DIVERGENCES: frozenset[tuple[str, str, str]] = frozenset({
    ("DELETE", "/api/v1/albums/{id}", "/api/v1/albums/{album_id}"),
    ("PATCH", "/api/v1/albums/{id}", "/api/v1/albums/{album_id}"),
    ("POST", "/api/v1/albums/{id}/items", "/api/v1/albums/{album_id}/items"),
    ("DELETE", "/api/v1/albums/{id}/items", "/api/v1/albums/{album_id}/items"),
    ("GET", "/api/v1/models/{id}", "/api/v1/models/{model_id}"),
    ("PATCH", "/api/v1/models/{id}", "/api/v1/models/{model_id}"),
    ("POST", "/api/v1/models/{id}/refresh-metadata",
     "/api/v1/models/{model_id}/refresh-metadata"),
    ("GET", "/api/v1/models/{id}/usage", "/api/v1/models/{model_id}/usage"),
    ("GET", "/api/v1/node-classes/{id}", "/api/v1/node-classes/{class_id}"),
    ("GET", "/api/v1/node-packages/{id}", "/api/v1/node-packages/{package_id}"),
    ("POST", "/api/v1/node-packages/{id}/check-update",
     "/api/v1/node-packages/{package_id}/check-update"),
    ("GET", "/api/v1/node-packages/{id}/classes",
     "/api/v1/node-packages/{package_id}/classes"),
    ("GET", "/api/v1/outputs/{id}", "/api/v1/outputs/{output_id}"),
    ("PATCH", "/api/v1/outputs/{id}", "/api/v1/outputs/{output_id}"),
    ("POST", "/api/v1/outputs/{id}/extract-workflow",
     "/api/v1/outputs/{output_id}/extract-workflow"),
    ("GET", "/api/v1/outputs/{id}/graph", "/api/v1/outputs/{output_id}/graph"),
    ("DELETE", "/api/v1/system/roots/{id}", "/api/v1/system/roots/{root_id}"),
    ("PATCH", "/api/v1/tags/{id}", "/api/v1/tags/{tag_id}"),
    ("DELETE", "/api/v1/tags/{id}", "/api/v1/tags/{tag_id}"),
    ("GET", "/api/v1/workflows/{id}", "/api/v1/workflows/{workflow_id}"),
    ("GET", "/api/v1/workflows/{id}/dependencies",
     "/api/v1/workflows/{workflow_id}/dependencies"),
    ("GET", "/api/v1/workflows/{id}/graph", "/api/v1/workflows/{workflow_id}/graph"),
    ("GET", "/api/v1/workflows/{id}/enable/plan",
     "/api/v1/workflows/{workflow_id}/enable/plan"),
    ("POST", "/api/v1/workflows/{id}/enable/fetch",
     "/api/v1/workflows/{workflow_id}/enable/fetch"),
    ("POST", "/api/v1/workflows/{id}/enable/recheck",
     "/api/v1/workflows/{workflow_id}/enable/recheck"),
})

#: Expected size of PATH_PARAM_NAME_DIVERGENCES, spelled out so the informational
#: test reports the shape of the divergence and not just its identity.
EXPECTED_PARAM_DIVERGENCE_PAIRS = 25
EXPECTED_PARAM_DIVERGENCE_PATHS = 20

# --- triaged divergence 2: implemented but undocumented -----------------------
#: Routes that exist in the app but are absent from API_CONTRACT.md.  This is a
#: real contract gap, not a style difference, so it is asserted for *equality*:
#: a newly added undocumented route fails immediately.
#:
#: ``DELETE /api/v1/mcp`` -- the MCP session-termination verb.  It is specified
#: in ``docs/MCP_SPEC.md`` (transport table, and step 12 of the session
#: walkthrough) but API_CONTRACT.md section 14 lists only ``POST`` and ``GET``.
#: Owner: the architect, who owns API_CONTRACT.md; raised through
#: api-connectivity.  The fix is one word in section 14, not a code change.
UNDOCUMENTED_KNOWN_GAPS: frozenset[tuple[str, str]] = frozenset({
    ("DELETE", "/api/v1/mcp"),
})

# --- triaged divergence 3: framework / app plumbing ---------------------------
#: Routes registered with ``include_in_schema=False`` that are deliberately not
#: part of the public contract: FastAPI's Swagger OAuth2 redirect stub, the
#: favicon shim in ``app/main.py``, and the built-SPA static mount (present only
#: when ``frontend/dist/assets`` exists, so the mount entry is tolerated rather
#: than required).
PLUMBING_EXEMPT: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/favicon.ico"),
    ("GET", "/docs/oauth2-redirect"),
    ("MOUNT", "/assets"),
})

# --- triaged divergence 4: prefix omitted in a prose cross-reference ----------
#: API_CONTRACT.md section 11 (line ~1146) writes "Every route is read-only
#: except `POST /comfyui/update/run`" -- the ``/api/v1`` prefix is missing.  The
#: canonical heading at line ~1220 spells the full path, so this is a typo in a
#: cross-reference and not a second endpoint.  It is rewritten to the canonical
#: path during parsing.  Asserting the resulting root-path set exactly means a
#: *new* unprefixed path in the doc fails the suite instead of being swallowed.
#: Owner: the architect (API_CONTRACT.md).
DOC_PREFIX_OMISSIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("POST", "/comfyui/update/run"): ("POST", "/api/v1/comfyui/update/run"),
}

#: Paths outside ``/api/v1`` that the contract legitimately documents
#: (section 15, "Static & docs").
DOCUMENTED_ROOT_PATHS = frozenset({"/", "/docs", "/openapi.json"})

#: The contract documents 100 ``/api/v1`` endpoints.  Asserted as a floor so a
#: silently broken parser cannot turn this whole module vacuously green.
MIN_DOCUMENTED_V1_ENDPOINTS = 100

#: Endpoints the live smoke test must not touch: they open a never-terminating
#: stream.  ``/api/v1/mcp`` is the MCP Streamable-HTTP SSE channel; its ``GET``
#: half is a stream even though the path does not end in ``/stream``.
LIVE_SMOKE_STREAM_EXEMPT = frozenset({"/api/v1/mcp"})


# ---------------------------------------------------------------------------
# Parsing API_CONTRACT.md
# ---------------------------------------------------------------------------

_FENCE = "`" * 3
_FENCED_BLOCK = re.compile(rf"^{_FENCE}.*?^{_FENCE}", re.DOTALL | re.MULTILINE)
_ENDPOINT = re.compile(r"`(GET|POST|PATCH|PUT|DELETE)\s+(/[^\s`]*)`")
_TABLE_VERB = re.compile(r"`(GET|POST|PATCH|PUT|DELETE)`")
_TABLE_PATH = re.compile(r"`(/[^\s`|]*)`")


def _clean_path(raw: str) -> str:
    """Drop illustrative query strings and any trailing slash."""
    path = raw.split("?", 1)[0].rstrip("/")
    return path or "/"


def normalize(path: str) -> str:
    """Collapse every ``{param}`` segment to a ``{}`` sentinel."""
    return PARAM_SENTINEL.sub("{}", path)


@lru_cache(maxsize=1)
def contract_text() -> str:
    assert API_CONTRACT_MD.is_file(), f"contract document missing: {API_CONTRACT_MD}"
    return API_CONTRACT_MD.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def documented_endpoints() -> frozenset[tuple[str, str]]:
    """Every ``(method, path)`` the contract document declares.

    Fenced code blocks are removed first: the JSON/JSONC response samples quote
    paths that are illustrations, not endpoint declarations.
    """
    body = _FENCED_BLOCK.sub("", contract_text())

    found: set[tuple[str, str]] = set()

    # Formats A and C: an inline `VERB /path` span anywhere in the prose.
    for match in _ENDPOINT.finditer(body):
        found.add((match.group(1), _clean_path(match.group(2))))

    # Format B: a markdown table whose first cell is the method (possibly two
    # methods joined by "/") and whose second cell is the path.
    for line in body.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        verbs = _TABLE_VERB.findall(cells[0])
        paths = _TABLE_PATH.findall(cells[1])
        if verbs and len(paths) == 1:
            for verb in verbs:
                found.add((verb, _clean_path(paths[0])))

    for wrong, right in DOC_PREFIX_OMISSIONS.items():
        if wrong in found:
            found.discard(wrong)
            found.add(right)

    return frozenset(found)


# ---------------------------------------------------------------------------
# The live route table (no server involved)
# ---------------------------------------------------------------------------

def _walk_routes(routes, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten the router tree into ``(method, full path)`` pairs.

    Recent FastAPI wraps ``include_router`` results in a lazy holder that keeps
    the real routes on ``original_router``, so a flat pass over ``app.routes``
    would see a handful of entries instead of a hundred.  Mounts are reported as
    ``("MOUNT", path)`` and then descended into.
    """
    out: list[tuple[str, str]] = []
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            out += _walk_routes(included.routes,
                                prefix + (getattr(context, "prefix", "") or ""))
            continue
        if isinstance(route, Mount):
            out.append(("MOUNT", prefix + route.path))
            out += _walk_routes(getattr(route, "routes", None) or [],
                                prefix + route.path)
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        for method in sorted(getattr(route, "methods", None) or []):
            out.append((method, prefix + path))
    return out


@lru_cache(maxsize=1)
def _route_table() -> tuple[tuple[str, str], ...]:
    from app.main import app

    return tuple(_walk_routes(app.routes))


def implemented_endpoints() -> frozenset[tuple[str, str]]:
    """``(method, path)`` for every route the app registers, real verbs only."""
    return frozenset(
        pair for pair in _route_table() if pair[0] in HTTP_VERBS
    )


def implemented_mounts() -> frozenset[tuple[str, str]]:
    """``("MOUNT", path)`` for every sub-application mounted on the app."""
    return frozenset(pair for pair in _route_table() if pair[0] == "MOUNT")


def _shapes(pairs) -> frozenset[tuple[str, str]]:
    return frozenset((method, normalize(path)) for method, path in pairs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_documented_endpoint_count_matches_expectation() -> None:
    """Guard the parser itself.

    Every other test here compares two sets.  If the parser silently stopped
    matching, both comparisons would pass against an empty set and the contract
    would be unguarded.  Pin a floor instead.
    """
    documented = documented_endpoints()
    v1 = {pair for pair in documented if pair[1].startswith("/api/v1")}
    assert len(v1) >= MIN_DOCUMENTED_V1_ENDPOINTS, (
        f"parsed only {len(v1)} /api/v1 endpoints out of API_CONTRACT.md; "
        f"expected at least {MIN_DOCUMENTED_V1_ENDPOINTS}. The document format "
        f"probably changed and the parser needs updating."
    )
    root = {path for _, path in documented if not path.startswith("/api/v1")}
    assert root == set(DOCUMENTED_ROOT_PATHS), (
        f"unexpected non-/api/v1 paths in the contract: "
        f"{sorted(root ^ set(DOCUMENTED_ROOT_PATHS))}. If a new root path was "
        f"documented, add it to DOCUMENTED_ROOT_PATHS; if a prefix was dropped "
        f"by mistake, add it to DOC_PREFIX_OMISSIONS."
    )


def test_every_documented_method_is_a_real_http_verb() -> None:
    """No pseudo-verbs leaked out of the parser or into the document."""
    methods = {method for method, _ in documented_endpoints()}
    assert methods, "parsed no methods at all"
    assert methods <= set(HTTP_VERBS), (
        f"contract declares non-HTTP methods: {sorted(methods - set(HTTP_VERBS))}"
    )


def test_every_documented_endpoint_is_implemented() -> None:
    """Zero tolerance: everything the frozen contract promises must exist.

    Compared on *shape* -- parameter names normalised to ``{}`` -- because the
    doc/code parameter-naming difference has its own test below.
    """
    missing = _shapes(documented_endpoints()) - _shapes(implemented_endpoints())
    assert not missing, (
        "API_CONTRACT.md documents endpoints the app does not implement:\n  "
        + "\n  ".join(f"{method} {path}" for method, path in sorted(missing))
    )


def test_no_undocumented_endpoint_exists() -> None:
    """Implemented - documented - plumbing must equal the known-gap set.

    Equality, not containment: a brand new route that nobody wrote into the
    contract fails here on the same commit that adds it.
    """
    documented = _shapes(documented_endpoints())
    plumbing = _shapes(PLUMBING_EXEMPT)
    undocumented = {
        (method, path) for method, path in implemented_endpoints()
        if (method, normalize(path)) not in documented
        and (method, normalize(path)) not in plumbing
    }
    assert undocumented == set(UNDOCUMENTED_KNOWN_GAPS), (
        "the set of undocumented routes changed.\n"
        f"  implemented but never documented: "
        f"{sorted(undocumented - set(UNDOCUMENTED_KNOWN_GAPS))}\n"
        f"  no longer a gap (now documented or removed -- drop it from "
        f"UNDOCUMENTED_KNOWN_GAPS): "
        f"{sorted(set(UNDOCUMENTED_KNOWN_GAPS) - undocumented)}"
    )


def test_plumbing_exemptions_are_all_still_real() -> None:
    """An exemption that no longer matches a route is dead weight.

    The ``/assets`` mount exists only once the SPA has been built, so it is
    checked against the mount table and tolerated when the build is absent.
    """
    implemented = implemented_endpoints()
    stale = {
        pair for pair in PLUMBING_EXEMPT
        if pair[0] != "MOUNT" and pair not in implemented
    }
    assert not stale, (
        f"PLUMBING_EXEMPT lists routes that no longer exist: {sorted(stale)}"
    )
    mounts = implemented_mounts()
    unknown_mounts = mounts - PLUMBING_EXEMPT
    assert not unknown_mounts, (
        f"the app mounts a sub-application that is neither documented nor "
        f"exempt: {sorted(unknown_mounts)}"
    )


def test_path_parameter_names_are_the_only_shape_difference() -> None:
    """Informational: enumerate the doc ``{id}`` vs code ``{typed_id}`` cases.

    This test is expected to PASS.  It exists so the divergence is reported by
    name on every run instead of disappearing into the normalisation, and so a
    doc path that stops corresponding to a real route shows up here as a change
    to the list rather than as a silent match.
    """
    documented = documented_endpoints()
    by_shape: dict[tuple[str, str], set[str]] = {}
    for method, path in implemented_endpoints():
        by_shape.setdefault((method, normalize(path)), set()).add(path)

    observed: set[tuple[str, str, str]] = set()
    for method, doc_path in documented:
        if "{" not in doc_path:
            continue
        for impl_path in by_shape.get((method, normalize(doc_path)), set()):
            if impl_path != doc_path:
                observed.add((method, doc_path, impl_path))

    assert observed == set(PATH_PARAM_NAME_DIVERGENCES), (
        "the doc/code path-parameter naming divergence changed.\n"
        f"  new: {sorted(observed - set(PATH_PARAM_NAME_DIVERGENCES))}\n"
        f"  gone: {sorted(set(PATH_PARAM_NAME_DIVERGENCES) - observed)}"
    )
    distinct_paths = {doc_path for _, doc_path, _ in observed}
    assert len(observed) == EXPECTED_PARAM_DIVERGENCE_PAIRS, (
        f"expected {EXPECTED_PARAM_DIVERGENCE_PAIRS} diverging method/path "
        f"pairs, got {len(observed)}"
    )
    assert len(distinct_paths) == EXPECTED_PARAM_DIVERGENCE_PATHS, (
        f"expected {EXPECTED_PARAM_DIVERGENCE_PATHS} distinct diverging paths, "
        f"got {len(distinct_paths)}"
    )


# ---------------------------------------------------------------------------
# Live smoke test
# ---------------------------------------------------------------------------

def _live_get_targets() -> list[str]:
    """Documented GETs that need no path parameter and do not open a stream."""
    return sorted(
        path for method, path in documented_endpoints()
        if method == "GET"
        and path.startswith("/api/v1")
        and "{" not in path
        and not path.endswith("/stream")
        and path not in LIVE_SMOKE_STREAM_EXEMPT
    )


def _probe(client, url: str) -> int | str:
    """Return the status code, or a marker string for a transport outcome."""
    import httpx

    try:
        return client.get(url).status_code
    except httpx.TimeoutException:
        # The route exists and accepted the request; it is merely slow (the
        # ComfyUI update planner shells out to git, `latest` calls GitHub).
        # A timeout is not a contract failure.
        return "timeout"
    except httpx.HTTPError as exc:  # transport level: the route broke the server
        return f"transport-error: {type(exc).__name__}: {exc}"


@pytest.mark.live
def test_documented_parameterless_gets_answer(running_server: str) -> None:
    """Every documented no-parameter GET answers something other than 404/500.

    Skips itself when no backend is listening -- ``running_server`` never starts
    one.  A 4xx other than 404 is fine: a missing required query parameter is a
    422 by contract.  What must not happen is "route not registered" (404) or
    "the handler raised" (500).
    """
    import httpx

    targets = _live_get_targets()
    assert targets, "no live smoke targets parsed out of the contract"

    failures: list[str] = []
    with httpx.Client(base_url=running_server, timeout=20.0,
                      headers={"X-Vault-Request": "1"}) as client:
        for path in targets:
            result = _probe(client, path)
            broke = result in (404, 500) or (
                isinstance(result, str) and result.startswith("transport-error")
            )
            if broke:
                failures.append(f"GET {path} -> {result}")

    assert not failures, (
        "documented GET endpoints failed the live smoke test:\n  "
        + "\n  ".join(failures)
    )
