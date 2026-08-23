"""CSRF, CORS and bind posture.

``X-Vault-Request: 1`` is the whole CSRF defence: a cross-origin *simple*
request cannot set a custom header, and anything that would need a preflight is
refused because the built app serves no CORS headers at all.  These tests walk
every mutating route in the live OpenAPI document rather than a hand-written
list, so a new route cannot be added without being covered.
"""

from __future__ import annotations

import os

import pytest

MUTATING = ("post", "put", "patch", "delete")

#: Placeholder values for path parameters, so the request reaches the CSRF
#: dependency rather than dying in routing.
PATH_PARAM_VALUE = "1"


def _mutating_routes(client):
    spec = client.get("/openapi.json").json()
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            if method in MUTATING:
                yield path.replace("{", "").replace("}", ""), path, method, operation


def _concrete(path: str) -> str:
    out = path
    while "{" in out:
        start = out.index("{")
        end = out.index("}")
        out = out[:start] + PATH_PARAM_VALUE + out[end + 1:]
    return out


# ---------------------------------------------------------------------------
# Every mutating v1 route rejects a request without the header
# ---------------------------------------------------------------------------

def test_every_mutating_v1_route_requires_the_csrf_header(naked_client):
    missing = []
    for _flat, path, method, _op in _mutating_routes(naked_client):
        if path == "/api/v1/mcp":
            continue  # covered separately - SECURITY_REVIEW S-02
        response = naked_client.request(method.upper(), _concrete(path),
                                        content=b"{}",
                                        headers={"Content-Type": "application/json"})
        if response.status_code != 400 or \
                response.json().get("error", {}).get("code") != "CSRF_HEADER_MISSING":
            missing.append((method.upper(), path, response.status_code))
    assert not missing, f"mutating routes without CSRF enforcement: {missing}"


def test_the_header_value_must_be_exactly_one(sec_vault):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        for value in ("", "0", "true", "yes", "2"):
            response = client.post("/api/v1/index/start", json={},
                                   headers={"X-Vault-Request": value})
            assert response.status_code == 400, value


def test_read_routes_do_not_require_the_header(naked_client):
    for path in ("/api/v1/ping", "/api/v1/system/info", "/api/v1/system/config",
                 "/api/v1/models", "/api/v1/search?q=x", "/api/v1/system/stats"):
        assert naked_client.get(path).status_code == 200, path


def test_reveal_is_a_get_but_still_demands_the_header(naked_client):
    """``/files/reveal`` starts a local process, so it opts in explicitly."""
    response = naked_client.get("/api/v1/files/reveal", params={"uid": "model:1"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CSRF_HEADER_MISSING"


# ---------------------------------------------------------------------------
# The MCP transport - SECURITY_REVIEW S-02
# ---------------------------------------------------------------------------

def _mcp_initialize(client, **headers):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    import json as _json

    return client.post("/api/v1/mcp", content=_json.dumps(body),
                       headers={"Content-Type": "application/json", **headers})


def test_mcp_rejects_a_non_loopback_origin(naked_client):
    for origin in ("https://evil.example", "http://evil.example:8127",
                   "null", "http://127.0.0.1.evil.example",
                   "http://localhost.evil.example"):
        response = _mcp_initialize(naked_client, Origin=origin)
        assert response.status_code == 403, origin


# S-02 regression gate: this was an open finding and is now fixed.
# It must never be marked xfail again - a failure here is a reopened breach.
def test_mcp_cannot_be_driven_by_a_browser_simple_request(naked_client):
    """A cross-origin POST a browser will send without a preflight."""
    import json as _json

    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    response = naked_client.post(
        "/api/v1/mcp", content=_json.dumps(body),
        headers={"Content-Type": "text/plain", "Origin": "http://127.0.0.1:8188"})
    assert response.status_code in (400, 403, 415), (
        "a simple cross-origin request reached the MCP dispatcher")


def test_mcp_session_id_is_required_after_initialize(naked_client):
    response = naked_client.post(
        "/api/v1/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={"X-Vault-Request": "1"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32001


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

def test_cors_is_never_a_wildcard(client):
    response = client.options(
        "/api/v1/models",
        headers={"Origin": "https://evil.example",
                 "Access-Control-Request-Method": "GET"})
    assert response.headers.get("access-control-allow-origin") != "*"


def test_cors_does_not_echo_an_arbitrary_origin(client):
    response = client.get("/api/v1/models",
                          headers={"Origin": "https://evil.example"})
    assert response.headers.get("access-control-allow-origin") not in (
        "https://evil.example", "*")


def test_cors_allowlist_is_loopback_dev_ports_only():
    from app.main import DEV_ORIGINS

    for origin in DEV_ORIGINS:
        assert origin.startswith(("http://localhost:", "http://127.0.0.1:")), origin


def test_cors_never_allows_credentials():
    import inspect

    from app import main

    source = inspect.getsource(main)
    assert "allow_credentials=True" not in source


# ---------------------------------------------------------------------------
# Bind posture
# ---------------------------------------------------------------------------

def test_default_bind_is_loopback(monkeypatch):
    from app.main import resolve_bind

    monkeypatch.delenv("VAULT_HOST", raising=False)
    monkeypatch.delenv("ALLOW_LAN", raising=False)
    host, port = resolve_bind()
    assert host == "127.0.0.1"
    assert port == 8127


def test_non_loopback_bind_is_refused_without_allow_lan(monkeypatch):
    from app.main import resolve_bind

    monkeypatch.delenv("ALLOW_LAN", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        resolve_bind("0.0.0.0")  # noqa: S104
    assert "loopback-only" in str(excinfo.value)


def test_allow_lan_is_an_explicit_opt_in(monkeypatch, caplog):
    from app.main import resolve_bind

    monkeypatch.setenv("ALLOW_LAN", "1")
    with caplog.at_level("WARNING"):
        host, _port = resolve_bind("0.0.0.0")  # noqa: S104
    assert host == "0.0.0.0"  # noqa: S104
    assert any("ALLOW_LAN=1" in r.message for r in caplog.records), (
        "binding the LAN must be logged loudly")


def test_mcp_http_refuses_to_mount_on_the_lan_without_a_token():
    """MCP_SPEC 9: ALLOW_LAN=1 and no VAULT_MCP_TOKEN -> the router is empty."""
    import importlib
    import sys as _sys

    saved = dict(os.environ)
    os.environ["ALLOW_LAN"] = "1"
    os.environ.pop("VAULT_MCP_TOKEN", None)
    try:
        module = importlib.reload(_sys.modules["app.mcp.http"])
        assert module.MOUNTED is False
        assert not module.router.routes
    finally:
        os.environ.clear()
        os.environ.update(saved)
        importlib.reload(_sys.modules["app.mcp.http"])
