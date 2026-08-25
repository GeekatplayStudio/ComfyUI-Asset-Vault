"""Update detection must be a computed fact, not a permanently-empty promise.

Two regressions are pinned here.  ``models.has_update`` was never written by
anything, so the "Updates available" album and the "Newer version" callout
could not fire even when Civitai knew a newer version existed.  And both
node-package update endpoints ended in an unconditional FEATURE_UNAVAILABLE -
a success path that did not exist behind two visible buttons.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import db as dbmod


def _seed_model(*, sha256: str = "a" * 64) -> int:
    now = dbmod.now_ms()

    def seed(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO roots(kind,path,path_key,label,is_default,source,available,created_at) "
            "VALUES ('comfyui','C:/probe','c:/probe','Probe',1,'config',1,?)", (now,))
        root_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO models(name,category,integrity,created_at,updated_at) "
            "VALUES ('probe','checkpoints','ok',?,?)", (now, now))
        model_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO model_files(model_id,root_id,abs_path,path_key,rel_path,folder,"
            "filename,stem,ext,size,mtime_ns,fingerprint,format,hash_state,sha256,"
            "first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (model_id, root_id, "C:/probe/probe.safetensors", "c:/probe/probe.safetensors",
             "checkpoints/probe.safetensors", "checkpoints", "probe.safetensors", "probe",
             ".safetensors", 1, now * 1_000_000, "fp-probe", "safetensors", "done",
             sha256, now, now))
        file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE models SET primary_file_id=? WHERE id=?", (file_id, model_id))
        conn.commit()
        return model_id

    return int(dbmod.writer().run(seed))


_pkg_counter = iter(range(1, 1000))


def _seed_package(*, repo_url: str | None, commit: str | None,
                  branch: str | None = "main", suspect: bool = False) -> int:
    now = dbmod.now_ms()
    name = f"pkg{next(_pkg_counter)}"

    def seed(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO node_packages(folder_name,path_key,abs_path,display_name,"
            "fingerprint,repo_url,repo_url_suspect,git_branch,git_commit,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, f"c:/probe/{name}", f"C:/probe/{name}", "Probe pack", f"fp-{name}",
             repo_url, 1 if suspect else 0, branch, commit, now, now))
        pkg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return pkg_id

    return int(dbmod.writer().run(seed))


def _model_row(model_id: int) -> dict:
    row = dbmod.one(dbmod.get_ro(),
                    "SELECT has_update, latest_version_id, latest_version_name "
                    "FROM models WHERE id=?", (model_id,))
    return dict(row)


def _package_row(pkg_id: int) -> dict:
    row = dbmod.one(dbmod.get_ro(),
                    "SELECT has_update, latest_commit, update_check_state, update_notes "
                    "FROM node_packages WHERE id=?", (pkg_id,))
    return dict(row)


# --------------------------------------------------------------- Civitai side

def _run_enrich(monkeypatch, model_id: int, *, matched_version: int,
                latest_version: int | None):
    from app.services import civitai_service

    async def fake_by_hash(_hash, *, force=False):
        return {"civitai_model_id": 777, "civitai_version_id": matched_version,
                "civitai_url": None, "description": None,
                "trigger_words_json": [], "download_url": None, "nsfw": 0,
                "rating": None, "download_count": None,
                "latest_version_name": "matched", "latest_version_id": matched_version,
                "latest_version_notes": None, "base_model": None,
                "preview_image": None, "recommended_settings_json": None}

    async def fake_latest(_model_id, *, force=False):
        if latest_version is None:
            return None
        return {"latest_version_id": latest_version,
                "latest_version_name": f"v{latest_version}",
                "latest_version_notes": None}

    monkeypatch.setattr(civitai_service, "fetch_by_hash", fake_by_hash)
    monkeypatch.setattr(civitai_service, "fetch_latest_version", fake_latest)
    return asyncio.run(civitai_service.enrich_model(model_id))


def test_a_newer_civitai_version_sets_has_update(temp_vault, monkeypatch):
    model_id = _seed_model()
    result = _run_enrich(monkeypatch, model_id, matched_version=100, latest_version=200)
    assert result["state"] == "matched"
    row = _model_row(model_id)
    assert row["has_update"] == 1
    assert row["latest_version_id"] == 200
    assert row["latest_version_name"] == "v200"


def test_the_current_version_does_not_claim_an_update(temp_vault, monkeypatch):
    model_id = _seed_model()
    _run_enrich(monkeypatch, model_id, matched_version=100, latest_version=100)
    assert _model_row(model_id)["has_update"] == 0


def test_a_failed_latest_lookup_never_blocks_the_match(temp_vault, monkeypatch):
    """Update detection is a bonus on a match, not a gate in front of it."""
    model_id = _seed_model()
    result = _run_enrich(monkeypatch, model_id, matched_version=100, latest_version=None)
    assert result["state"] == "matched"
    assert _model_row(model_id)["has_update"] == 0


# ---------------------------------------------------------- node package side

def test_a_moved_remote_tip_marks_the_package_behind(temp_vault, monkeypatch):
    from app.enable import git_fetch
    from app.services import node_update_service

    local = "b" * 40
    remote = "c" * 40
    pkg = _seed_package(repo_url="https://github.com/x/y", commit=local)
    monkeypatch.setattr(git_fetch, "resolve_revision",
                        lambda url, ref=None, timeout_s=45: (remote, None))
    result = node_update_service.check_package(pkg)
    assert result == {"state": "ok", "reason": None,
                      "has_update": True, "latest_commit": remote}
    row = _package_row(pkg)
    assert row["has_update"] == 1
    assert row["latest_commit"] == remote
    assert row["update_check_state"] == "ok"


def test_a_matching_tip_reports_up_to_date(temp_vault, monkeypatch):
    from app.enable import git_fetch
    from app.services import node_update_service

    local = "b" * 40
    pkg = _seed_package(repo_url="https://github.com/x/y", commit=local)
    monkeypatch.setattr(git_fetch, "resolve_revision",
                        lambda url, ref=None, timeout_s=45: (local, None))
    result = node_update_service.check_package(pkg)
    assert result["has_update"] is False
    assert _package_row(pkg)["has_update"] == 0


def test_an_unreachable_remote_is_an_error_not_a_lie(temp_vault, monkeypatch):
    from app.enable import git_fetch
    from app.services import node_update_service

    pkg = _seed_package(repo_url="https://github.com/x/y", commit="b" * 40)
    monkeypatch.setattr(git_fetch, "resolve_revision",
                        lambda url, ref=None, timeout_s=45: (None, "network down"))
    result = node_update_service.check_package(pkg)
    assert result["state"] == "error"
    row = _package_row(pkg)
    assert row["update_check_state"] == "error"
    assert row["update_notes"] == "network down"


@pytest.mark.parametrize("repo_url,commit,expected", [
    (None, "b" * 40, "none"),          # no remote recorded
    ("https://github.com/x/y", None, "error"),  # no local commit to compare
])
def test_unanswerable_packages_say_why(temp_vault, repo_url, commit, expected):
    from app.services import node_update_service

    pkg = _seed_package(repo_url=repo_url, commit=commit)
    assert node_update_service.check_package(pkg)["state"] == expected


def test_a_suspect_remote_is_never_contacted(temp_vault, monkeypatch):
    from app.enable import git_fetch
    from app.services import node_update_service

    def boom(*a, **kw):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("ls-remote must not run for a suspect remote")

    monkeypatch.setattr(git_fetch, "resolve_revision", boom)
    pkg = _seed_package(repo_url="https://github.com/x/y", commit="b" * 40,
                        suspect=True)
    assert node_update_service.check_package(pkg)["state"] == "suspect_remote"


def test_enqueue_marks_only_checkable_packages(temp_vault, monkeypatch):
    from app.services import node_update_service

    # Keep the drain worker out of it: this test is about the queueing rule.
    monkeypatch.setattr(node_update_service.threading, "Thread",
                        lambda *a, **kw: type("T", (), {"start": lambda self: None})())
    checkable = _seed_package(repo_url="https://github.com/x/y", commit="b" * 40)
    no_remote = _seed_package(repo_url=None, commit=None)
    result = node_update_service.enqueue_checks(None)
    assert result["queued"] == 1
    assert _package_row(checkable)["update_check_state"] == "pending"
    assert _package_row(no_remote)["update_check_state"] == "none"
