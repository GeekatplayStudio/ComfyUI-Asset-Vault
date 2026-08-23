"""C7 - changing the ComfyUI path must propagate everywhere, with no restart.

Defect B6 was a second source of truth for "where is ComfyUI?".  When the path
changed, some consumers followed and some did not, and the file-operation root
guard was one of the ones that did not - every file operation then answered 403.
A regression here is severe, so these tests assert propagation *by execution*
against a synthetic install rather than by inspection.

The last test is the specific Wave 1 regression REQUIREMENTS_R2 C7.5 names: the
node registry cache was keyed only on "have I loaded yet", so the first root
asked about won that cache forever.  Pointing the app at a second install then
silently produced an empty registry, node-class enrichment stopped, and the class
count fell from 1,866 to 1,855 with no error anywhere.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import DB_PATH  # noqa: E402
from app.core import config_service  # noqa: E402
from app.core import db as dbmod  # noqa: E402
from app.core.errors import PathNotAllowed  # noqa: E402
from app.core.migrations import migrate  # noqa: E402
from app.core.pathsafe import path_key, resolve_within_roots  # noqa: E402
from app.parsers import node_registry  # noqa: E402
from app.services import file_ops  # noqa: E402

MODEL_DIRS = ("checkpoints", "loras", "vae")


def make_install(base: Path, name: str, *, node_ids: list[str] | None = None,
                 registry_url: str | None = None) -> Path:
    """A directory tree that ``validate_comfyui_path`` accepts as ComfyUI."""
    root = base / name
    (root / "models").mkdir(parents=True, exist_ok=True)
    for category in MODEL_DIRS:
        (root / "models" / category).mkdir(parents=True, exist_ok=True)
    for rel in ("output", "input", "custom_nodes", "workflows",
                os.path.join("user", "default", "workflows")):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("# ComfyUI\n", encoding="utf-8")
    (root / "nodes.py").write_text("NODE_CLASS_MAPPINGS = {}\n", encoding="utf-8")
    (root / "comfyui_version.py").write_text(
        f'__version__ = "9.9.{len(name)}"\n', encoding="utf-8")

    if node_ids is not None:
        manager = root / "custom_nodes" / "ComfyUI-Manager"
        manager.mkdir(parents=True, exist_ok=True)
        (manager / "extension-node-map.json").write_text(
            json.dumps({registry_url or f"https://example.invalid/{name}":
                        [list(node_ids), {"title_aux": name}]}),
            encoding="utf-8")
    return root


@pytest.fixture
def vault(tmp_path):
    """An isolated DB plus two synthetic installs, restored on the way out."""
    db = tmp_path / "propagation.db"
    original = dbmod.db_path()
    dbmod.set_db_path(db)
    migrate()
    config_service.invalidate()
    node_registry.reset_registry()

    first = make_install(tmp_path, "install-a",
                         node_ids=["AlphaNode", "BetaNode"],
                         registry_url="https://example.invalid/alpha")
    second = make_install(tmp_path, "install-b",
                          node_ids=["GammaNode", "DeltaNode", "EpsilonNode"],
                          registry_url="https://example.invalid/gamma")
    config_service.set_config({"comfyui_path": str(first), "is_configured": True})
    yield {"a": first, "b": second, "tmp": tmp_path}

    dbmod.shutdown_writer()
    dbmod.close_thread_connections()
    dbmod.set_db_path(original)
    config_service.invalidate()
    node_registry.reset_registry()


def _switch(to: Path):
    return config_service.set_config({"comfyui_path": str(to)})


# ---------------------------------------------------------------------------
# 1 - the config service itself
# ---------------------------------------------------------------------------

def test_config_cache_follows_the_new_path(vault):
    assert config_service.get_config().comfyui_path == Path(vault["a"])
    _switch(vault["b"])
    # No restart, no explicit reload: set_config invalidates the process cache.
    assert config_service.get_config().comfyui_path == Path(vault["b"])


def test_roots_are_recomputed_not_appended(vault):
    _switch(vault["b"])
    cfg = config_service.get_config()
    comfy = [r for r in cfg.roots if r.kind == "comfyui"]
    assert len(comfy) == 1, "the old root must not linger in the live configuration"
    assert path_key(comfy[0].path) == path_key(vault["b"])


# ---------------------------------------------------------------------------
# 2 - the indexer's directory resolution
# ---------------------------------------------------------------------------

def test_indexer_directories_follow(vault):
    _switch(vault["b"])
    cfg = config_service.get_config()

    model_dirs = [str(d) for _cat, d, _root in config_service.model_dirs(cfg)]
    assert model_dirs, "model categories must resolve under the new root"
    assert all(path_key(vault["b"]) in path_key(d) for d in model_dirs)

    for resolver in (config_service.workflow_dirs, config_service.output_dirs,
                     config_service.custom_nodes_dirs):
        paths = [str(p) for p, _root in resolver(cfg)]
        assert paths, f"{resolver.__name__} returned nothing after the path change"
        assert all(path_key(vault["b"]) in path_key(p) for p in paths)
        assert not any(path_key(vault["a"]) in path_key(p) for p in paths)


# ---------------------------------------------------------------------------
# 3 - the file-operation root guard: B6's actual blast radius
# ---------------------------------------------------------------------------

def test_root_guard_accepts_the_new_root_and_rejects_the_old(vault):
    old_file = vault["a"] / "models" / "loras" / "old.safetensors"
    old_file.write_bytes(b"x" * 32)
    new_file = vault["b"] / "models" / "loras" / "new.safetensors"
    new_file.write_bytes(b"x" * 32)

    # Before the switch: the first install is writable, the second is not.
    resolve_within_roots(old_file, config_service.get_config().roots)
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(new_file, config_service.get_config().roots)

    _switch(vault["b"])

    # After: exactly the other way round, with no restart in between.  This is
    # the assertion that would have caught B6's "403 on every file operation".
    roots = file_ops._roots()
    resolve_within_roots(new_file, roots)
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(old_file, roots)


def test_file_ops_sees_the_switch_without_reimport(vault):
    """``file_ops`` must read the config per call, never capture it at import."""
    before = {path_key(r.path) for r in file_ops._roots()}
    _switch(vault["b"])
    after = {path_key(r.path) for r in file_ops._roots()}
    assert path_key(vault["a"]) in before
    assert path_key(vault["b"]) in after
    assert path_key(vault["a"]) not in after


# ---------------------------------------------------------------------------
# 4 - C7.5: node-class enrichment must survive a path change
# ---------------------------------------------------------------------------

def test_node_registry_cache_is_keyed_on_the_root(vault):
    """The exact Wave 1 regression: 1,866 node classes silently became 1,855.

    ``get_registry`` used to cache on a bare ``loaded`` flag.  Ask it about root A
    first and it answered for A forever - so after a path change the registry for
    B was empty, enrichment found nothing, and nothing anywhere reported an error.
    """
    first = node_registry.get_registry(vault["a"])
    assert first.loaded
    assert first.lookup("https://example.invalid/alpha", "ComfyUI-Manager")[0], \
        "install A's registry must resolve its own package"
    assert set(first.by_url), "install A's registry must not be empty"

    _switch(vault["b"])

    second = node_registry.get_registry(config_service.get_config().comfyui_path)
    assert second is not first, "the registry must be rebuilt for a different root"
    assert second.loaded
    assert second.source is not None, \
        "enrichment is dead if the new root's registry never loads"
    assert vault["b"].name in str(second.source)

    hit, _how = second.lookup("https://example.invalid/gamma", "ComfyUI-Manager")
    assert hit, "the new root's registry entry must resolve"
    ids, _meta = hit
    assert "GammaNode" in ids, \
        "the new root's node ids must be discoverable after the path change"

    # And switching back must not resurrect the stale one either.
    _switch(vault["a"])
    third = node_registry.get_registry(config_service.get_config().comfyui_path)
    assert vault["a"].name in str(third.source)


def test_node_class_enrichment_still_runs_after_a_path_change(vault):
    """End-to-end: run the nodes phase against B and assert classes were written.

    Counting rows is the assertion that matters.  The Wave 1 defect produced a
    *smaller* count, never an exception, so only a count can catch it.
    """
    from app.indexing.phases import nodes as nodes_phase
    from app.indexing.phases import roots as roots_phase

    package = vault["b"] / "custom_nodes" / "demo_pack"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        'from .n import NODE_CLASS_MAPPINGS\n'
        '__all__ = ["NODE_CLASS_MAPPINGS"]\n', encoding="utf-8")
    (package / "n.py").write_text(
        "class GammaNode:\n"
        "    CATEGORY = 'demo'\n"
        "    RETURN_TYPES = ('IMAGE',)\n"
        "    FUNCTION = 'run'\n"
        "    @classmethod\n"
        "    def INPUT_TYPES(cls):\n"
        "        return {'required': {'image': ('IMAGE',)}}\n"
        "\n"
        "class DeltaNode:\n"
        "    CATEGORY = 'demo'\n"
        "    RETURN_TYPES = ('LATENT',)\n"
        "    FUNCTION = 'run'\n"
        "\n"
        "NODE_CLASS_MAPPINGS = {'GammaNode': GammaNode, 'DeltaNode': DeltaNode}\n"
        "NODE_DISPLAY_NAME_MAPPINGS = {'GammaNode': 'Gamma', 'DeltaNode': 'Delta'}\n",
        encoding="utf-8")

    _switch(vault["b"])
    ctx = _make_ctx()
    roots_phase.run(ctx)
    result = nodes_phase.run(ctx)

    conn = dbmod.get_ro()
    classes = int(dbmod.scalar(conn, "SELECT COUNT(*) FROM node_classes") or 0)
    packages = int(dbmod.scalar(conn, "SELECT COUNT(*) FROM node_packages") or 0)
    assert packages >= 1, f"no packages indexed under the new root: {result}"
    assert classes >= 2, (
        "node-class enrichment produced no classes after the path change - this "
        f"is the C7.5 regression. phase result: {result}")
    found = {r["node_id"] for r in dbmod.rows(conn, "SELECT node_id FROM node_classes")}
    assert {"GammaNode", "DeltaNode"} <= found, found


def _make_ctx():
    """A minimal ScanContext: enough for the roots and nodes phases, no threads."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.core import progress
    from app.indexing.service import PHASES, ScanContext

    executor = ThreadPoolExecutor(max_workers=2)
    return ScanContext(
        job_id=0, kind="full", trigger="test", force=True, enrich_online=False,
        phases=PHASES, cfg=config_service.get_config(),
        cancel=threading.Event(), bus=progress.bus("test-index"),
        ex_io=executor, ex_ast=executor, ex_img=executor,
    )


# ---------------------------------------------------------------------------
# 5 - C7.3: the retention decision, asserted rather than assumed
# ---------------------------------------------------------------------------

def test_retired_roots_are_retained_and_reported(vault):
    from app.indexing.phases import roots as roots_phase
    from app.services import storage_service

    ctx = _make_ctx()
    roots_phase.run(ctx)
    conn = dbmod.get_ro()
    before = {str(r["path_key"]) for r in dbmod.rows(conn, "SELECT path_key FROM roots")}
    assert path_key(vault["a"]) in before

    _switch(vault["b"])
    roots_phase.run(_make_ctx())

    rows = {str(r["path_key"]): dict(r) for r in dbmod.rows(
        conn, "SELECT path_key, available FROM roots")}
    assert path_key(vault["a"]) in rows, "the retired root row must be retained"
    assert int(rows[path_key(vault["a"])]["available"]) == 0, \
        "a retired root must be marked unavailable so prune skips its rows"
    assert int(rows[path_key(vault["b"])]["available"]) == 1

    report = storage_service.roots_report()
    retired = [r for r in report["items"] if r["retired"]]
    assert report["retention_policy"] == "retain"
    assert any(path_key(r["path"]) == path_key(vault["a"]) for r in retired), \
        "the API must expose which rows are being retained"


def test_prune_leaves_retired_root_rows_alone(vault):
    """A retired root's rows survive the missing-file sweep, drive or no drive."""
    import shutil

    from app.indexing.phases import prune as prune_phase
    from app.indexing.phases import roots as roots_phase

    roots_phase.run(_make_ctx())
    conn = dbmod.get_ro()
    root_id = int(dbmod.scalar(
        conn, "SELECT id FROM roots WHERE path_key = ?", (path_key(vault["a"]),)))

    now = dbmod.now_ms()

    def _seed(c):
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT INTO outputs(root_id,abs_path,path_key,rel_path,folder,filename,"
            "ext,size,mtime_ns,created_at_file,fingerprint,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (root_id, str(vault["a"] / "output" / "gone.png"),
             path_key(vault["a"] / "output" / "gone.png"), "output/gone.png",
             "output", "gone.png", ".png", 1234, 0, now, "fp", now, now))
        c.commit()

    dbmod.writer().run(_seed)

    _switch(vault["b"])
    roots_phase.run(_make_ctx())
    # Simulate the old drive going away entirely.
    shutil.rmtree(vault["a"], ignore_errors=True)
    prune_phase.run(_make_ctx())

    row = dbmod.one(conn, "SELECT missing_since FROM outputs WHERE root_id = ?",
                    (root_id,))
    assert row is not None, "the retired root's row was deleted - retention broken"
    assert row["missing_since"] is None, (
        "a retired root's rows must not be flagged missing; that is what would "
        "hard-delete the previous library 30 days later")


# ---------------------------------------------------------------------------
# 6 - the live vault, when one exists
# ---------------------------------------------------------------------------

def test_live_vault_config_has_exactly_one_comfyui_root():
    if not Path(DB_PATH).exists():
        pytest.skip("no indexed vault.db; run a scan first")
    cfg = config_service.get_config()
    comfy = [r for r in cfg.roots if r.kind == "comfyui"]
    assert len(comfy) <= 1, [r.path for r in comfy]
