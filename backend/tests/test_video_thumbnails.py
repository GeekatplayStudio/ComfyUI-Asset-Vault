"""Video poster frames, and the cache identity that makes them reach a browser.

Two bugs are pinned here.

1. Videos rendered a placeholder even when ffmpeg was installed.
2. Fixing (1) was not enough: thumbnails are served ``immutable`` with a
   one-year max-age, so every client that had already cached a placeholder kept
   it.  The renderer version therefore has to appear in the cache key, the
   ETag *and* the URL - which means the backend constant and the frontend
   constant must never drift apart.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from app.jobs import video_frame
from app.jobs.thumb_service import THUMB_VERSION, versioned
from app.services.queries import thumb_url

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Cache identity
# --------------------------------------------------------------------------

def test_the_cache_key_carries_the_renderer_version():
    """A file whose bytes never change still needs a new key when we re-render."""
    assert versioned("abc123") == f"abc123:v{THUMB_VERSION}"
    assert versioned("abc123") != "abc123"


def test_the_emitted_url_carries_the_renderer_version():
    url = thumb_url("output:906", 320)
    assert f"v={THUMB_VERSION}" in url, url


def test_the_frontend_mirrors_the_backend_version():
    """The two constants are mirrored by hand; this is what catches the drift.

    If they disagree the frontend keeps requesting the old URL, so a renderer
    change silently never reaches an existing client.
    """
    src = (REPO / "frontend" / "src" / "services" / "api.js").read_text(encoding="utf-8")
    m = re.search(r"export const THUMB_VERSION\s*=\s*(\d+)", src)
    assert m, "frontend THUMB_VERSION not found in services/api.js"
    assert int(m.group(1)) == THUMB_VERSION, (
        f"frontend THUMB_VERSION={m.group(1)} but backend THUMB_VERSION={THUMB_VERSION}; "
        "bump both or cached thumbnails go stale for a year")


# --------------------------------------------------------------------------
# The ffmpeg call site
# --------------------------------------------------------------------------

def test_only_local_paths_are_ever_handed_to_ffmpeg():
    """ffmpeg opens http:, rtsp:, concat: and friends.  The vault must not."""
    for hostile in ("http://evil.test/x.mp4", "https://evil.test/x.mp4",
                    "rtsp://evil.test/s", "concat:a|b", "-i", "-", ""):
        assert video_frame.extract_frame(hostile, 320) is None, hostile


def test_a_missing_file_is_not_an_error():
    assert video_frame.extract_frame(r"Z:\definitely\not\here.mp4", 320) is None


def test_the_ffmpeg_argv_is_a_list_and_never_a_shell(monkeypatch, tmp_path):
    """shell=False and a list argv, so a filename can never become an argument."""
    fake = tmp_path / "clip.mp4"
    fake.write_bytes(b"\x00" * 64)
    seen = {}

    class _Result:
        returncode = 1
        stdout = b""
        stderr = b"nope"

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(video_frame, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(video_frame.subprocess, "run", _fake_run)
    video_frame.extract_frame(str(fake), 320)

    assert isinstance(seen["argv"], list), "argv must be a list, never a string"
    assert seen["kwargs"].get("shell") is False
    assert seen["kwargs"].get("timeout"), "an extraction must be bounded in time"
    assert str(fake) in seen["argv"], "the path is passed as one argv element"


def test_video_frame_is_the_only_module_that_may_start_ffmpeg():
    app_dir = REPO / "backend" / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "video_frame.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "ffmpeg" in text.lower() and "subprocess" in text:
            offenders.append(str(path.relative_to(app_dir)))
    assert not offenders, f"ffmpeg started outside jobs/video_frame.py: {offenders}"


def test_thumb_service_does_not_import_subprocess():
    """The whole point of the split: the thumbnailer stays subprocess-free."""
    src = (REPO / "backend" / "app" / "jobs" / "thumb_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "subprocess" not in imported


# --------------------------------------------------------------------------
# End to end, only where ffmpeg actually exists
# --------------------------------------------------------------------------

@pytest.mark.skipif(not video_frame.available(), reason="ffmpeg is not installed")
def test_a_real_video_yields_a_png_frame(tmp_path):
    """Generate a tiny clip with ffmpeg, then read a frame back out of it."""
    clip = tmp_path / "probe.mp4"
    build = subprocess.run(  # noqa: S603
        [video_frame.ffmpeg_path(), "-v", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "2",
         "-pix_fmt", "yuv420p", str(clip)],
        capture_output=True, shell=False, timeout=60, check=False)
    if build.returncode != 0 or not clip.is_file():
        pytest.skip("this ffmpeg build cannot synthesise a test clip")

    data = video_frame.extract_frame(str(clip), 320)
    assert data, "a readable video must produce a frame"
    assert data.startswith(b"\x89PNG"), "the frame is handed back as PNG"

    import io as _io

    from PIL import Image
    with Image.open(_io.BytesIO(data)) as im:
        assert im.width <= 320
        # A real frame keeps the source aspect; a placeholder would be square.
        assert im.width != im.height
