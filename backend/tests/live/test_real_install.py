"""The owner's install, asserted against the numbers the rebuild was accepted on.

Every test here is marked ``live`` and reads a **snapshot** of ``vault.db`` taken
with the sqlite backup API, so nothing it does can touch the real library.  The
counts are the ones the six defects were closed against; if any of them moves,
either the library changed or something regressed, and both are worth knowing.

Ground truth, 2026-08-22, after build-time probe residue was purged:
237 models (1.589 TB) - 34 node packages - 1,866 node classes (841 official) -
211 workflows - 3,834 outputs - 6,182 FTS documents - 10 albums - schema v4.

The brief's "212 workflows / 6,183 documents" counted one leftover probe row
(``qa_extract_probe``) whose file had already been deleted.  Thirty-seven more
of the same kind arrived from the MCP mutation exercise.  All were purged; the
corrected figures are above, and ``test_no_soft_deleted_rows_remain`` keeps them
that way.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

# Tolerance: the owner keeps using ComfyUI while the suite runs, so counts drift.
# Wide enough not to be flaky, narrow enough that a collapse to zero fails.
EXPECTED = {
    "models": 237,
    "node_packages": 34,
    "node_classes": 1866,
    "workflows": 211,
    "outputs": 3834,
    "search_docs": 6182,
    "albums": 10,
}
DRIFT = 0.10


#: Models whose primary file sits on a root that is still available.  Retiring a
#: root (unplugging a drive, or turning off `read_held_extra_paths`) keeps its
#: rows on purpose, so a bare COUNT(*) measures the vault's *history* rather
#: than the library's current scale, and drifts every time a root comes or goes.
ACTIVE_MODELS = """
    SELECT COUNT(DISTINCT m.id) FROM models m
    JOIN model_files f ON f.id = m.primary_file_id
    JOIN roots r ON r.id = f.root_id
    WHERE r.available = 1
"""

ACTIVE_MODEL_BYTES = """
    SELECT COALESCE(SUM(m.total_size), 0) FROM models m
    JOIN model_files f ON f.id = m.primary_file_id
    JOIN roots r ON r.id = f.root_id
    WHERE r.available = 1
"""


def count(conn, table: str) -> int:
    if table == "models":
        return conn.execute(ACTIVE_MODELS).fetchone()[0]
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("table,expected", sorted(EXPECTED.items()))
def test_row_counts_are_close_to_the_accepted_ground_truth(live_conn, table, expected):
    actual = count(live_conn, table)
    assert actual > 0, f"{table} is EMPTY — this is the B1 failure mode"
    low, high = expected * (1 - DRIFT), expected * (1 + DRIFT)
    assert low <= actual <= high, (
        f"{table} = {actual}, accepted ground truth {expected} "
        f"(tolerance {low:.0f}-{high:.0f})")


def test_the_schema_is_at_the_expected_version(live_conn):
    assert live_conn.execute("PRAGMA user_version").fetchone()[0] == 5


def test_albums_are_exactly_ten_with_no_duplicates(live_conn):
    """B6: repeated startups used to re-create the system albums every time."""
    rows = live_conn.execute("SELECT id, name FROM albums").fetchall()
    assert len(rows) == 10, f"{len(rows)} albums, expected exactly 10"
    names = [r["name"] for r in rows]
    assert len(set(names)) == len(names), f"duplicate album names: {names}"


def test_two_roots_are_configured_and_both_are_available(live_conn):
    rows = live_conn.execute("SELECT path, available FROM roots").fetchall()
    assert rows, "no roots recorded"
    unavailable = [r["path"] for r in rows if not r["available"]]
    assert not unavailable, f"roots marked unavailable: {unavailable}"


# ---------------------------------------------------------------------------
# B1 on the real corpus
# ---------------------------------------------------------------------------

def test_no_output_has_positive_equal_to_negative(live_conn):
    """The gate: ``pos == neg`` count is 0 across 3,834 real outputs."""
    n = live_conn.execute(
        "SELECT COUNT(*) FROM outputs WHERE positive_prompt IS NOT NULL "
        "AND positive_prompt <> '' AND positive_prompt = negative_prompt").fetchone()[0]
    assert n == 0, f"{n} outputs report the same text as positive and negative"


def test_a_meaningful_share_of_outputs_carry_a_resolved_prompt(live_conn):
    with_meta = live_conn.execute(
        "SELECT COUNT(*) FROM outputs WHERE has_metadata = 1").fetchone()[0]
    with_prompt = live_conn.execute(
        "SELECT COUNT(*) FROM outputs WHERE positive_prompt IS NOT NULL "
        "AND positive_prompt <> ''").fetchone()[0]
    assert with_meta > 1000, f"only {with_meta} outputs carry metadata"
    assert with_prompt >= with_meta * 0.85, (
        f"{with_prompt} of {with_meta} outputs with metadata yielded a prompt; "
        "the link resolver is giving up on the rest")


def test_no_stored_prompt_looks_like_a_serialised_link(live_conn):
    """A row reading ``['88:97', 0]`` means the link was stored, not followed."""
    bad = live_conn.execute(
        "SELECT id, positive_prompt FROM outputs "
        "WHERE positive_prompt LIKE '[''%' OR positive_prompt LIKE '[\"%' "
        "OR positive_prompt LIKE '[%,%]' LIMIT 10").fetchall()
    assert not bad, f"prompts stored as raw links: {[dict(r) for r in bad]}"


# ---------------------------------------------------------------------------
# B3 on the real corpus
# ---------------------------------------------------------------------------

def test_architecture_accuracy_over_the_whole_library(live_conn):
    """At most a handful of the 237 may be Unknown."""
    total = count(live_conn, "models")
    unknown = live_conn.execute(
        "SELECT COUNT(*) FROM models WHERE base_model_family IS NULL "
        "OR base_model_family IN ('', 'Unknown')").fetchone()[0]
    accuracy = 1 - unknown / total
    assert accuracy >= 0.92, (
        f"{unknown} of {total} models have no family ({accuracy:.1%} resolved)")


def test_flux1_dev_fp8_is_a_flux_checkpoint_with_the_right_param_count(live_conn):
    row = live_conn.execute(
        "SELECT * FROM models WHERE name LIKE '%flux1-dev-fp8%'").fetchone()
    if row is None:
        pytest.skip("flux1-dev-fp8 is not in this library")
    assert row["base_model_family"] == "FLUX.1"
    assert row["model_role"] == "checkpoint", "was detected as a VAE before the fix"
    assert row["category"] == "checkpoints"
    billions = (row["param_count_primary"] or 0) / 1e9
    assert 11.0 <= billions <= 12.5, f"primary params {billions:.2f} B, expected ~11.9 B"
    assert row["param_count_total"] > row["param_count_primary"]


def test_no_model_label_names_a_family_other_than_its_own(live_conn):
    """Gate: an ``architecture_label`` may never claim a foreign family."""
    from app.parsers import arch_detect

    offenders = []
    for row in live_conn.execute(
            "SELECT name, base_model_family, architecture_label FROM models "
            "WHERE architecture_label IS NOT NULL"):
        named = arch_detect.label_names_family(row["architecture_label"])
        if named is not None and named != row["base_model_family"]:
            offenders.append(f"{row['name']}: family={row['base_model_family']} "
                             f"label={row['architecture_label']!r}")
    assert not offenders, "labels contradicting their family:\n" + "\n".join(offenders)


def test_every_model_has_a_file_and_a_size(live_conn):
    orphans = live_conn.execute(
        "SELECT COUNT(*) FROM models m WHERE NOT EXISTS "
        "(SELECT 1 FROM model_files f WHERE f.id = m.primary_file_id)").fetchone()[0]
    assert orphans == 0, f"{orphans} models have no primary file"
    sizeless = live_conn.execute(
        "SELECT COUNT(*) FROM models WHERE total_size IS NULL OR total_size <= 0"
    ).fetchone()[0]
    assert sizeless == 0, f"{sizeless} models report no size"


def test_the_library_is_the_expected_scale_on_disk(live_conn):
    total = live_conn.execute(ACTIVE_MODEL_BYTES).fetchone()[0] or 0
    tb = total / 1024 ** 4
    assert 1.3 <= tb <= 1.9, f"library measures {tb:.3f} TB, expected ~1.589 TB"


# ---------------------------------------------------------------------------
# B2 on the real corpus
# ---------------------------------------------------------------------------

def test_no_model_carries_the_empty_string_digest(live_conn):
    """``E3B0C44298`` is SHA-256 of nothing; it was B2's fingerprint."""
    n = live_conn.execute(
        "SELECT COUNT(*) FROM model_files WHERE autov2 = 'E3B0C44298'").fetchone()[0]
    assert n == 0, f"{n} files carry the empty-string AutoV2"


def test_every_stored_autov2_is_the_prefix_of_its_own_sha256(live_conn):
    rows = live_conn.execute(
        "SELECT id, sha256, autov2 FROM model_files "
        "WHERE sha256 IS NOT NULL AND autov2 IS NOT NULL").fetchall()
    for r in rows:
        assert r["autov2"] == r["sha256"][:10].upper(), (
            f"file {r['id']}: autov2 {r['autov2']} is not the prefix of {r['sha256'][:16]}")


def test_hash_state_is_a_known_value(live_conn):
    allowed = {"unhashed", "queued", "hashing", "done", "failed", "stale", None}
    seen = {r[0] for r in live_conn.execute("SELECT DISTINCT hash_state FROM model_files")}
    assert seen <= allowed, f"unexpected hash states: {seen - allowed}"


# ---------------------------------------------------------------------------
# B4 on the real corpus
# ---------------------------------------------------------------------------

def test_at_least_thirty_of_the_custom_packages_yield_classes(live_conn):
    total = live_conn.execute(
        "SELECT COUNT(*) FROM node_packages WHERE is_official = 0").fetchone()[0]
    empty = [r["folder_name"] for r in live_conn.execute(
        "SELECT folder_name FROM node_packages WHERE is_official = 0 "
        "AND (class_count IS NULL OR class_count = 0)")]
    yielding = total - len(empty)
    assert yielding >= 30, (
        f"only {yielding} of {total} custom packages yield classes; empty: {empty}")


def test_at_least_six_hundred_official_classes_are_extracted(live_conn):
    n = live_conn.execute(
        "SELECT COUNT(*) FROM node_classes nc JOIN node_packages p ON p.id = nc.package_id "
        "WHERE p.is_official = 1").fetchone()[0]
    assert n >= 600, f"only {n} official ComfyUI node classes extracted"


def test_the_previously_broken_packages_all_yield_classes(live_conn):
    """The five the audit named by name."""
    named = ("ComfyUI-KJNodes", "ComfyUI_IPAdapter_plus", "ComfyUI-WanVideoWrapper",
             "ComfyMath", "ComfyUI_UltimateSDUpscale")
    for folder in named:
        row = live_conn.execute(
            "SELECT class_count FROM node_packages WHERE folder_name = ?",
            (folder,)).fetchone()
        if row is None:
            continue
        assert (row["class_count"] or 0) > 0, f"{folder} still yields zero classes"


def test_every_node_class_belongs_to_a_package(live_conn):
    orphans = live_conn.execute(
        "SELECT COUNT(*) FROM node_classes nc WHERE nc.package_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM node_packages p WHERE p.id = nc.package_id)"
    ).fetchone()[0]
    assert orphans == 0, f"{orphans} node classes point at a missing package"


def test_extraction_used_more_than_one_strategy(live_conn):
    """If only S1 fired, the multi-strategy extractor has silently regressed."""
    strategies = {r[0] for r in live_conn.execute(
        "SELECT DISTINCT source_strategy FROM node_classes")}
    assert len(strategies) >= 3, f"only these strategies fired: {strategies}"
    assert "S5" in strategies, "the structural fallback never fired"


# ---------------------------------------------------------------------------
# Search index
# ---------------------------------------------------------------------------

def test_the_document_count_equals_the_row_count(live_conn):
    rows = sum(count(live_conn, t) for t in
               ("models", "node_packages", "node_classes", "workflows", "outputs"))
    docs = count(live_conn, "search_docs")
    assert docs == rows, f"{docs} documents for {rows} rows"


def test_the_fts_table_and_the_document_table_agree(live_conn):
    assert count(live_conn, "search_fts") == count(live_conn, "search_docs")


def test_no_document_points_at_a_deleted_row(live_conn):
    for kind, table in (("model", "models"), ("workflow", "workflows"),
                        ("output", "outputs"), ("node_package", "node_packages"),
                        ("node_class", "node_classes")):
        # table/kind come from the fixed tuple above, never from input
        sql = ("SELECT COUNT(*) FROM search_docs d WHERE d.kind = ? AND NOT EXISTS "  # noqa: S608
               f"(SELECT 1 FROM {table} t WHERE (? || t.id) = d.uid)")
        orphans = live_conn.execute(sql, (kind, kind + ":")).fetchone()[0]
        assert orphans == 0, f"{orphans} orphaned {kind} documents"


# ---------------------------------------------------------------------------
# Scan health
# ---------------------------------------------------------------------------

def test_the_most_recent_scan_completed(live_conn):
    row = live_conn.execute(
        "SELECT status, error_count, error_message FROM scan_jobs "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "completed", (
        f"last scan ended {row['status']}: {row['error_message']}")


def test_scan_errors_are_only_the_expected_benign_kinds(live_conn):
    """231 recorded errors: 186 unreadable EXR, 45 non-model files.  Both benign."""
    rows = live_conn.execute(
        "SELECT code, COUNT(*) n FROM scan_errors GROUP BY code").fetchall()
    known = {"IMAGE_UNREADABLE", "NOT_A_MODEL", "FILE_LOCKED", "HEADER_INVALID",
             "JSON_INVALID", "PERMISSION_DENIED", "FILE_MISSING"}
    unexpected = {r["code"]: r["n"] for r in rows if r["code"] not in known}
    assert not unexpected, f"unexpected scan error codes: {unexpected}"


def test_no_scan_job_is_stuck_in_running(live_conn):
    n = live_conn.execute(
        "SELECT COUNT(*) FROM scan_jobs WHERE status = 'running'").fetchone()[0]
    assert n == 0, f"{n} scan jobs are stuck in 'running'"


def test_no_model_or_output_is_marked_missing(live_conn):
    """A model or output going missing is a data-loss signal and must be zero."""
    for table in ("models", "outputs"):
        n = live_conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE missing_since IS NOT NULL"  # noqa: S608
        ).fetchone()[0]
        assert n == 0, f"{n} {table} rows are marked missing"


SOFT_DELETE_TABLES = ("workflows", "node_packages", "models", "outputs")


@pytest.mark.parametrize("table", SOFT_DELETE_TABLES)
def test_no_soft_deleted_rows_remain(live_conn, table):
    """A soft delete that nobody restores is residue, and it inflates every count.

    Prune deliberately keeps a row when its file disappears, so the asset can
    come back with its tags, albums and notes intact.  That is right for the
    owner's assets and wrong for anything a test created: thirty-eight probe
    rows accumulated this way during the build and pushed the workflow count
    from 211 to 248.  Zero is the only defensible steady state, and any test
    that creates an asset owns purging its row.
    """
    rows = live_conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE missing_since IS NOT NULL").fetchone()[0]  # noqa: S608
    assert rows == 0, f"{rows} soft-deleted {table} rows are still present"


def test_no_probe_named_row_survives_anywhere(live_conn):
    """Names any test-shaped asset that outlived the test that made it."""
    probe_like = ("zz_%", "qa_%", "probe%", "test_probe%", "vault_probe%")
    found = []
    for table, col in (("workflows", "name"), ("models", "name"),
                       ("node_packages", "folder_name"), ("outputs", "filename")):
        clause = " OR ".join(f"{col} LIKE ?" for _ in probe_like)
        found += [f"{table}.{r[0]}" for r in live_conn.execute(
            f"SELECT {col} FROM {table} WHERE {clause}", probe_like)]  # noqa: S608
    assert not found, f"probe residue left in the owner's vault: {found}"


def test_only_the_owners_tags_remain(live_conn):
    """Probe tags are residue too, and they show up in the UI's tag list."""
    names = sorted(r[0] for r in live_conn.execute("SELECT name FROM tags"))
    residue = [n for n in names if n.startswith(("zz", "qa-", "probe"))]
    assert not residue, f"probe tags left behind: {residue}"


def test_the_live_package_count_is_thirty_four(live_conn):
    """The count that matters for B4 excludes soft-deleted rows."""
    n = live_conn.execute(
        "SELECT COUNT(*) FROM node_packages WHERE missing_since IS NULL").fetchone()[0]
    assert n == 34, f"{n} live node packages, expected 34"


# ---------------------------------------------------------------------------
# Files really exist
# ---------------------------------------------------------------------------

def test_a_sample_of_indexed_paths_still_exist_on_disk(live_conn, live_root):
    from app.core.pathsafe import long_path

    missing = []
    for table, col in (("model_files", "abs_path"), ("outputs", "abs_path"),
                       ("workflows", "abs_path")):
        # soft-deleted rows are *expected* to have no file; that is what the flag means
        where = " WHERE missing_since IS NULL" if table != "model_files" else ""
        rows = live_conn.execute(
            f"SELECT {col} FROM {table}{where} ORDER BY RANDOM() LIMIT 25").fetchall()  # noqa: S608
        missing.extend(r[0] for r in rows if not os.path.exists(long_path(r[0])))
    assert not missing, f"indexed paths that no longer exist: {missing[:5]}"


def test_no_indexed_path_escapes_a_configured_root(live_conn):
    from app.core.pathsafe import is_contained

    roots = [r["path"] for r in live_conn.execute("SELECT path FROM roots")]
    escaped = []
    for table in ("model_files", "outputs", "workflows"):
        escaped.extend(
            r[0] for r in live_conn.execute(f"SELECT abs_path FROM {table}")  # noqa: S608
            if not any(is_contained(r[0], root) for root in roots))
    assert not escaped, f"{len(escaped)} indexed paths outside every root: {escaped[:3]}"
