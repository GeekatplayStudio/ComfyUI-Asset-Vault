"""``apply_update.py`` replaces the application's own files.  Prove it is safe.

This is the highest-consequence script in the repository: it runs before the
engine, it moves directories the app is made of, and a bug in it damages an
installation rather than a request.  So it is tested the way it actually runs -
as a subprocess against a real directory tree in ``tmp_path``, never imported.

The four properties that matter:

* a launch with nothing staged is a no-op that exits 0 (the normal case);
* a staged update replaces the app tree and removes files the release dropped;
* ``backend/data`` and ``venv`` are never touched, because that is the owner's
  library and their Python environment;
* a failure mid-swap rolls back to a working install rather than leaving half
  of two versions behind.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "apply_update.py"


def _install(root: Path, *, version: str = "1.0.0") -> None:
    """A miniature but structurally real installation."""
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend" / "app" / "main.py").write_text(
        f"VERSION = '{version}'\n", encoding="utf-8")
    (root / "backend" / "app" / "goes_away.py").write_text("old\n", encoding="utf-8")
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "frontend" / "dist" / "index.html").write_text(
        f"<b>{version}</b>", encoding="utf-8")
    (root / "README.md").write_text(f"readme {version}\n", encoding="utf-8")

    # The owner's data and environment.  Neither may be read or moved.
    (root / "backend" / "data").mkdir(parents=True)
    (root / "backend" / "data" / "vault.db").write_bytes(b"PRECIOUS")
    (root / "backend" / "data" / "thumbs").mkdir()
    (root / "backend" / "data" / "thumbs" / "a.webp").write_bytes(b"thumb")
    (root / "venv").mkdir()
    (root / "venv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")

    shutil.copy2(SCRIPT, root / "apply_update.py")


def _stage(root: Path, *, version: str = "2.0.0") -> Path:
    updates = root / "backend" / "data" / "updates"
    staged = updates / "staged"
    (staged / "backend" / "app").mkdir(parents=True)
    (staged / "backend" / "app" / "main.py").write_text(
        f"VERSION = '{version}'\n", encoding="utf-8")
    (staged / "backend" / "app" / "brand_new.py").write_text("new\n", encoding="utf-8")
    (staged / "frontend" / "dist").mkdir(parents=True)
    (staged / "frontend" / "dist" / "index.html").write_text(
        f"<b>{version}</b>", encoding="utf-8")
    (staged / "README.md").write_text(f"readme {version}\n", encoding="utf-8")
    (updates / "pending.json").write_text(json.dumps({
        "version": version, "from_version": "1.0.0", "files": 4,
    }), encoding="utf-8")
    return updates


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - a fixed argv of this repo's own script
        [sys.executable, str(root / "apply_update.py")],
        cwd=str(root), capture_output=True, text=True, timeout=120, check=False)


def test_a_launch_with_nothing_staged_does_nothing(tmp_path):
    _install(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "backend" / "app" / "main.py").read_text(
        encoding="utf-8") == "VERSION = '1.0.0'\n"


def test_a_staged_update_replaces_the_app(tmp_path):
    _install(tmp_path)
    _stage(tmp_path)

    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    app = tmp_path / "backend" / "app"
    assert app.joinpath("main.py").read_text(encoding="utf-8") == "VERSION = '2.0.0'\n"
    assert app.joinpath("brand_new.py").is_file()
    # A directory is replaced wholesale, so a file dropped upstream is gone.
    assert not app.joinpath("goes_away.py").exists()
    assert (tmp_path / "frontend" / "dist" / "index.html").read_text(
        encoding="utf-8") == "<b>2.0.0</b>"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "readme 2.0.0\n"


def test_the_owners_data_and_venv_are_never_touched(tmp_path):
    _install(tmp_path)
    _stage(tmp_path)
    _run(tmp_path)

    data = tmp_path / "backend" / "data"
    assert data.joinpath("vault.db").read_bytes() == b"PRECIOUS"
    assert data.joinpath("thumbs", "a.webp").read_bytes() == b"thumb"
    assert (tmp_path / "venv" / "pyvenv.cfg").is_file()


def test_the_staging_area_is_cleared_so_it_applies_once(tmp_path):
    _install(tmp_path)
    updates = _stage(tmp_path)
    _run(tmp_path)

    assert not (updates / "pending.json").exists()
    assert not (updates / "staged").exists()
    # The previous version is kept, which is what makes a rollback possible.
    assert (updates / "backup" / "backend" / "app" / "goes_away.py").is_file()

    # A second launch is a no-op rather than a re-apply.
    second = _run(tmp_path)
    assert second.returncode == 0
    assert (tmp_path / "backend" / "app" / "main.py").read_text(
        encoding="utf-8") == "VERSION = '2.0.0'\n"


def test_an_empty_staged_tree_is_discarded_not_applied(tmp_path):
    """An interrupted download must not blank the installation."""
    _install(tmp_path)
    updates = tmp_path / "backend" / "data" / "updates"
    (updates / "staged").mkdir(parents=True)
    (updates / "pending.json").write_text('{"version": "2.0.0"}', encoding="utf-8")

    result = _run(tmp_path)
    assert result.returncode == 0
    assert (tmp_path / "backend" / "app" / "main.py").is_file()
    assert not (updates / "pending.json").exists()


def test_an_unreadable_marker_is_ignored(tmp_path):
    _install(tmp_path)
    updates = tmp_path / "backend" / "data" / "updates"
    (updates / "staged").mkdir(parents=True)
    (updates / "staged" / "x.py").write_text("x", encoding="utf-8")
    (updates / "pending.json").write_text("{not json", encoding="utf-8")

    result = _run(tmp_path)
    assert result.returncode == 0
    assert (tmp_path / "backend" / "app" / "main.py").read_text(
        encoding="utf-8") == "VERSION = '1.0.0'\n"


@pytest.mark.skipif(sys.platform != "win32",
                    reason="the lock this simulates is a Windows sharing violation")
def test_a_failed_swap_rolls_back_to_a_working_install(tmp_path):
    """Hold a file open so the move fails, then prove the app still runs."""
    _install(tmp_path)
    _stage(tmp_path)

    # An open handle on a file inside frontend/dist makes moving that directory
    # fail on Windows, after backend/app has already been swapped.
    held = tmp_path / "frontend" / "dist" / "index.html"
    with open(held, "rb"):
        result = _run(tmp_path)

    assert result.returncode == 1, result.stdout
    assert "rolled back" in result.stdout.lower()
    # Every part of the original install is back where it belongs.
    assert (tmp_path / "backend" / "app" / "main.py").read_text(
        encoding="utf-8") == "VERSION = '1.0.0'\n"
    assert (tmp_path / "backend" / "app" / "goes_away.py").is_file()
    assert held.read_text(encoding="utf-8") == "<b>1.0.0</b>"
    assert (tmp_path / "backend" / "data" / "vault.db").read_bytes() == b"PRECIOUS"
