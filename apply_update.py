#!/usr/bin/env python3
"""Apply a staged Asset Vault update.  Run by the launcher, before the engine.

Geekatplay ComfyUI Asset Vault - Geekatplay Studio - Vladimir Chopine

This is the only code that replaces the application's own files, and it runs at
exactly one moment: after a launcher starts and before ``uvicorn`` imports
anything, when no request is in flight and no module is loaded.  That is the
whole reason it lives outside ``backend/app`` and imports nothing from it - the
tree it is about to replace must not be the tree it is running from.

What it does, in order:

1. read ``backend/data/updates/pending.json``; exit 0 immediately if absent,
   because "no update staged" is the normal case on every launch;
2. move the current ``backend/app``, ``frontend/dist`` and top-level scripts
   into ``backend/data/updates/backup/``;
3. move the staged tree into place;
4. on any failure, put the backup back and exit non-zero, so a broken update
   leaves a working app rather than a half-written one.

``backend/data`` (the database, thumbnails, the embedding model) and ``venv``
are never read, moved or deleted by this script.

Exit codes: 0 nothing to do or applied cleanly, 1 failed and rolled back,
2 failed *and* the rollback failed - the only case needing a manual restore,
and it prints exactly where the backup is.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPDATES = ROOT / "backend" / "data" / "updates"
STAGED = UPDATES / "staged"
BACKUP = UPDATES / "backup"
MARKER = UPDATES / "pending.json"
LOG = UPDATES / "last-apply.log"

#: Directories replaced wholesale.  Anything the new release does not ship is
#: gone afterwards, which is what makes a removal in a release take effect.
REPLACE_DIRS = ("backend/app", "frontend/dist", "docs")


def say(message: str) -> None:
    print(f"[update] {message}", flush=True)


def _rel_targets() -> list[str]:
    """Every path the staged tree wants to place, as install-relative strings."""
    targets: list[str] = []
    for entry in STAGED.rglob("*"):
        if entry.is_file():
            targets.append(entry.relative_to(STAGED).as_posix())
    return targets


def _set_aside(live: Path, saved: Path, moved: list[tuple[Path, Path]]) -> None:
    """Move one live path into the backup, recording it *before* the attempt.

    Recording first is the whole point.  ``shutil.move`` falls back to
    copy-then-delete when a rename cannot work, so a failure can leave the
    destination partly written - and an unrecorded partial move is one the
    rollback would not know to repair.
    """
    saved.parent.mkdir(parents=True, exist_ok=True)
    moved.append((live, saved))
    shutil.move(str(live), str(saved))


def _swap_in(rel_files: list[str], moved: list[tuple[Path, Path]]) -> None:
    """Move current files aside, then move staged ones in.

    ``moved`` is the caller's list and is appended to as work proceeds, so an
    exception leaves the caller holding every move that needs undoing.
    """
    # Whole directories first, so a file deleted upstream really disappears.
    for rel in REPLACE_DIRS:
        live = ROOT / rel
        if not live.exists():
            continue
        if not any(f.startswith(rel + "/") for f in rel_files):
            continue  # this release ships nothing for that directory
        _set_aside(live, BACKUP / rel, moved)

    for rel in rel_files:
        source = STAGED / rel
        target = ROOT / rel
        if any(rel.startswith(d + "/") for d in REPLACE_DIRS):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            continue
        if target.exists():
            _set_aside(target, BACKUP / rel, moved)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))


def _roll_back(moved: list[tuple[Path, Path]]) -> bool:
    """Put every set-aside path back.  Returns False only on a real failure."""
    ok = True
    for live, saved in reversed(moved):
        try:
            # Never clear the live path unless there is a backup to restore in
            # its place: a recorded move that never started leaves the live
            # tree authoritative, and deleting it would cause the damage this
            # function exists to prevent.
            if not saved.exists():
                continue
            if live.exists():
                if live.is_dir():
                    shutil.rmtree(live, ignore_errors=True)
                else:
                    live.unlink(missing_ok=True)
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(saved), str(live))
        except OSError as exc:  # noqa: PERF203 - each failure is reported
            say(f"ROLLBACK FAILED for {live}: {exc}")
            ok = False
    return ok


def main() -> int:
    if not MARKER.is_file() or not STAGED.is_dir():
        return 0

    try:
        marker = json.loads(MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        say(f"the staged update is unreadable and was ignored: {exc}")
        return 0

    version = marker.get("version") or "unknown"
    say(f"applying {marker.get('from_version', '?')} -> {version}")

    rel_files = _rel_targets()
    if not rel_files:
        say("the staged update is empty; discarding it")
        shutil.rmtree(STAGED, ignore_errors=True)
        MARKER.unlink(missing_ok=True)
        return 0

    shutil.rmtree(BACKUP, ignore_errors=True)
    BACKUP.mkdir(parents=True, exist_ok=True)

    moved: list[tuple[Path, Path]] = []
    try:
        _swap_in(rel_files, moved)
    except (OSError, shutil.Error) as exc:
        say(f"FAILED: {exc}")
        say("rolling back to the previous version ...")
        if _roll_back(moved):
            say("rolled back; the app is unchanged.")
            shutil.rmtree(STAGED, ignore_errors=True)
            MARKER.unlink(missing_ok=True)
            return 1
        say(f"MANUAL RESTORE NEEDED - your previous files are in {BACKUP}")
        return 2

    shutil.rmtree(STAGED, ignore_errors=True)
    MARKER.unlink(missing_ok=True)
    try:
        LOG.write_text(
            f"applied {marker.get('from_version', '?')} -> {version}\n"
            f"files: {len(rel_files)}\n"
            f"previous version kept in: {BACKUP}\n",
            encoding="utf-8")
    except OSError:
        pass
    say(f"updated to {version}. The previous version is kept in {BACKUP}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("interrupted")
        sys.exit(1)
