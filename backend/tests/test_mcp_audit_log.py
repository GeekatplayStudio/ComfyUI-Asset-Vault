"""``GET /api/v1/mcp/audit`` - the reading half of DECISIONS C5 rail 3.

C5 hands an external MCP client the full file-operation set over a 1.5 TB
library, and the audit row is the only way the owner ever discovers what one of
them did.  Two properties therefore matter equally here:

* the log can be **read** - filtered, paged and summarised - which is what most
  of this module exercises;
* the log cannot be **written** through the API.  An audit trail the application
  can edit or erase is not an audit trail, so the last section asserts that no
  route mutates ``mcp_audit`` and that no source file outside the one writer
  even contains a statement that could.

Everything here is hermetic: a synthetic install and a throwaway vault.db.  The
owner's library is never opened.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

AUDIT = "/api/v1/mcp/audit"

#: A manufactured timeline, oldest first.  ``elapsed_ms`` and ``affected`` are
#: distinct so a sort can be told apart from the insertion order.
SEED = [
    # (tool, transport, outcome, session, uids, affected, elapsed, error_code)
    ("vault_reindex", "http", "ok", "sess-a", [], 1, 10, None),
    ("vault_rename", "http", "ok", "sess-a", ["workflow:7"], 1, 20, None),
    ("vault_delete", "http", "error", "sess-a", ["model:41"], 0, 5,
     "VALIDATION_ERROR"),
    ("vault_delete", "http", "ok", "sess-b", ["model:41", "model:42"], 2, 30, None),
    ("vault_assign_tags", "stdio", "partial", "sess-b", ["output:9"], 1, 40, None),
    ("vault_create_folder", "stdio", "ok", "sess-c", ["root:1"], 1, 50, None),
]

#: Fixed, far in the past, one minute apart, so ``since``/``until`` have a real
#: timeline to cut and the assertions do not depend on the clock.
BASE_TS = 1_700_000_000_000
STEP_MS = 60_000


@pytest.fixture
def audit_client(hermetic_client):
    """A vault whose audit log holds exactly SEED, on a known timeline."""
    from app.core import db as dbmod
    from app.services import mcp_audit

    for tool, transport, outcome, session, uids, affected, elapsed, code in SEED:
        row_id = mcp_audit.record(
            transport=transport, tool=tool,
            arguments={"uids": uids, "note": f"{tool}-args"} if uids
                      else {"mode": "incremental"},
            outcome=outcome, session_id=session, uids=uids, affected=affected,
            error_code=code, elapsed_ms=elapsed)
        assert row_id, "the audit writer refused a row"

    # The writer stamps ``ts`` from the clock and offers no way to set it - as
    # it should.  The test rewrites the timeline directly, which is something
    # only a test does: no application code path can reach this statement.
    def _timeline(conn: sqlite3.Connection) -> None:
        for offset, row in enumerate(
                conn.execute("SELECT id FROM mcp_audit ORDER BY id").fetchall()):
            conn.execute("UPDATE mcp_audit SET ts = ? WHERE id = ?",
                         (BASE_TS + offset * STEP_MS, int(row[0])))
        conn.commit()

    dbmod.writer().run(_timeline)
    return hermetic_client


def get(client, **params):
    response = client.get(AUDIT, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_the_log_reads_back_newest_first(audit_client):
    body = get(audit_client)
    tools = [item["tool"] for item in body["items"]]
    assert tools == [row[0] for row in reversed(SEED)]
    assert [item["ts"] for item in body["items"]] == sorted(
        [item["ts"] for item in body["items"]], reverse=True)
    assert body["page"]["total"] == len(SEED)


def test_an_entry_carries_the_argument_values_it_was_given(audit_client):
    """Rail 3's whole point: values, not merely the fact of a call."""
    body = get(audit_client, tool="vault_delete", outcome="ok")
    entry = body["items"][0]
    assert entry["arguments"]["uids"] == ["model:41", "model:42"]
    assert entry["arguments"]["note"] == "vault_delete-args"
    assert entry["uids"] == ["model:41", "model:42"]
    assert entry["affected"] == 2
    assert entry["elapsed_ms"] == 30
    assert entry["session_id"] == "sess-b"


def test_each_entry_says_what_the_tool_does(audit_client):
    """A delete must not read like a tag assignment in the UI."""
    by_tool = {item["tool"]: item for item in get(audit_client)["items"]}
    assert by_tool["vault_delete"]["kind"] == "destructive"
    assert by_tool["vault_delete"]["destructive"] is True
    assert by_tool["vault_assign_tags"]["kind"] == "write"
    assert by_tool["vault_assign_tags"]["destructive"] is False
    assert all(item["mutating"] for item in by_tool.values())


def test_an_unrecognised_tool_is_reported_rather_than_guessed_at(audit_client):
    from app.services import mcp_audit

    mcp_audit.record(transport="http", tool="vault_from_the_future",
                     arguments={"x": 1}, outcome="ok", session_id="sess-z")
    entry = get(audit_client)["items"][0]
    assert entry["tool"] == "vault_from_the_future"
    assert entry["kind"] == "unknown"
    assert entry["mutating"] is None


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_filter_by_tool_accepts_csv_and_repetition(audit_client):
    csv_form = get(audit_client, tool="vault_delete,vault_rename")
    assert {i["tool"] for i in csv_form["items"]} == {"vault_delete", "vault_rename"}
    assert csv_form["page"]["total"] == 3

    repeated = audit_client.get(
        AUDIT + "?tool=vault_delete&tool=vault_rename").json()
    assert repeated["page"]["total"] == 3


def test_filter_by_outcome(audit_client):
    assert get(audit_client, outcome="error")["page"]["total"] == 1
    assert get(audit_client, outcome="ok")["page"]["total"] == 4
    assert get(audit_client, outcome="ok,error")["page"]["total"] == 5


def test_filter_by_transport(audit_client):
    stdio = get(audit_client, transport="stdio")
    assert stdio["page"]["total"] == 2
    assert {i["transport"] for i in stdio["items"]} == {"stdio"}


def test_filter_by_session(audit_client):
    body = get(audit_client, session_id="sess-a")
    assert body["page"]["total"] == 3
    assert {i["session_id"] for i in body["items"]} == {"sess-a"}


def test_filter_by_time_window(audit_client):
    """``since``/``until`` are epoch ms and inclusive at both ends."""
    third = BASE_TS + 2 * STEP_MS
    assert get(audit_client, since=third)["page"]["total"] == 4
    assert get(audit_client, until=third)["page"]["total"] == 3
    assert get(audit_client, since=third, until=third)["page"]["total"] == 1
    assert get(audit_client, since=BASE_TS - 1,
               until=BASE_TS - 1)["page"]["total"] == 0


def test_free_text_matches_the_tool_name_and_the_affected_uids(audit_client):
    assert get(audit_client, q="rename")["page"]["total"] == 1
    by_uid = get(audit_client, q="model:41")
    assert by_uid["page"]["total"] == 2
    assert all("model:41" in i["uids"] for i in by_uid["items"])
    assert get(audit_client, q="no-such-thing")["page"]["total"] == 0


def test_a_free_text_wildcard_is_matched_literally(audit_client):
    """``%`` is a character in a search box, not a LIKE operator."""
    assert get(audit_client, q="%")["page"]["total"] == 0


def test_filters_combine_with_and(audit_client):
    body = get(audit_client, tool="vault_delete", transport="http", outcome="ok")
    assert body["page"]["total"] == 1
    assert body["items"][0]["session_id"] == "sess-b"


# ---------------------------------------------------------------------------
# Sort, pagination
# ---------------------------------------------------------------------------

def test_sort_is_an_allowlist(audit_client):
    response = audit_client.get(AUDIT, params={"sort": "arguments"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "allowed" in body["error"]["details"]


def test_sort_by_a_named_column(audit_client):
    elapsed = [i["elapsed_ms"] for i in get(audit_client, sort="-elapsed")["items"]]
    assert elapsed == sorted(elapsed, reverse=True)
    tools = [i["tool"] for i in get(audit_client, sort="tool")["items"]]
    assert tools == sorted(tools)


def test_pagination_never_repeats_or_drops_a_row(audit_client):
    first = get(audit_client, limit=2, offset=0)
    second = get(audit_client, limit=2, offset=2)
    third = get(audit_client, limit=2, offset=4)
    ids = [i["id"] for i in first["items"] + second["items"] + third["items"]]
    assert len(ids) == len(SEED)
    assert len(set(ids)) == len(SEED)
    assert ids == sorted(ids, reverse=True)
    assert first["page"]["has_more"] is True
    assert third["page"]["has_more"] is False


def test_page_limits_follow_the_contract(audit_client):
    assert audit_client.get(AUDIT, params={"limit": 0}).status_code == 422
    assert audit_client.get(AUDIT, params={"limit": 501}).status_code == 422
    assert audit_client.get(AUDIT, params={"offset": -1}).status_code == 422


def test_an_impossible_window_is_rejected_with_a_field_error(audit_client):
    response = audit_client.get(AUDIT, params={"since": 200, "until": 100})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["field_errors"][0]["field"] == "until"


def test_an_unknown_outcome_is_rejected(audit_client):
    response = audit_client.get(AUDIT, params={"outcome": "maybe"})
    assert response.status_code == 422
    assert response.json()["error"]["field_errors"][0]["field"] == "outcome"


# ---------------------------------------------------------------------------
# Summary (C11: the UI leads with it)
# ---------------------------------------------------------------------------

def test_the_summary_counts_outcomes_tools_sessions_and_the_time_range(audit_client):
    summary = get(audit_client)["summary"]
    assert summary["total"] == len(SEED)
    assert summary["vault_total"] == len(SEED)
    assert summary["filtered"] is False
    assert summary["by_outcome"] == {"ok": 4, "partial": 1, "error": 1}
    assert summary["by_transport"] == {"http": 4, "stdio": 2}
    assert summary["sessions"] == 3
    assert summary["affected"] == 6
    assert summary["first_ts"] == BASE_TS
    assert summary["last_ts"] == BASE_TS + (len(SEED) - 1) * STEP_MS
    by_tool = {t["tool"]: t for t in summary["by_tool"]}
    assert by_tool["vault_delete"]["count"] == 2
    assert by_tool["vault_delete"]["errors"] == 1
    assert by_tool["vault_delete"]["kind"] == "destructive"
    assert summary["by_kind"]["destructive"] == 3


def test_the_summary_is_measured_under_the_same_filters_as_the_page(audit_client):
    body = get(audit_client, session_id="sess-a")
    assert body["summary"]["total"] == body["page"]["total"] == 3
    assert body["summary"]["filtered"] is True
    assert body["summary"]["sessions"] == 1
    # The one unfiltered figure, so the UI can say "3 of 6".
    assert body["summary"]["vault_total"] == len(SEED)


def test_an_empty_log_answers_with_zeroes_rather_than_an_error(hermetic_client):
    body = hermetic_client.get(AUDIT).json()
    assert body["page"]["total"] == 0
    assert body["items"] == []
    assert body["summary"]["by_outcome"] == {"ok": 0, "partial": 0, "error": 0}
    assert body["summary"]["first_ts"] is None


# ---------------------------------------------------------------------------
# Read-only: the log the app cannot erase
# ---------------------------------------------------------------------------

MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def test_the_audit_path_offers_no_mutating_verb(audit_client):
    for method in MUTATING_METHODS:
        response = audit_client.request(method, AUDIT, json={},
                                        headers={"X-Vault-Request": "1"})
        assert response.status_code == 405, f"{method} {AUDIT} -> {response.status_code}"


def test_no_route_anywhere_in_the_app_targets_the_audit_table():
    """Equality on the route table, so a future write route fails here."""
    from starlette.routing import Mount

    from app.main import app

    def walk(routes, prefix=""):
        out = []
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                context = getattr(route, "include_context", None)
                out += walk(inner.routes,
                            prefix + (getattr(context, "prefix", "") or ""))
                continue
            if isinstance(route, Mount):
                out += walk(getattr(route, "routes", None) or [],
                            prefix + route.path)
                continue
            path = getattr(route, "path", None)
            if path is None:
                continue
            for method in sorted(getattr(route, "methods", None) or []):
                out.append((method, prefix + path))
        return out

    audit_routes = {pair for pair in walk(app.routes) if "audit" in pair[1]}
    assert audit_routes == {("GET", AUDIT)}, sorted(audit_routes)


def test_reading_the_log_cannot_change_it(audit_client):
    from app.core import db as dbmod

    before = dbmod.scalar(dbmod.get_ro(), "SELECT COUNT(*) FROM mcp_audit")
    checksum = dbmod.scalar(
        dbmod.get_ro(),
        "SELECT COALESCE(SUM(id + ts + affected), 0) FROM mcp_audit")
    for params in ({}, {"outcome": "error"}, {"q": "delete"}, {"sort": "tool"},
                   {"limit": 1, "offset": 3}, {"session_id": "sess-a"}):
        assert audit_client.get(AUDIT, params=params).status_code == 200
    assert dbmod.scalar(dbmod.get_ro(), "SELECT COUNT(*) FROM mcp_audit") == before
    assert dbmod.scalar(
        dbmod.get_ro(),
        "SELECT COALESCE(SUM(id + ts + affected), 0) FROM mcp_audit") == checksum


def test_only_one_module_can_write_the_audit_table():
    """Source-level: the table is append-only by construction, not by habit."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    writer = app_dir / "services" / "mcp_audit.py"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "mcp_audit" not in text:
            continue
        offenders.extend(
            f"{path.name}: {statement}"
            for statement in ("delete from mcp_audit", "update mcp_audit",
                              "drop table mcp_audit", "truncate mcp_audit")
            if statement in text)
        if "insert into mcp_audit" in text and path != writer:
            offenders.append(f"{path.name}: a second writer")
    assert not offenders, "the audit log is no longer append-only: " + str(offenders)


def test_the_query_service_exposes_no_write_function():
    from app.services.queries import mcp_audit_query

    exported = [name for name in dir(mcp_audit_query) if not name.startswith("_")]
    forbidden = ("delete", "purge", "prune", "clear", "update", "record", "write",
                 "trim")
    assert not [name for name in exported
                if any(word in name.lower() for word in forbidden)], exported
