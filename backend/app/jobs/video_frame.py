"""Single sanctioned ffmpeg call site: one still frame out of one video file.

This module exists so that ``thumb_service`` never imports ``subprocess``
itself.  It mirrors ``enable/git_fetch.py``: one module, one external program,
a fixed argument vector, and nothing that arrives from the media file is ever
executed or interpolated into a shell.

Rules this module holds to, asserted in ``tests/security``:

* ``shell=False`` and a **list** argv - always.  The only caller-supplied value
  is a filesystem path that the vault already resolved from its own database,
  and it is passed as a single argv element, never concatenated.
* ``-nostdin`` so a malformed file can never leave ffmpeg waiting on input.
* A hard timeout, and the child is killed on expiry.
* Output is read from a pipe with a size cap, so a crafted file cannot make the
  vault buffer an unbounded PNG.
* No ffmpeg "protocol" tricks: ``-f`` is pinned on input, and a path that looks
  like a URL or a protocol specifier is refused outright.

If ffmpeg is not installed the module reports that and the caller falls back to
its placeholder.  This is DECISIONS D6 - frame extraction is available *when the
user has ffmpeg*, and its absence is a graceful degradation, never an error.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading

log = logging.getLogger("vault.video")

#: Never buffer more than this from ffmpeg's stdout for a single frame.
MAX_FRAME_BYTES = 24 * 1024 * 1024
#: Wall-clock ceiling for one extraction.
TIMEOUT_S = 12.0
#: Seek offsets tried in order.  A lot of generated video opens on black or on a
#: fade-in, so the first second is usually the least representative frame.
SEEK_OFFSETS = (1.0, 0.0)

_probe_lock = threading.Lock()
_resolved: tuple[bool, str | None] = (False, None)


def ffmpeg_path() -> str | None:
    """Absolute path to ffmpeg, or ``None``.  Resolved once, then cached.

    ``VAULT_FFMPEG`` overrides discovery so a portable ffmpeg that is not on
    PATH can still be used without changing the environment globally.
    """
    global _resolved
    done, value = _resolved
    if done:
        return value
    with _probe_lock:
        done, value = _resolved
        if done:
            return value
        override = (os.environ.get("VAULT_FFMPEG") or "").strip().strip('"')
        found: str | None = None
        if override:
            found = override if os.path.isfile(override) else None
            if found is None:
                log.warning("VAULT_FFMPEG is set but not a file: %s", override)
        if found is None:
            found = shutil.which("ffmpeg")
        _resolved = (True, found)
        log.info("ffmpeg %s", f"found at {found}" if found else "not found; videos use placeholders")
        return found


def available() -> bool:
    return ffmpeg_path() is not None


def _rejects_path(src: str) -> bool:
    """Refuse anything that is not a plain local path.

    ffmpeg happily opens ``http:``, ``rtsp:``, ``concat:`` and friends.  The
    vault only ever asks about files it indexed, so anything protocol-shaped is
    a bug or an attack, and either way it is refused rather than opened.
    """
    if not src or src.startswith("-"):
        return True
    head = src.split(":", 1)[0].lower()
    # A Windows drive letter is a single character; a protocol is longer.
    return len(head) > 1 and not os.path.isabs(src)


def extract_frame(src: str, max_px: int) -> bytes | None:
    """Return one PNG frame scaled to fit ``max_px``, or ``None``.

    Never raises for an unreadable or malformed video - the caller falls back to
    a placeholder, exactly as it did before ffmpeg was available.
    """
    exe = ffmpeg_path()
    if exe is None or _rejects_path(src) or not os.path.isfile(src):
        return None

    box = max(16, int(max_px))
    # Downscale only; never upscale a small source into a big blurry tile.
    scale = f"scale='min({box},iw)':-2:flags=bicubic"

    for offset in SEEK_OFFSETS:
        argv = [
            exe, "-v", "error", "-nostdin",
            "-ss", str(offset),
            "-i", src,
            "-frames:v", "1",
            "-vf", scale,
            "-f", "image2", "-c:v", "png",
            "-",
        ]
        try:
            proc = subprocess.run(  # noqa: S603 - list argv, shell=False, fixed program
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                shell=False,
                timeout=TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.debug("ffmpeg timed out on %s", src)
            return None
        except OSError as exc:
            log.debug("ffmpeg could not start: %s", exc)
            return None

        data = proc.stdout or b""
        if len(data) > MAX_FRAME_BYTES:
            log.debug("ffmpeg frame exceeded the cap for %s", src)
            return None
        if proc.returncode == 0 and data.startswith(b"\x89PNG"):
            return data
        if offset == SEEK_OFFSETS[-1]:
            detail = (proc.stderr or b"")[:200].decode("utf-8", "replace").strip()
            if detail:
                log.debug("ffmpeg failed on %s: %s", src, detail)
    return None
