"""SQL injection: allowlists, not escaping.

The v0 defect was ``models_api.list_models`` interpolating ``sort_column``
straight into the statement.  The structural fix is that every sort, group,
facet and filter token is mapped through a frozen dict before it can reach SQL,
so a token that is not a key simply has no column to become.  These tests prove
the mapping holds on every list surface - including the new ``/storage`` queries,
where ``reclaim_score`` is computed inside SQL - and that the database survives.
"""

from __future__ import annotations

import ast
import re

import pytest

#: Payloads that would matter if any of them reached a statement.
SQL_PAYLOADS = [
    "name; DROP TABLE models--",
    "name'--",
    "name\" --",
    "1) OR 1=1--",
    "name,(SELECT 1)",
    "name UNION SELECT sqlite_version()",
    "name/**/UNION/**/SELECT/**/1",
    "'; ATTACH DATABASE 'evil.db' AS evil--",
    "name) ; UPDATE models SET name='x' WHERE (1=1",
    "name COLLATE NOCASE; DELETE FROM models",
    "\x00name",
    "name\nDROP TABLE models",
]

LIST_ENDPOINTS = [
    ("/api/v1/models", "sort", "group"),
    ("/api/v1/node-packages", "sort", "group"),
    ("/api/v1/node-classes", "sort", "group"),
    ("/api/v1/workflows", "sort", "group"),
    ("/api/v1/outputs", "sort", "group"),
]


def _tables(client) -> set[str]:
    from app.core import db as dbmod

    return {r["name"] for r in dbmod.rows(
        dbmod.get_ro(), "SELECT name FROM sqlite_master WHERE type='table'")}


# ---------------------------------------------------------------------------
# Sort / group / facet allowlists
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint,sort_param,_group", LIST_ENDPOINTS)
@pytest.mark.parametrize("payload", SQL_PAYLOADS)
def test_sort_is_allowlisted(client, endpoint, sort_param, _group, payload):
    response = client.get(endpoint, params={sort_param: payload})
    assert response.status_code == 422, f"{endpoint}?{sort_param}={payload!r}"
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["field_errors"][0]["field"] == "sort"


@pytest.mark.parametrize("endpoint,_sort,group_param", LIST_ENDPOINTS)
@pytest.mark.parametrize("payload", SQL_PAYLOADS[:6])
def test_group_is_allowlisted(client, endpoint, _sort, group_param, payload):
    response = client.get(endpoint, params={group_param: payload})
    assert response.status_code == 422
    assert response.json()["error"]["field_errors"][0]["field"] == "group"


@pytest.mark.parametrize("payload", SQL_PAYLOADS[:6])
def test_storage_sort_is_allowlisted(client, payload):
    """C10.5: the combined reclaim score is computed in SQL - the sort key
    selecting it must still be a dictionary lookup."""
    response = client.get("/api/v1/storage/candidates", params={"sort": payload})
    assert response.status_code == 422
    assert "Unsupported sort" in response.json()["error"]["message"]


def test_storage_reason_filter_is_allowlisted(client):
    response = client.get("/api/v1/storage/candidates",
                          params={"reason": "unused'; DROP TABLE models--"})
    assert response.status_code in (200, 422)
    assert "models" in _tables(client)


def test_storage_stale_days_is_coerced_to_an_integer(client):
    response = client.get("/api/v1/storage/candidates",
                          params={"reason": "stale",
                                  "stale_days": "180; DROP TABLE models--"})
    assert response.status_code == 422
    assert "models" in _tables(client)


# ---------------------------------------------------------------------------
# Free-text filters are bound, never interpolated
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("param", ["q", "folder"])
@pytest.mark.parametrize("payload", SQL_PAYLOADS[:6])
def test_free_text_filters_are_bound_parameters(client, param, payload):
    response = client.get("/api/v1/models", params={param: payload})
    assert response.status_code == 200, response.text[:200]
    assert response.json()["page"]["total"] == 0


def test_search_query_syntax_cannot_break_out_of_fts(client):
    for payload in ('" OR 1=1 --', 'a" NEAR/999999 "b', "*", '""""',
                    "a AND (b OR", "NOT NOT NOT", "column:models"):
        response = client.get("/api/v1/search", params={"q": payload})
        assert response.status_code in (200, 422), payload


def test_tag_filter_is_a_bound_parameter(client):
    response = client.get("/api/v1/models",
                          params={"tag": "x' OR '1'='1"})
    assert response.status_code == 200
    assert response.json()["page"]["total"] == 0


def test_album_id_must_be_an_integer(client):
    response = client.get("/api/v1/models", params={"album_id": "1 OR 1=1"})
    assert response.status_code == 422


def test_root_id_must_be_an_integer(client):
    response = client.get("/api/v1/models", params={"root_id": "1); DROP TABLE models--"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The database survives everything above
# ---------------------------------------------------------------------------

def test_the_schema_is_intact_after_every_payload(client):
    tables = _tables(client)
    for expected in ("models", "model_files", "workflows", "outputs",
                     "node_packages", "node_classes", "trash_items", "mcp_audit"):
        assert expected in tables, f"{expected} is gone"
    assert not any(t.startswith("evil") for t in tables)


# ---------------------------------------------------------------------------
# Static: nothing interpolates a caller value into SQL
# ---------------------------------------------------------------------------

_SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|"
                        r"ATTACH|PRAGMA)\b")
_SQL_CLAUSE = re.compile(r"\b(FROM|WHERE|ORDER\s+BY|GROUP\s+BY|VALUES|SET)\b")


def _sql_fstrings(path):
    """Every f-string in the file whose text is really a SQL statement.

    A prose f-string that happens to contain the word "from" must not trip the
    check, so the literal has to start with a SQL verb *or* carry an uppercase
    SQL clause keyword.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        literal = "".join(v.value for v in node.values
                          if isinstance(v, ast.Constant) and isinstance(v.value, str))
        if _SQL_START.match(literal) or _SQL_CLAUSE.search(literal):
            yield node, literal


def test_no_sql_fstring_interpolates_a_bare_request_value(app_dir):
    """Interpolation is allowed only for module-built fragments and ``?`` runs.

    Every site is checked to interpolate a *name* that the module itself built
    (``where_sql``, ``order``, ``ph``, ``col``, a fixed table) and never a value
    that arrived from the caller.
    """
    allowed_names = {
        "where", "where_sql", "order", "ph", "placeholders", "cols", "col",
        "column", "assignment", "table", "base", "inner", "outer", "clause",
        "size_col", "id_col", "sql", "BM25_WEIGHTS", "t", "names", "chunk",
        "joined", "having", "select", "expr", "extra", "limit_sql", "kind_sql",
        # module-built SQL fragments, each a literal chosen by the module itself
        "hash_reset", "_CLASS_SELECT", "_MODEL_SQL", "_OUTPUT_SQL",
        "_AUDIT_COLUMNS",
    }
    offenders = []
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for node, literal in _sql_fstrings(path):
            for value in node.values:
                if isinstance(value, ast.Constant):
                    continue
                expr = value.value
                if isinstance(expr, ast.Name):
                    name = expr.id
                elif isinstance(expr, ast.Attribute):
                    name = expr.attr
                elif isinstance(expr, ast.Call):
                    name = getattr(expr.func, "attr", getattr(expr.func, "id", "?"))
                    if name in ("join", "int", "escape"):
                        continue
                elif isinstance(expr, ast.Subscript):
                    name = "?subscript"
                elif isinstance(expr, ast.IfExp):
                    continue
                else:
                    name = type(expr).__name__
                if name not in allowed_names:
                    offenders.append((str(path.relative_to(app_dir)), node.lineno,
                                      name, literal[:60]))
    assert not offenders, (
        "SQL f-strings interpolating something other than a module-built "
        f"fragment: {offenders}")


def test_no_percent_or_format_string_building_in_sql(app_dir):
    offenders = []
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            upper = line.upper()
            if not (_SQL_START.match(line.strip().strip('"\'')) or
                    ("SELECT " in upper and "FROM" in upper)):
                continue
            if ".format(" in line or re.search(r'"\s*%\s*\(', line) or \
                    re.search(r"'\s*%\s*\(", line):
                offenders.append((path.name, lineno, line.strip()[:70]))
    assert not offenders, f"SQL built with % or .format(): {offenders}"


def test_the_v0_sort_column_defect_is_gone(app_dir):
    """The exact shape that shipped in ``models_api.list_models``."""
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert "{sort_column}" not in text, path
        assert "{sort_dir}" not in text, path
        assert "ORDER BY {sort" not in text, path
    assert not (app_dir / "api" / "models_api.py").exists()


def test_sort_vocabularies_are_frozen_tuples():
    from app.api.deps import GROUP_VALUES, SORT_FIELDS

    for scope, fields in SORT_FIELDS.items():
        assert isinstance(fields, tuple), scope
    for scope, values in GROUP_VALUES.items():
        assert isinstance(values, tuple), scope


def test_parse_sort_refuses_an_unknown_key():
    from app.core.errors import ValidationError
    from app.services.queries import parse_sort

    with pytest.raises(ValidationError):
        parse_sort("name; DROP TABLE models", {"name": "name"}, "name")


def test_parse_sort_only_ever_emits_allowlisted_columns():
    from app.services.queries import parse_sort

    allowed = {"name": "name COLLATE NOCASE", "size": "total_size"}
    assert parse_sort("-size,name", allowed, "name") == \
        "total_size DESC, name COLLATE NOCASE ASC, id ASC"


# ---------------------------------------------------------------------------
# Pagination bounds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", [e[0] for e in LIST_ENDPOINTS])
def test_limit_maximum_is_enforced(client, endpoint):
    assert client.get(endpoint, params={"limit": 501}).status_code == 422
    assert client.get(endpoint, params={"limit": 10**9}).status_code == 422
    assert client.get(endpoint, params={"limit": -1}).status_code == 422
    assert client.get(endpoint, params={"offset": -1}).status_code == 422


def test_search_limit_maximum_is_enforced(client):
    assert client.get("/api/v1/search",
                      params={"q": "a", "limit": 201}).status_code == 422
