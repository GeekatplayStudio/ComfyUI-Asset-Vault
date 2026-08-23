"""The search index must stay in lockstep with the rows, on every path.

The audit found ``asset_fts`` created but never populated or queried, while
``/api/search`` reloaded every row of all four tables, rebuilt the vocabulary and
re-vectorised the whole corpus **on every keystroke** — O(corpus) per query.

What replaced it only works if one invariant holds: **one row, one document**.
It is easy to satisfy on a fresh scan and easy to break on a mutation, because a
rename has to add the new term *and* stop the old one matching, and a restore has
to put the document back.  Those three are the ones asserted here; the mutation
paths on the real vault are covered by ``tests/test_fts_sync.py``.
"""

from __future__ import annotations

import pytest

from app.core import db as dbmod
from app.search import fts, hybrid, sync

KINDS = ("model", "node_package", "node_class", "workflow", "output")
TABLE_BY_KIND = {"model": "models", "node_package": "node_packages",
                 "node_class": "node_classes", "workflow": "workflows",
                 "output": "outputs"}


def row_total(conn) -> int:
    return sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
               for t in TABLE_BY_KIND.values())


# ---------------------------------------------------------------------------
# Query sanitisation — FTS5 syntax must never reach the user as an error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "flux", "flux dev", "FLUX.1", "wan 2.1", "sd_xl_base",
    '"quoted phrase"', "trailing-", "-leading", "a OR b", "a AND b", "NOT a",
    "(unbalanced", "unbalanced)", "*", "**", "^", "col:value", "a*b",
    "emoji 🎨 name", "日本語モデル", "café", "", "   ", "'", '"', "\\", "%",
    "a" * 500, "NEAR(a b)", "x:y:z", ";DROP TABLE models;--",
])
def test_sanitize_produces_a_query_fts5_can_parse(q, temp_vault):
    """Any string a user can type must produce a result, never a syntax error."""
    conn = dbmod.get_ro()
    cleaned = fts.sanitize(q)
    assert isinstance(cleaned, str)
    try:
        fts.search(conn, cleaned)
    except Exception as exc:  # noqa: BLE001 - the whole point is that none escape
        pytest.fail(f"sanitized {q!r} -> {cleaned!r} still broke FTS5: {exc}")


def test_sql_injection_in_a_query_changes_nothing(temp_vault):
    conn = dbmod.get_ro()
    before = row_total(conn)
    for q in ("'; DROP TABLE models; --", '" OR 1=1 --', "1); DELETE FROM outputs; --"):
        fts.search(conn, fts.sanitize(q))
    assert row_total(dbmod.get_ro()) == before


# ---------------------------------------------------------------------------
# Document/row parity
# ---------------------------------------------------------------------------

@pytest.fixture
def indexed(temp_vault, synthetic_comfyui):
    """A scanned synthetic vault, with outputs so mutations have a subject."""
    import time

    from builders import write_png_with_prompt

    graph = {"1": {"class_type": "CheckpointLoaderSimple",
                   "inputs": {"ckpt_name": "sd15-probe.safetensors"}},
             "2": {"class_type": "CLIPTextEncode",
                   "inputs": {"text": "searchable probe subject", "clip": ["1", 1]}}}
    for i in range(6):
        write_png_with_prompt(synthetic_comfyui / "output" / f"srch_{i}_.png", graph)

    from app.indexing.service import get_indexer

    indexer = get_indexer()
    indexer.start(mode="full", trigger="test")
    deadline = time.monotonic() + 60
    while indexer.running():
        if time.monotonic() > deadline:
            indexer.cancel()
            pytest.fail("scan did not finish")
        time.sleep(0.02)
    return temp_vault


def test_document_count_matches_row_count(indexed):
    conn = dbmod.get_ro()
    rows = row_total(conn)
    docs = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    assert docs == rows, f"{docs} documents for {rows} rows"


def test_the_fts_table_matches_the_document_table(indexed):
    conn = dbmod.get_ro()
    docs = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    assert fts.count(conn) == docs, "search_docs and search_fts have diverged"


def test_every_row_has_exactly_one_document(indexed):
    conn = dbmod.get_ro()
    dupes = conn.execute(
        "SELECT uid, COUNT(*) n FROM search_docs GROUP BY uid HAVING n > 1").fetchall()
    assert not dupes, f"duplicated documents: {[dict(r) for r in dupes]}"
    for kind, table in TABLE_BY_KIND.items():
        missing = conn.execute(
            f"SELECT COUNT(*) FROM {table} t WHERE NOT EXISTS "  # noqa: S608
            "(SELECT 1 FROM search_docs d WHERE d.uid = ? || t.id)", (kind + ":",)
        ).fetchone()[0]
        assert missing == 0, f"{missing} {table} rows have no search document"


def test_a_known_model_is_findable_by_name(indexed):
    hits = hybrid.search("sd15-probe", limit=25)
    names = {(r.get("name") or r.get("title") or "") for r in hits.items}
    assert any("sd15-probe" in (n or "") for n in names), (
        f"an indexed model is not findable by its own name; got {names}")


def test_search_is_not_o_corpus_per_query(indexed):
    """The audit's headline: the vocabulary was rebuilt on every keystroke."""
    import time

    hybrid.search("probe", limit=25)  # warm
    t0 = time.perf_counter()
    for _ in range(20):
        hybrid.search("probe", limit=25)
    per_query_ms = (time.perf_counter() - t0) * 1000 / 20
    assert per_query_ms < 100, (
        f"{per_query_ms:.1f} ms per lexical query on a tiny corpus — "
        "the index is being rebuilt per call")


# ---------------------------------------------------------------------------
# Mutation paths: rename, delete, restore
# ---------------------------------------------------------------------------

def find_uid(kind: str) -> str:
    conn = dbmod.get_ro()
    row = conn.execute(
        f"SELECT id FROM {TABLE_BY_KIND[kind]} ORDER BY id LIMIT 1").fetchone()  # noqa: S608
    if row is None:
        pytest.skip(f"no {kind} rows")
    return f"{kind}:{row['id']}"


def matches(term: str) -> set[str]:
    """uids the lexical index returns for a term."""
    conn = dbmod.get_ro()
    return {uid for uid, _kind, _score in
            fts.search(conn, fts.sanitize(term), limit=200)}


def test_rename_makes_the_new_term_searchable_and_the_old_one_stop(indexed):
    from app.services import file_ops

    uid = find_uid("model")
    conn = dbmod.get_ro()
    old_name = conn.execute("SELECT name FROM models WHERE id = ?",
                            (int(uid.split(":")[1]),)).fetchone()["name"]
    assert uid in matches(old_name), "the model was not findable before the rename"

    new_stem = "zzrenamedprobe"
    results = file_ops.rename(uid, f"{new_stem}.safetensors")
    result = results[0] if isinstance(results, list) else results
    assert getattr(result, "ok", True), getattr(result, "message", result)

    assert uid in matches(new_stem), "the new name is not searchable after a rename"
    assert uid not in matches(old_name), (
        "the old name still matches — the document was added, not replaced")


def test_document_count_still_matches_row_count_after_a_rename(indexed):
    from app.services import file_ops

    uid = find_uid("model")
    file_ops.rename(uid, "zzcountcheck.safetensors")
    conn = dbmod.get_ro()
    docs = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    assert docs == row_total(conn), "a rename changed the document/row balance"


def test_trash_removes_the_document_and_restore_puts_it_back(indexed):
    from app.services import file_ops

    uid = find_uid("output")
    conn = dbmod.get_ro()
    before = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]

    file_ops.delete([uid], mode="trash")
    conn = dbmod.get_ro()
    after_delete = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    assert after_delete == before - 1, "trashing did not remove the document"
    assert uid not in matches("probe"), "a trashed row is still searchable"

    ids = [t["id"] for t in file_ops.trash_list(limit=10)["items"]]
    assert ids, "nothing in the trash after a delete"
    file_ops.trash_restore([ids[0]])

    conn = dbmod.get_ro()
    after_restore = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    assert after_restore == before, (
        f"restore did not re-add the document ({before} -> {after_restore})")
    assert after_restore == row_total(conn)


def test_permanent_delete_leaves_no_orphan_document(indexed):
    from app.services import file_ops

    uid = find_uid("output")
    conn = dbmod.get_ro()
    before = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    file_ops.delete([uid], mode="permanent", confirm=True)
    conn = dbmod.get_ro()
    assert conn.execute("SELECT COUNT(*) FROM search_docs WHERE uid = ?",
                        (uid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == before - 1
    assert conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == row_total(conn)


def test_resync_is_idempotent(indexed):
    conn = dbmod.get_ro()
    uids = [r["uid"] for r in conn.execute("SELECT uid FROM search_docs LIMIT 20")]
    before = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    sync.resync(uids)
    sync.resync(uids)
    conn = dbmod.get_ro()
    assert conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == before


def test_rebuild_reproduces_exactly_the_same_document_set(indexed):
    conn = dbmod.get_ro()
    before = {r["uid"] for r in conn.execute("SELECT uid FROM search_docs")}
    dbmod.writer().run(fts.rebuild)
    conn = dbmod.get_ro()
    after = {r["uid"] for r in conn.execute("SELECT uid FROM search_docs")}
    assert after == before, f"rebuild changed the corpus: {before ^ after}"
